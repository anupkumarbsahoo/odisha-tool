"""Playwright (Firefox, persistent profile) flow for One Time Registration.

Runs in a worker thread. ``ui`` must provide status(msg), warn(title, msg),
ask(title, msg) -> bool, ask_text(title, msg, image=None) -> str | None,
show_username(username), ask_choice(title, msg) and start_wait/wait_choice/end_wait for the
non-blocking "please fill this in Firefox" notice; it marshals them onto Tk.
Selectors and fixed values come from config.json "registration".
"""
import json
import re
from dataclasses import asdict
from pathlib import Path

from playwright.sync_api import sync_playwright

from automation import (BASE_DIR, Cancelled, _same, _transform, choose, choose_fuzzy, launch_browser,
                        save_debug, selected_text, settle, wait_for_close_or_refill)
from docprep import prepare_upload


class _Missing(Exception):
    """An optional field is not on the page."""


def _xpath_literal(text):
    return f'"{text}"' if '"' not in text else f"'{text}'"


def field_locator(page, step, section, tag):
    """Locate a form control from a step: an explicit "sel", or the first visible control whose
    label starts with step["label"], searching forward from the section heading."""
    if step.get("sel"):
        return page.locator(step["sel"]).filter(visible=True).first
    label = _xpath_literal(step["label"])
    start = ""
    if section:
        start = f"{_heading(section)}/following::"
    else:
        start = "//"
    xpath = f"xpath={start}label[starts-with(normalize-space(.), {label})]/ancestor::div[1]//{tag}"
    return page.locator(xpath).filter(visible=True).first


def _heading(section):
    """XPath for the element holding a section heading (matches any of its own text pieces)."""
    return f"//*[text()[starts-with(normalize-space(.), {_xpath_literal(section)})]]"


def _visible(page, selector):
    """The site repeats some ids in hidden popups (e.g. two #username fields), so use the visible one."""
    return page.locator(selector).filter(visible=True).first


def save_session(session):
    """Keep the documents, fields and username per Aadhaar so a created account is never registered twice."""
    folder = BASE_DIR / "sessions"
    folder.mkdir(exist_ok=True)
    (folder / f"reg_{session.fields.get('app_aadhaar', 'unknown')}.json").write_text(
        json.dumps(asdict(session), indent=2, ensure_ascii=False), encoding="utf-8")


def load_saved_username(aadhaar):
    try:
        data = json.loads((BASE_DIR / "sessions" / f"reg_{aadhaar}.json").read_text(encoding="utf-8"))
        return data.get("username", "")
    except (OSError, ValueError):
        return ""


def _ask_until(ui, title, message, image=None):
    value = ui.ask_text(title, message, image)
    if value is None:
        raise Cancelled()
    return value.strip()


