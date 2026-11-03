"""Playwright (Firefox, persistent profile) flow for Pay Annual Contribution.

Runs in a worker thread. All user interaction goes through ``ui`` (ui_common.UIBridge): status,
warn, ask, and the Stop run / Fill remaining fields buttons; it marshals the calls onto Tk.
"""
import re
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright
from rapidfuzz import fuzz, process, utils

from extractors import FIELDS

BASE_DIR = Path(__file__).resolve().parent
PLACEHOLDER_OPTION = re.compile(r"^\s*(-+\s*)?(select|choose|--)", re.I)


class Cancelled(Exception):
    pass


def _norm(text):
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def _digits(text):
    return re.sub(r"\D", "", str(text or ""))


def _parse_date(text):
    text = str(text or "").strip()
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%d-%m-%y", "%d/%m/%y", "%d.%m.%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    return None


def _transform(value, kind, date_format):
    if not value:
        return ""
    if kind == "digits":
        return _digits(value)
    if kind == "date":
        parsed = _parse_date(value)
        return parsed.strftime(date_format) if parsed else value
    if kind == "address":
        return re.sub(r"\s+", " ", value.replace("-", " ")).strip()
    return value


def _same(current, desired, kind):
    if kind == "digits":
        return _digits(current) == _digits(desired)
    if kind == "date":
        a, b = _parse_date(current), _parse_date(desired)
        if a and b:
            return a == b
    return _norm(current) == _norm(desired)


def _locate(page, target):
    """Accept either a selector string or an existing Locator."""
    return page.locator(target).first if isinstance(target, str) else target


def _options(locator):
    return locator.evaluate(
        "el => Array.from(el.options).map(o => ({value: o.value, text: o.textContent.trim()}))"
    )


def _real_options(locator):
    return [o for o in _options(locator)
            if o["value"] not in ("", "0", "-1") and not PLACEHOLDER_OPTION.match(o["text"])]


def _find_option(options, wanted):
    wanted_n = _norm(wanted)
    for o in options:
        if _norm(o["text"]) == wanted_n:
            return o
    for o in options:
        if wanted_n in _norm(o["text"]):
            return o
    return None


def choose(page, selector, wanted, timeout_ms):
    """Select `wanted` in a <select> (waiting for cascading options), or click a radio/checkbox/button."""
    loc = _locate(page, selector)
    loc.wait_for(state="visible")
    tag = loc.evaluate("el => el.tagName.toLowerCase()")
    if tag != "select":
        loc.click()
        return wanted
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        option = _find_option(_real_options(loc), wanted)
        if option:
            loc.select_option(value=option["value"])
            return option["text"]
        if time.monotonic() > deadline:
            raise RuntimeError(f"Option '{wanted}' not found in {selector}")
        page.wait_for_timeout(300)


def choose_fuzzy(page, selector, wanted, threshold, timeout_ms, scorer=fuzz.QRatio):
    """Pick the closest option to `wanted`. Returns (option_text, score); option_text is None below threshold."""
    loc = _locate(page, selector)
    loc.wait_for(state="visible")
    deadline = time.monotonic() + timeout_ms / 1000
    options = _real_options(loc)
    while not options and time.monotonic() < deadline:
        page.wait_for_timeout(300)
        options = _real_options(loc)
    if not options:
        raise RuntimeError(f"No options loaded in {selector}")
    while True:
        text, score, index = process.extractOne(wanted, [o["text"] for o in options],
                                                scorer=scorer, processor=utils.default_process)
        if score >= threshold:
            break
        # The list may still hold the options of the previous Block/District - wait for the reload.
        if time.monotonic() > deadline:
            return None, score, text
        page.wait_for_timeout(500)
        options = _real_options(loc) or options
    loc.select_option(value=options[index]["value"])
    return text, score, text


def fill_text_field(page, spec, fields, date_format):
    """Fill an empty input from the session. Returns (outcome, site_value, doc_value); outcome is filled/mismatch/kept/blank."""
    kind = spec.get("transform")
    desired = _transform(fields.get(spec["field"], ""), kind, date_format)
    loc = page.locator(spec["selector"]).first
    loc.wait_for(state="visible")
    current = loc.input_value()
    if current.strip():
        if desired and not _same(current, desired, kind):
            return "mismatch", current, desired
        return "kept", current, desired
    if not desired:
        return "blank", "", ""
    loc.fill(desired)
    return "filled", "", desired