class RegistrationRun:
    def __init__(self, config, session, ui, page, messages):
        self.cfg = config["registration"]
        self.config = config
        self.session = session
        self.fields = session.fields
        self.ui = ui
        self.page = page
        self.date_format = config.get("date_format", "%Y-%m-%d")
        self.dropdown_timeout = config.get("dropdown_load_timeout_ms", 20000)
        self.threshold = config.get("village_match_threshold", 80)
        self.messages = messages
        self.done_once = set()
        self.section = None
        self.summary, self.mismatches, self.manual = [], [], []

    # --- page helpers -------------------------------------------------------
    def value(self, step):
        """A key starting with "=" is a fixed value; anything else names a session field."""
        key = step["key"]
        if key.startswith("="):
            return key[1:]
        return str(self.fields.get(key, "") or "")

    def name(self, step):
        return step.get("label") or step.get("key", "").lstrip("=") or step.get("sel")

    def locate(self, step, tag):
        """Find a control by its id; if that id is not shown (the site has hidden twins, e.g. the
        Aadhaar OTP box), also accept the control found by its label."""
        loc = field_locator(self.page, step, self.section, tag)
        timeout = 4000 if step.get("optional") else None
        try:
            if step.get("sel") and step.get("label"):
                try:
                    loc.wait_for(state="visible", timeout=3000)
                    return loc
                except Exception:
                    by_label = field_locator(self.page, {"label": step["label"]}, self.section, tag)
                    loc = self.page.locator(step["sel"]).or_(by_label).filter(visible=True).first
            loc.wait_for(state="visible", timeout=timeout)
        except Exception:
            if step.get("optional"):
                raise _Missing()
            raise
        return loc

    def fill(self, step):
        kind = step.get("transform")
        desired = _transform(self.value(step), kind, self.date_format)
        loc = self.locate(step, "input")
        current = loc.input_value()
        if current.strip():
            if desired and not _same(current, desired, kind):
                self.mismatches.append((self.name(step), current, desired))
            return
        if not desired:
            self.summary.append(f"{self.name(step)}: (left blank)")
            return
        loc.fill(desired)
        loc.dispatch_event("change")
        self.summary.append(f"{self.name(step)}: {desired}")

    def ask_fill(self, step):
        loc = self.locate(step, "input")
        if loc.input_value().strip():
            return
        value = self.value(step)
        if not value:
            value = _ask_until(self.ui, step["label"], step["prompt"])
            self.fields[step["key"]] = value
        loc.fill(value)
        loc.dispatch_event("change")
        self.summary.append(f"{self.name(step)}: {value}")

    def select(self, step):
        label = self.name(step)
        if step.get("or_fill"):
            try:
                field_locator(self.page, step, self.section, "select").wait_for(state="visible", timeout=8000)
            except Exception:
                return self.fill({**step, "sel": step["or_fill"]})
        loc = self.locate(step, "select")
        wanted = self.value(step)
        current = selected_text(self.page, loc)
        if current:
            if wanted and not step.get("fuzzy") and not _same(current, wanted, None):
                self.mismatches.append((label, current, wanted))
            return
        if not wanted:
            self.wait_for_user(label, f"There is no value for {label} in the documents or extra details.", loc)
            return
        if step.get("fuzzy"):
            matched, score, best = choose_fuzzy(self.page, loc, wanted, self.threshold, self.dropdown_timeout)
            if not matched:
                self.wait_for_user(label, f"No option close enough to '{wanted}' (best: '{best}', {score:.0f}%).",
                                   loc)
                return
            self.summary.append(f"{label}: {matched}" + (f"  [{score:.0f}% match for '{wanted}']" if score < 100 else ""))
        else:
            try:
                chosen = choose(self.page, loc, wanted, self.dropdown_timeout)
            except RuntimeError:
                self.wait_for_user(label, f"'{wanted}' is not one of the options.", loc)
                return
            self.summary.append(f"{label}: {chosen}")
        settle(self.page, 3000)

    def wait_for_user(self, label, reason, loc, kind="select"):
        """Ask the user to do one field in Firefox and continue by itself as soon as it has a value."""
        verb = {"select": "choose", "check": "tick"}.get(kind, "fill in")
        self.manual.append(f"{label} (by hand)")
        self.ui.status(f"Waiting for you to {verb} {label} in Firefox...")
        self.ui.start_wait(f"Please {verb} {label}",
                           f"{reason}\n\nPlease {verb} '{label}' in Firefox.\n"
                           "The tool continues by itself as soon as it is done.")
        try:
            while True:
                choice = "stop" if self.ui.stop_requested() else self.ui.wait_choice()
                if choice == "stop":
                    raise Cancelled()
                if choice == "skip":
                    return
                try:
                    if kind == "select":
                        done = bool(selected_text(self.page, loc))
                    elif kind == "check":
                        done = loc.is_checked()
                    else:
                        done = bool(loc.input_value().strip())
                except Exception:
                    done = False
                if done:
                    self.ui.status(f"{label}: done - continuing.")
                    settle(self.page, 3000)
                    return
                self.page.wait_for_timeout(1000)
        finally:
            self.ui.end_wait()

    def check(self, step):
        """step["check"] is a selector, or "first" for the first checkbox after the section heading.
        The site draws its own checkboxes over hidden real ones, so fall back to the label, then to JS."""
        if step["check"] != "first":
            loc = self.page.locator(step["check"]).first
        else:
            loc = self.page.locator(f"xpath=({_heading(self.section)}/following::input[@type='checkbox'])[1]")
        loc.wait_for(state="attached")
        if not loc.is_checked():
            try:
                loc.check(timeout=5000)
            except Exception:
                label = loc.locator("xpath=following-sibling::label[1] | parent::label")
                if label.count():
                    label.first.click(timeout=5000)
            if not loc.is_checked():
                loc.evaluate("""el => { el.checked = true;
                    el.dispatchEvent(new Event('click', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true})); }""")
        settle(self.page, 3000)

    def click(self, step):
        button = self.page.locator(step["click"]).filter(visible=True).first
        if step.get("optional"):
            try:
                button.wait_for(state="visible", timeout=4000)
            except Exception:
                raise _Missing()
        button.click()
        settle(self.page, step.get("settle_ms", 5000))

    def otp(self, step):
        loc = self.locate(step, "input")
        code = _ask_until(self.ui, "OTP", step["prompt"])
        loc.fill(code)

    def upload(self, step):
        out_dir = BASE_DIR / "sessions" / "uploads"
        inputs = self.page.locator(
            f"xpath={_heading(self.section)}/following::input[@type='file']")
        ids = step.get("sel") or []
        for index, doc in enumerate(step["upload"]):
            path = self.session.docs.get(doc)
            if not path:
                self.summary.append(f"Upload {doc}: (no file)")
                continue
            prepared = prepare_upload(path, out_dir, max_kb=self.cfg.get("max_upload_kb", 490),
                                      allow_pdf=doc != "app_photo")
            target = self.page.locator(ids[index]).first if index < len(ids) else inputs.nth(index)
            target.set_input_files(prepared)
            self.summary.append(f"Upload {doc}: {Path(prepared).name}")

    # --- flow ---------------------------------------------------------------
    def one_time_registration(self):
        c, f, page, ui = self.cfg["otr"], self.fields, self.page, self.ui
        ui.status("Opening One Time Registration...")
        _visible(page, c["open_link"]).click()
        _visible(page, c["declaration"]).check()
        _visible(page, c["terms_proceed"]).click()
        _visible(page, c["first_name"]).fill(f["app_first_name"])
        _visible(page, c["surname"]).fill(f["app_surname"])
        choose(page, _visible(page, c["gender"]), f["app_sex"], self.dropdown_timeout)
        _visible(page, c["mobile"]).fill(f["app_mobile"])
        _visible(page, c["registered_no"]).check()
        before = len(self.messages)
        _visible(page, c["proceed"]).click()
        try:
            _visible(page, c["otp"]).wait_for(timeout=20000)
        except Exception:
            said = " | ".join(self.messages[before:]) or "no OTP box appeared"
            raise RuntimeError(f"The site did not send a registration OTP: {said}") from None

        prompt = f"Enter the OTP sent to {f['app_mobile']} (One Time Registration)."
        for attempt in range(3):
            code = _ask_until(ui, "Registration OTP", prompt)
            _visible(page, c["otp"]).fill(code)
            _visible(page, c["submit"]).click()
            try:
                page.locator(c["username_text"]).first.wait_for(timeout=20000)
                break
            except Exception:
                prompt = "That OTP was not accepted. Check the site message and enter the OTP again."
        else:
            raise RuntimeError("Registration OTP was not accepted after 3 tries")

        text = page.locator(c["username_text"]).first.inner_text()
        match = re.search(c["username_regex"], text)
        if not match:
            raise RuntimeError(f"Could not read the username from: {text!r}")
        self.session.username = match.group(1)
        save_session(self.session)
        ui.show_username(self.session.username)
        close = page.locator(c["close"]).filter(visible=True)
        if close.count():
            close.first.click()

    def login(self):
        c, page, ui = self.cfg["login"], self.page, self.ui
        ui.status(f"Logging in as {self.session.username}...")
        for attempt in range(3):
            self.check_stop()
            _visible(page, c["username"]).fill(self.session.username)
            _visible(page, c["request_otp"]).click()
            settle(page, 3000)
            code = _ask_until(ui, "Login OTP", f"Enter the login OTP sent to the registered mobile "
                                               f"for {self.session.username}.")
            _visible(page, c["otp"]).fill(code)
            captcha = _visible(page, c["captcha_image"]).screenshot()
            text = _ask_until(ui, "Captcha", "Type the numbers shown in the picture.", captcha)
            _visible(page, c["captcha_input"]).fill(text)
            _visible(page, c["sign_in"]).click()
            try:
                page.locator(c["logged_in_marker"]).first.wait_for(timeout=30000)
                return
            except Exception:
                ui.warn("Login failed", "The site did not log in (wrong OTP or captcha?). Let's try again.")
                refresh = page.locator(c["captcha_refresh"]).filter(visible=True)
                if refresh.count():
                    refresh.first.click()
        raise RuntimeError("Login failed after 3 tries")

    def application_form(self, refilling=False):
        handlers = {"fill": self.fill, "ask_fill": self.ask_fill, "select": self.select, "check": self.check,
                    "click": self.click, "otp": self.otp, "upload": self.upload}
        skipping = False
        for index, step in enumerate(self.cfg["form"]):
            self.check_stop()
            if index in self.done_once:
                continue
            if "section" in step:
                self.section = step["section"]
                skipping = refilling and step.get("skip_on_refill", False)
                self.ui.status(f"{self.section}: skipped when filling remaining fields." if skipping
                               else f"Filling {self.section}...")
                continue
            if skipping:
                continue
            if "skip_section_if_shown" in step:
                marker = field_locator(self.page, {"label": step["skip_section_if_shown"]}, self.section, "input")
                try:
                    marker.wait_for(state="visible", timeout=3000)
                    skipping = True
                    self.ui.status(f"{self.section}: already done on the site ({step['skip_section_if_shown']} "
                                   "is shown) - skipping.")
                except Exception:
                    pass
                continue
            if step.get("confirm"):
                self.confirm()
                continue
            action = next(a for a in handlers if a in step)
            if action in ("fill", "ask_fill", "select"):
                step = {**step, "key": step[action]}
            while True:
                try:
                    handlers[action](step)
                    if step.get("once") or action == "upload":
                        self.done_once.add(index)   # never add a member/nominee/establishment twice
                    break
                except _Missing:
                    break
                except Cancelled:
                    raise
                except Exception as exc:
                    if not self.recover(action, step, exc):
                        break

    def watchable(self, action, step):
        """(locator, kind) for a failed field the user can do by hand and the tool can watch, else None."""
        try:
            if action == "check":
                loc = self.page.locator(step["check"]).first if step["check"] != "first" else None
                kind = "check"
            elif action in ("fill", "ask_fill", "otp", "select"):
                kind = "select" if action == "select" else "input"
                loc = field_locator(self.page, step, self.section, kind)
                if step.get("sel") and step.get("label"):
                    by_label = field_locator(self.page, {"label": step["label"]}, self.section, kind)
                    loc = self.page.locator(step["sel"]).or_(by_label).filter(visible=True).first
            else:
                return None
            if loc is None:
                return None
            loc.wait_for(state="attached", timeout=2000)
            return loc, kind
        except Exception:
            return None

    def recover(self, action, step, exc):
        """A step failed: let the user do it in Firefox and carry on. Returns True to try the step again."""
        what = {"click": f"click '{step.get('click')}'", "check": "tick the checkbox",
                "upload": "attach the documents", "otp": f"enter the OTP in '{step.get('label')}'"}.get(
            action, f"fill '{self.name(step)}'")
        where = f" in the '{self.section}' section" if self.section else ""
        saved = save_debug(self.page)
        watch = self.watchable(action, step)
        if watch:
            loc, kind = watch
            self.wait_for_user(self.name(step) if action != "check" else "the checkbox",
                               f"The tool could not {what}{where}.", loc, kind)
            return False
        self.manual.append(f"{self.name(step) if action in ('fill', 'ask_fill', 'select') else what} (by hand)")
        self.ui.status(f"Could not {what}{where}. Waiting for you...")
        choice = self.ui.ask_choice(
            "Please help with one field",
            f"The tool could not {what}{where}.\n({str(exc).splitlines()[0][:200]})\n\n"
            "Do it yourself in Firefox, then choose:\n\n"
            "Yes = I have done it, continue with the next field\n"
            "No = try this field again\n"
            "Cancel = stop the run here"
            + (f"\n\n(Details saved for support: logs/{saved})" if saved else ""))
        if choice is None:
            raise Cancelled()
        if choice is False:
            self.manual.pop()
            return True
        return False

    def check_stop(self):
        """The "Stop run" button on the status panel: stop before the next field."""
        if self.ui.stop_requested():
            raise Cancelled()

    def refill(self):
        """Go through the open application page again, filling only what is still empty."""
        self.section = None
        self.ui.status("Filling the remaining fields from your documents...")
        self.application_form(refilling=True)
        self.ui.status("Remaining fields filled. Check the page, then finish in Firefox.")

    def confirm(self):
        if self.mismatches:
            self.ui.warn("Prefilled data mismatch",
                         "These fields were already filled on the site and differ from your documents. "
                         "They were NOT changed:\n\n" +
                         "\n".join(f"- {k}: site='{cur}'  documents='{doc}'" for k, cur, doc in self.mismatches))
        lines = [f"Username: {self.session.username}", ""] + self.summary[-40:] + ["", "Upload the documents now?"]
        if not self.ui.ask("Confirm details", "\n".join(lines)):
            raise Cancelled()


def run_registration(config, session, ui):
    result = {"status": "started", "username": "", "filled": [], "mismatches": [], "skipped": [],
              "site_messages": [], "error": ""}
    with sync_playwright() as p:
        try:
            context = launch_browser(p, config, ui)
        except Cancelled:
            result["status"] = "cancelled_by_user"
            return result
        page = context.pages[0] if context.pages else context.new_page()

        def on_dialog(dialog):
            message = dialog.message
            dialog.accept()
            result["site_messages"].append(message)
            ui.status(f"Site message: {message}")

        context.on("dialog", on_dialog)
        run = RegistrationRun(config, session, ui, page, result["site_messages"])
        try:
            page.goto(config["login_url"], wait_until="domcontentloaded")
            if not session.username:
                run.one_time_registration()
            result["username"] = session.username
            run.login()
            run.application_form()
            result["status"] = "documents_uploaded"
            ui.status("Done up to Upload Documents. In Firefox: check the page, accept the terms, choose the "
                      "payment mode, tick the declarations and click SUBMIT yourself. Close Firefox when finished.")
        except Cancelled:
            result["status"] = "cancelled_by_user"
            ui.status("Stopped. Finish in Firefox if you want, then close the browser window.")
        except Exception as exc:
            result["status"] = "error"
            result["error"] = f"{type(exc).__name__}: {exc}".splitlines()[0][:500]
            saved = save_debug(page)
            if saved:
                result["error"] += f" (screenshot: {saved})"
            ui.warn("Automation error", result["error"] + "\n\nThe browser stays open so you can continue manually.")
            ui.status("Error - see popup. Close the browser window when done.")
        result["username"] = session.username
        result["filled"] = [(line, "") for line in run.summary]
        result["mismatches"] = run.mismatches
        result["skipped"] = run.manual
        wait_for_close_or_refill(context, page, run, ui)
    return result