def selected_text(page, selector):
    """Text of the chosen option, or "" when a placeholder is selected."""
    loc = _locate(page, selector)
    loc.wait_for(state="visible")
    option = loc.evaluate("el => el.selectedIndex < 0 ? null : {value: el.value, text: el.options[el.selectedIndex].text.trim()}")
    if not option or option["value"] in ("", "0", "-1") or PLACEHOLDER_OPTION.match(option["text"]):
        return ""
    return option["text"]


def settle(page, timeout_ms):
    """Give AJAX calls a chance to finish; some pages never report idle, so this never fails."""
    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except Exception:
        pass


def _profile_processes(profile):
    """PIDs of Firefox processes started by this tool with its profile (never the user's own Firefox)."""
    script = ("Get-CimInstance Win32_Process -Filter \"Name='firefox.exe'\" | "
              "Where-Object { $_.CommandLine -like '*-profile*' } | "
              "ForEach-Object { '' + $_.ProcessId + '|' + $_.CommandLine }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True,
                             text=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except Exception:
        return []
    target = str(profile).lower()
    return [line.split("|", 1)[0] for line in out.splitlines()
            if "|" in line and target in line.lower()]


def launch_browser(p, config, ui):
    """Open the tool's Firefox. A Firefox left over from an earlier run locks the profile, so offer to close it."""
    profile = BASE_DIR / config.get("browser_profile_dir", "profile/firefox")
    profile.mkdir(parents=True, exist_ok=True)
    leftover = _profile_processes(profile)
    if leftover:
        if not ui.ask("Firefox already open",
                      "A Firefox window from an earlier run of this tool is still open, so a new one cannot "
                      "start.\n\nClose that Firefox window and continue?\n\n"
                      "(Only the tool's own Firefox is closed - your normal Firefox is not touched.)"):
            raise Cancelled()
        for pid in leftover:
            subprocess.run(["taskkill", "/PID", pid, "/T", "/F"], capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        time.sleep(2)
    ui.status("Launching Firefox...")
    try:
        context = p.firefox.launch_persistent_context(
            str(profile),
            headless=config.get("headless", False),
            slow_mo=config.get("slow_mo_ms", 0),
            no_viewport=True,
            accept_downloads=True,
        )
    except Exception as exc:
        raise RuntimeError("Firefox could not start. Close any Firefox window opened by this tool and try "
                           f"again. ({str(exc).splitlines()[0][:150]})") from None
    context.set_default_timeout(config.get("default_timeout_ms", 30000))
    context.on("page", lambda pg: pg.on("download", lambda d: save_download(d, ui)))
    for pg in context.pages:
        pg.on("download", lambda d: save_download(d, ui))
    return context


def save_download(download, ui):
    """Firefox under Playwright hands downloads (e.g. the money receipt) to the tool instead of showing them,
    and the tab that started it closes. Save the file in downloads/ and open it."""
    try:
        folder = BASE_DIR / "downloads"
        folder.mkdir(exist_ok=True)
        name = download.suggested_filename or "download"
        target = folder / f"{time.strftime('%Y%m%d_%H%M%S')}_{name}"
        download.save_as(str(target))
        ui.status(f"Downloaded {name} - saved to downloads/{target.name} and opened.")
        os.startfile(str(target))
    except Exception as exc:
        ui.status(f"Could not save the download: {str(exc).splitlines()[0][:200]}")


def save_debug(page):
    """Screenshot plus page HTML in logs/, for diagnosing a failed step. Returns the screenshot name."""
    shot = BASE_DIR / "logs" / f"error_{datetime.now():%Y%m%d_%H%M%S}.png"
    try:
        shot.parent.mkdir(exist_ok=True)
        page.screenshot(path=str(shot), full_page=True)
        shot.with_suffix(".html").write_text(page.content(), encoding="utf-8")
        return shot.name
    except Exception:
        return ""


def wait_for_close_or_refill(context, page, run, ui):
    """Keep the browser open for the user; whenever they press "Fill remaining fields", fill again."""
    closed = []
    context.on("close", lambda _: closed.append(True))
    ui.set_refill_available(True)
    try:
        while not closed and not page.is_closed():
            if ui.take_refill_request():
                ui.clear_stop()
                ui.set_refill_available(False)
                try:
                    run.refill()
                except Cancelled:
                    ui.status("Stopped filling. You can press 'Fill remaining fields' again.")
                except Exception as exc:
                    if page.is_closed():
                        break
                    saved = save_debug(page)
                    ui.warn("Could not fill", f"{type(exc).__name__}: {str(exc).splitlines()[0][:300]}"
                                              + (f"\n\n(Details saved: logs/{saved})" if saved else ""))
                ui.set_refill_available(True)
            page.wait_for_timeout(500)
    except Exception:
        pass   # the browser was closed
    ui.set_refill_available(False)


class AnnualRun:
    """The establishment-details part of Pay Annual Contribution, repeatable via "Fill remaining fields"."""

    def __init__(self, config, session, ui, page, result):
        self.config, self.session, self.ui, self.page, self.result = config, session, ui, page, result
        self.sel = config["selectors"]
        self.dropdown_timeout = config.get("dropdown_load_timeout_ms", 20000)
        self.field_timeout = config.get("field_timeout_ms", 5000)
        self.date_format = config.get("date_format", "%Y-%m-%d")
        self.add_details_clicked = False
        self.summary = []

    def check_stop(self):
        if self.ui.stop_requested():
            raise Cancelled()

    def fill_details(self):
        """Fill every establishment field; a field that is missing, slow or fails is noted and skipped."""
        page, ui, result, fields = self.page, self.ui, self.result, self.session.fields
        self.summary = []
        result["mismatches"] = []
        page.set_default_timeout(self.field_timeout)
        try:
            for step in self.config["details_form"]:
                self.check_stop()
                label = (step.get("label") or FIELDS.get(step.get("field"), step.get("field"))
                         or step.get("selector"))
                try:
                    self.fill_step(step, label, fields)
                except Cancelled:
                    raise
                except Exception as exc:
                    reason = str(exc).splitlines()[0][:120]
                    result["skipped"].append(f"{label} (not filled)")
                    self.summary.append(f"{label}: (not filled - {reason})")
                    ui.status(f"{label}: not filled ({reason}) - continuing.")
        finally:
            page.set_default_timeout(self.config.get("default_timeout_ms", 30000))

    def fill_step(self, step, label, fields):
        page, ui, result = self.page, self.ui, self.result
        if "field" in step:
            ui.status(f"Checking {label}...")
            outcome, site_value, doc_value = fill_text_field(page, step, fields, self.date_format)
            if outcome == "filled":
                result["filled"].append((label, doc_value))
                self.summary.append(f"{label}: {doc_value}")
            elif outcome == "mismatch":
                result["mismatches"].append((label, site_value, doc_value))
                self.summary.append(f"{label}: {site_value}  (site value kept, document says '{doc_value}')")
            elif outcome == "kept":
                self.summary.append(f"{label}: {site_value}  (already on site)")
            else:
                result["skipped"].append(label)
                self.summary.append(f"{label}: (left blank)")
        elif "select_if_empty" in step:
            wanted = step["select_if_empty"]
            current = selected_text(page, step["selector"])
            if not current:
                ui.status(f"Selecting {label}: {wanted}...")
                self.summary.append(f"{label}: {choose(page, step['selector'], wanted, self.dropdown_timeout)}")
            elif _norm(current) != _norm(wanted):
                result["mismatches"].append((label, current, wanted))
                self.summary.append(f"{label}: {current}  (site value kept, expected '{wanted}')")
            else:
                self.summary.append(f"{label}: {current}  (already on site)")
        elif "select" in step:
            ui.status(f"Selecting {label}: {step['select']}...")
            self.summary.append(f"{label}: {choose(page, step['selector'], step['select'], self.dropdown_timeout)}")
        elif "fuzzy_select" in step:
            keys = step["fuzzy_select"] if isinstance(step["fuzzy_select"], list) else [step["fuzzy_select"]]
            wanted = next((fields[k] for k in keys if fields.get(k)), "") or step.get("fallback", "")
            if not wanted:
                result["skipped"].append(keys[0])
                self.summary.append(f"{label}: (left blank - not in document)")
                return
            current = selected_text(page, step["selector"])
            if current and (not step.get("replace") or _norm(current) == _norm(wanted)):
                self.summary.append(f"{label}: {current}  (already on site)")
                return
            ui.status(f"Matching {label} '{wanted}'...")
            matched, score, best = choose_fuzzy(page, step["selector"], wanted,
                                                self.config.get("village_match_threshold", 80),
                                                self.dropdown_timeout)
            if "village" in keys:
                result["village_match"], result["village_score"] = matched or "", round(score)
            if matched:
                self.summary.append(f"{label}: {matched}  [{score:.0f}% match for '{wanted}']")
            else:
                result["skipped"].append(keys[0])
                self.summary.append(f"{label}: (NOT selected - best was '{best}' at {score:.0f}%)")
                ui.status(f"{label}: no option close to '{wanted}' (best '{best}', {score:.0f}%) - "
                          "please choose it in Firefox.")

    def finish(self):
        """Show mismatches, confirm, then click Add Details (only once)."""
        if self.add_details_clicked:
            self.ui.status("Add Details was already clicked - check the page and finish in Firefox.")
            return
        result = self.result
        if result["mismatches"]:
            self.ui.warn("Prefilled data mismatch",
                         "These fields were already filled on the site and differ from the document. "
                         "They were NOT changed:\n\n" +
                         "\n".join(f"- {k}: site='{cur}'  document='{doc}'" for k, cur, doc in result["mismatches"]))
        confirm = ([f"Registration No: {self.session.registration_number}", ""] + self.summary
                   + ["", "Click Add Details now?"])
        if not self.ui.ask("Confirm details", "\n".join(confirm)):
            raise Cancelled()
        self.check_stop()
        messages_before = len(result["site_messages"])
        self.page.locator(self.sel["add_details_button"]).first.click()
        self.add_details_clicked = True
        settle(self.page, self.config.get("settle_timeout_ms", 10000))
        result["status"] = "add_details_clicked"
        new_messages = result["site_messages"][messages_before:]
        self.ui.status("Add Details clicked"
                       + (f" - site said: {' | '.join(new_messages)}" if new_messages else "")
                       + ". Check the page, finish in Firefox, then close the browser window.")

    def refill(self):
        self.ui.status("Filling the remaining fields from your document...")
        self.fill_details()
        self.finish()


def run_annual_contribution(config, session, ui):
    sel = config["selectors"]
    settle_ms = config.get("settle_timeout_ms", 10000)
    result = {"status": "started", "village_match": "", "village_score": "",
              "filled": [], "mismatches": [], "skipped": [], "site_messages": [], "error": ""}

    with sync_playwright() as p:
        try:
            context = launch_browser(p, config, ui)
        except Cancelled:
            result["status"] = "cancelled_by_user"
            return result
        page = context.pages[0] if context.pages else context.new_page()

        # The site reports results with alert(); Playwright would otherwise dismiss them silently.
        def on_dialog(dialog):
            message = dialog.message
            dialog.accept()
            result["site_messages"].append(message)
            ui.status(f"Site message: {message}")

        context.on("dialog", on_dialog)
        run = AnnualRun(config, session, ui, page, result)
        try:
            ui.status("Opening construction worker login page...")
            page.goto(config["login_url"], wait_until="domcontentloaded")

            run.check_stop()
            ui.status("Clicking Pay Annual Contribution...")
            page.locator(sel["login_pay_annual_contribution"]).first.click()

            run.check_stop()
            ui.status(f"Entering registration number {session.registration_number}...")
            page.locator(sel["registration_number_input"]).first.fill(session.registration_number)
            page.locator(sel["proceed_button"]).first.click()

            run.check_stop()
            ui.status("Clicking Pay Annual Contribution on applicant details...")
            page.locator(sel["pay_annual_contribution_button"]).first.click()
            page.wait_for_load_state("domcontentloaded")

            run.check_stop()
            ui.status("Uploading scanned document...")
            page.locator(sel["document_upload_input"]).first.set_input_files(session.document_path)
            page.locator(sel["upload_documents_button"]).first.click()
            settle(page, settle_ms)

            run.fill_details()
            run.finish()
        except Cancelled:
            result["status"] = "cancelled_by_user"
            ui.status("Stopped. Press 'Fill remaining fields' to continue filling, or finish in Firefox.")
        except Exception as exc:
            result["status"] = "error"
            result["error"] = f"{type(exc).__name__}: {exc}".splitlines()[0][:500]
            saved = save_debug(page)
            if saved:
                result["error"] += f" (screenshot: {saved})"
            ui.warn("Automation error", result["error"] + "\n\nThe browser stays open. You can press "
                                                           "'Fill remaining fields' or continue manually.")
            ui.status("Error - see popup. Close the browser window when done.")

        wait_for_close_or_refill(context, page, run, ui)
    return result
