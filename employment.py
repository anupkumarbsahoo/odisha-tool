"""Playwright (Firefox) flow for Employment Registration on the State Employment Service portal
(empmission.odisha.gov.in, "JobSeeker Registration").

Runs in a worker thread with the same ``ui`` bridge as the other flows. The portal is a set of
pages (Select Exchange, Personal Details, Educational Qualification, Experience, Languages,
Additional Information, Willingness); the tool recognises the page it is on, fills it, presses
Next and carries on, so "Fill remaining fields" simply resumes from whatever page is open.
Controls are found by the label text next to them (config.json "employment"), because the pages
have few ids. It stops on the Willingness page: the declaration and Submit are left to the user.
"""
import json
import re
from dataclasses import asdict
from pathlib import Path

from playwright.sync_api import sync_playwright
from rapidfuzz import fuzz

from automation import (BASE_DIR, Cancelled, _parse_date, _real_options, choose, choose_fuzzy, launch_browser, save_debug,
                        selected_text, settle, wait_for_close_or_refill)
from docprep import prepare_upload
from registration import RegistrationRun, _Missing, _ask_until
from runlog import log_run

def names_ratio(wanted, option, **kwargs):
    """Best match of `wanted` against any run of words inside an option, for options that list many
    names in one ("Kurmi/ Kuruma Chasa/ Kudumi/ ..."): KUDMI -> Kudumi, not Kudiya."""
    words = option.split()
    n = max(1, len(wanted.split()))
    return max(fuzz.QRatio(wanted, " ".join(words[i:i + n])) for i in range(max(1, len(words) - n + 1)))


SCORERS = {"partial": fuzz.partial_ratio, "token": fuzz.token_set_ratio, "names": names_ratio}
LEVEL_ORDER = ["10TH PASS", "12TH PASS", "ITI CERTIFICATE HOLDER", "DIPLOMA HOLDER", "GRADUATE",
               "GRADUATE(TECH.)", "HONOURS GRADUATE", "POST GRADUATE", "DOCTORATE"]

# Marks the n-th visible control after the first visible text that starts with `label` (searching
# from the first visible `after` heading) and returns True; the caller then uses the mark.
_MARK_JS = """([label, kind, after, nth, mark]) => {
  const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
  const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length)
                   && getComputedStyle(e).visibility !== 'hidden';
  const ownText = (e, t) => !['OPTION', 'SELECT', 'SCRIPT', 'STYLE', 'TEXTAREA'].includes(e.tagName)
                   && [...e.childNodes].some(n => n.nodeType === 3 && norm(n.textContent).startsWith(t));
  const textTypes = ['text', 'email', 'tel', 'number', 'password', ''];
  const ok = e => {
    if (kind === 'select') return e.tagName === 'SELECT';
    if (kind === 'checkbox') return e.tagName === 'INPUT' && e.type === 'checkbox';
    if (kind === 'radio') return e.tagName === 'INPUT' && e.type === 'radio';
    if (kind === 'file') return e.tagName === 'INPUT' && e.type === 'file';
    if (kind.startsWith('button:')) {
      const want = norm(kind.slice(7));
      return (e.tagName === 'INPUT' && ['button', 'submit'].includes(e.type) && norm(e.value) === want)
             || (e.tagName === 'BUTTON' && norm(e.textContent) === want);
    }
    return e.tagName === 'TEXTAREA' || (e.tagName === 'INPUT' && textTypes.includes(e.type));
  };
  document.querySelectorAll('[data-tool-loc]').forEach(e => e.removeAttribute('data-tool-loc'));
  // label texts are matched as text pieces, so "Read", "Write" and "Speak" in one cell are told apart
  const texts = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    if (ownText(n.parentElement, '') && vis(n.parentElement) && norm(n.textContent)) texts.push(n);
  }
  const follows = (a, b) => !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
  let from = null;
  if (after) {
    from = texts.find(n => norm(n.textContent).startsWith(norm(after)));
    if (!from) return false;
  }
  const at = texts.find(n => (!from || follows(from, n)) && norm(n.textContent).startsWith(norm(label)));
  if (!at) return false;
  if (kind === 'text') {   // the text shown next to the label, e.g. the Registration No. value
    const value = texts.find(n => follows(at, n) && n.parentElement !== at.parentElement);
    if (!value) return false;
    value.parentElement.setAttribute('data-tool-loc', mark);
    return true;
  }
  let count = 0;
  for (const e of document.body.querySelectorAll('input, select, textarea, button')) {
    if (follows(at, e) && ok(e) && (vis(e) || kind === 'file') && ++count === nth) {
      e.setAttribute('data-tool-loc', mark);
      return true;
    }
  }
  return false;
}"""


def save_emp_session(session):
    """Keep the documents, fields and registration number per Aadhaar so nobody is registered twice."""
    folder = BASE_DIR / "sessions"
    folder.mkdir(exist_ok=True)
    (folder / f"emp_{session.fields.get('aadhaar') or 'unknown'}.json").write_text(
        json.dumps(asdict(session), indent=2, ensure_ascii=False), encoding="utf-8")


def load_saved_reg_no(aadhaar):
    try:
        data = json.loads((BASE_DIR / "sessions" / f"emp_{aadhaar}.json").read_text(encoding="utf-8"))
        return data.get("reg_no", "")
    except (OSError, ValueError):
        return ""


def known_options(name, group, seeds=()):
    """Dropdown values the tool has seen on the site before (e.g. the sub-castes of OBC), so the
    details screen can offer them; the site only shows them after the captcha."""
    try:
        saved = json.loads((BASE_DIR / f"{name}.json").read_text(encoding="utf-8")).get(group, [])
    except (OSError, ValueError):
        saved = []
    return list(dict.fromkeys([*saved, *seeds]))


def remember_options(name, group, loc):
    try:
        options = [o["text"] for o in _real_options(loc)]
        path = BASE_DIR / f"{name}.json"
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if options and group and data.get(group) != options:
            data[group] = options
            path.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass   # only a convenience for the next run


def _compact(text):
    return re.sub(r"[^A-Z0-9]", "", str(text or "").upper())


def _height(text):
    """'5.4' / 5'4" -> (feet, inches, cm) as strings; a plain number over 100 is taken as cm."""
    numbers = re.findall(r"\d+(?:\.\d+)?", str(text or ""))
    if not numbers:
        return "", "", ""
    if float(numbers[0]) > 100:
        return "", "", str(round(float(numbers[0])))
    parts = re.split(r"[.'\s\"]+", str(text).strip())
    feet = parts[0] if parts and parts[0].isdigit() else str(int(float(numbers[0])))
    inches = parts[1] if len(parts) > 1 and parts[1].isdigit() else "0"
    return feet, inches, str(round((int(feet) * 12 + int(inches)) * 2.54))


class EmploymentRun(RegistrationRun):
    """Reuses the One Time Registration step engine (fill only empty fields, wait for the user on a
    problem, Stop run); controls are found by label instead of by id."""

    def __init__(self, config, session, ui, page, messages):
        super().__init__(config, session, ui, page, messages)
        self.cfg = config["employment"]
        self.pages = self.cfg["pages"]
        self.marks = 0
        self.qual = {}

    # --- locating -----------------------------------------------------------
    def mark(self, label, kind, after=None, nth=1):
        """Locator for the control after a label, or None when it is not on the page."""
        self.marks += 1
        tag = f"t{self.marks}"
        if not self.page.evaluate(_MARK_JS, [label, kind, after or "", nth, tag]):
            return None
        return self.page.locator(f"[data-tool-loc='{tag}']")

    def locate(self, step, tag):
        kind = {"input": "input", "select": "select"}.get(tag, tag)
        if step.get("sel"):
            loc = self.page.locator(step["sel"]).filter(visible=True).first
            try:
                loc.wait_for(state="visible", timeout=4000 if step.get("optional") else 15000)
                return loc
            except Exception:
                if step.get("optional"):
                    raise _Missing()
                raise
        for attempt in range(20 if not step.get("optional") else 4):
            loc = self.mark(step["label"], kind, step.get("after"), step.get("nth", 1))
            if loc is not None:
                return loc
            self.page.wait_for_timeout(500)
        if step.get("optional"):
            raise _Missing()
        raise RuntimeError(f"'{step['label']}' is not on the page")

    def watchable(self, action, step):
        kind = {"select": "select", "check": "checkbox"}.get(action, "input")
        try:
            loc = (self.page.locator(step["sel"]).filter(visible=True).first if step.get("sel")
                   else self.mark(step["label"], kind, step.get("after"), step.get("nth", 1)))
            if loc is None:
                return None
            loc.wait_for(state="attached", timeout=2000)
            return loc, {"checkbox": "check"}.get(kind, kind)
        except Exception:
            return None

    def value(self, step):
        key = step["key"]
        if key.startswith("="):
            return key[1:]
        if key.startswith("q."):
            return str(self.qual.get(key[2:], "") or "")
        return str(self.fields.get(key, "") or "")

    # --- step handlers ------------------------------------------------------
    def select(self, step):
        """Like the registration select, but 'replace' re-selects a pre-chosen default (Gender is
        "Male", Marital Status "UNMARRIED" ... until changed)."""
        label = self.name(step)
        loc = self.locate(step, "select")
        if step.get("remember_options"):
            remember_options(step["remember_options"], self.fields.get("caste", ""), loc)
        wanted = self.value(step)
        current = selected_text(self.page, loc)
        if current and (not step.get("replace") or not wanted or _compact(current) == _compact(wanted)):
            return
        if not wanted:
            if step.get("optional"):
                return
            self.wait_for_user(label, f"There is no value for {label} in the documents.", loc)
            return
        if step.get("fuzzy"):
            scorer = SCORERS.get(step.get("scorer"), fuzz.QRatio)
            matched, score, best = choose_fuzzy(self.page, loc, wanted, step.get("threshold", self.threshold),
                                                self.dropdown_timeout, scorer)
            if not matched:
                if step.get("confirm"):
                    self.confirm_choice(label, wanted, loc)
                    return
                if step.get("optional"):
                    return
                self.wait_for_user(label, f"No option close enough to '{wanted}' (best: '{best}', {score:.0f}%).",
                                   loc)
                return
            self.summary.append(f"{label}: {matched}")
            if step.get("confirm"):
                settle(self.page, 2000)
                self.confirm_choice(label, wanted, loc)
                return
        else:
            try:
                self.summary.append(f"{label}: {choose(self.page, loc, wanted, self.dropdown_timeout)}")
            except RuntimeError:
                self.wait_for_user(label, f"'{wanted}' is not one of the options.", loc)
                return
        settle(self.page, 4000)

    def confirm_choice(self, label, wanted, loc):
        """Popup with all the site's options and the tool's pick, so the user can correct it."""
        options = [o["text"] for o in _real_options(loc)]
        current = selected_text(self.page, loc)
        chosen = self.ui.ask_pick(
            f"Check {label}",
            f"The documents say: {wanted or '(nothing)'}\n"
            + (f"The tool selected: {current}\n\n" if current else "No close match was found.\n\n")
            + f"Keep it, or choose the correct {label} below, then press OK - the tool continues with the rest.",
            options, current)
        if chosen is None:
            raise Cancelled()
        if chosen != current:
            chosen = choose(self.page, loc, chosen, self.dropdown_timeout)
            settle(self.page, 3000)
        if current:
            self.summary.pop()   # the line for the tool's own pick
        self.summary.append(f"{label}: {chosen}")
        self.ui.status(f"{label}: {chosen} (checked by you)")

    def date(self, step):
        """Day / month / year boxes after the label."""
        parsed = _parse_date(self.value(step))
        if not parsed:
            self.summary.append(f"{self.name(step)}: (left blank)")
            return
        for nth, part in enumerate((f"{parsed.day:02d}", f"{parsed.month:02d}", str(parsed.year)), start=1):
            box = self.locate({**step, "nth": nth}, "input")
            if box.input_value().strip() in ("", "0", "00", "0000"):
                box.fill(part)
                box.dispatch_event("change")
        self.summary.append(f"{self.name(step)}: {parsed:%d/%m/%Y}")

    def check(self, step):
        loc = (self.page.locator(step["sel"]).first if step.get("sel")
               else self.locate(step, "checkbox"))
        if not loc.is_checked():
            loc.check()
            settle(self.page, 2000)

    def click(self, step):
        if step.get("button"):
            button = self.locate({**step, "label": step["after_label"], "after": step.get("after")},
                                 f"button:{step['button']}")
        else:
            button = self.page.locator(step["click"]).filter(visible=True).first
        button.click()
        settle(self.page, step.get("settle_ms", 4000))

    def tab(self, step):
        self.page.get_by_text(step["tab"], exact=True).filter(visible=True).first.click()
        self.page.wait_for_timeout(800)

    def upload(self, step):
        key = step["upload"]
        path = self.qual.get(key[2:]) if key.startswith("q.") else self.session.docs.get(key)
        if not path:
            self.summary.append(f"{self.name(step)}: (no file)")
            return
        loc = self.locate(step, "file")
        if loc.evaluate("e => e.files.length"):
            return
        out_dir = BASE_DIR / "sessions" / "uploads"
        if step.get("as") == "photo":
            prepared = prepare_upload(path, out_dir, max_kb=self.cfg.get("max_photo_kb", 19), allow_pdf=False,
                                      min_width=60)
        else:
            prepared = prepare_upload(path, out_dir, max_kb=self.cfg.get("max_pdf_kb", 98), pdf_only=True)
        loc.set_input_files(prepared)
        loc.dispatch_event("change")
        self.summary.append(f"{self.name(step)}: {Path(prepared).name}")

    def height(self, step):
        feet, inches, cm = _height(self.fields.get(step["height"], ""))
        if not cm:
            return
        for nth, value in ((step.get("feet_nth", 1), feet), (step.get("inch_nth", 2), inches),
                           (step.get("cm_nth", 3), cm)):
            if not value:
                continue
            box = self.locate({**step, "nth": nth}, "input")
            if not box.input_value().strip():
                box.fill(value)
                box.dispatch_event("change")
        self.summary.append(f"Height: {feet} ft {inches} in / {cm} cm")

    def run_steps(self, steps):
        handlers = {"fill": self.fill, "select": self.select, "date": self.date, "check": self.check,
                    "click": self.click, "tab": self.tab, "upload": self.upload, "height": self.height}
        for step in steps:
            self.check_stop()
            when = step.get("when")
            if when and not (self.qual.get(when[2:]) if when.startswith("q.") else self.fields.get(when)):
                continue
            action = next(a for a in handlers if a in step)
            if action in ("fill", "select", "date"):
                step = {**step, "key": step[action]}
            while True:
                try:
                    handlers[action](step)
                    break
                except _Missing:
                    break
                except Cancelled:
                    raise
                except Exception as exc:
                    if not self.recover(action, step, exc):
                        break

    # --- pages --------------------------------------------------------------
    def detect(self):
        """Name of the portal page that is open (config "page_markers", checked in order)."""
        try:
            text = self.page.locator("body").inner_text(timeout=5000)
        except Exception:
            return None
        flat = re.sub(r"\s+", " ", text).lower()
        for name, marker in self.cfg["page_markers"].items():
            if marker.lower() in flat:
                return name
        return None

    def wait_for_page(self, name, timeout_ms=30000):
        """True once the page is no longer `name`."""
        deadline = timeout_ms // 500
        for _ in range(deadline):
            self.page.wait_for_timeout(500)
            if self.detect() not in (name, None):
                return True
        return self.detect() != name

    def press_next(self, name, selector):
        before = len(self.messages)
        self.page.locator(selector).filter(visible=True).first.click()
        settle(self.page, 5000)
        if self.wait_for_page(name, 20000):
            return True
        said = " | ".join(self.messages[before:])
        self.ui.status(f"The site stayed on the same page{': ' + said if said else ''}.")
        return False

    def captcha(self):
        c = self.cfg["captcha"]
        image = self.page.locator(c["image"]).filter(visible=True).first.screenshot()
        text = _ask_until(self.ui, "Captcha", "Type the characters shown in the picture.", image)
        box = self.page.locator(c["input"]).filter(visible=True)
        if not box.count():
            box = self.page.locator(c["input_after_image"]).filter(visible=True)
        box.first.fill(text)

    def wait_until_moved(self, name, why):
        """The site would not move on (a validation message, or a field the tool could not fill):
        the user corrects it in Firefox and presses the button; the tool continues on the next page."""
        save_debug(self.page)
        self.ui.start_wait("Please continue in Firefox",
                           f"{why}\n\nPlease correct the page in Firefox and press its Next / Submit button.\n"
                           "The tool continues by itself on the next page.")
        try:
            while True:
                choice = "stop" if self.ui.stop_requested() else self.ui.wait_choice()
                if choice == "stop":
                    raise Cancelled()
                if choice == "skip" or self.detect() not in (name, None):
                    return
                self.page.wait_for_timeout(1000)
        finally:
            self.ui.end_wait()

    def page_with_captcha(self, name, steps_key, next_selector):
        for attempt in range(3):
            self.ui.status(f"Filling {self.cfg['page_titles'][name]}...")
            self.run_steps(self.pages[steps_key])
            self.check_stop()
            self.captcha()
            if self.press_next(name, next_selector):
                return
            if self.detect() != name:
                return
        self.wait_until_moved(name, "The site did not accept the page (wrong captcha or a field it rejected).")

    def exchange_page(self):
        self.page_with_captcha("exchange", "exchange", self.cfg["buttons"]["submit"])

    def personal_page(self):
        reg_no = self.session.reg_no
        self.page_with_captcha("personal", "personal", self.cfg["buttons"]["next"])
        if self.session.reg_no == reg_no:
            self.read_reg_no()

    def read_reg_no(self):
        """Once Personal Details is saved the next page shows "Registration No." - pop it up to copy."""
        pattern = self.cfg["reg_no_regex"]
        try:
            cell = self.mark(self.cfg["reg_no_label"], "text")
            text = cell.inner_text(timeout=3000) if cell is not None else ""
            match = re.search(pattern, text) or re.search(
                pattern, self.page.locator("body").inner_text(timeout=5000))
        except Exception:
            match = None
        if match and match.group(1) != self.session.reg_no:
            self.session.reg_no = match.group(1)
            save_emp_session(self.session)
            log_run({"action": "employment_registration", "registration_number": self.session.reg_no,
                     "status": "registration_number_created"})
            self.ui.status(f"Registration No.: {self.session.reg_no} (copied to the clipboard)")
            self.ui.show_username(self.session.reg_no, "The job seeker is registered. Keep this Registration "
                                                       "Number - it is needed to log in and renew.")

    def added_exams(self):
        """Exam names in the table of qualifications already saved on the page."""
        cells = self.page.evaluate("""() => [...document.querySelectorAll('td')]
            .filter(td => !td.querySelector('select, input, td, textarea'))
            .map(td => td.innerText)""")
        return {_compact(c) for c in cells if c.strip()}

    def qualification_page(self):
        """Lower qualifications go in with "Add Qualification". The one at the level chosen on the
        Select Exchange page must be last: for it the site hides that button and its Submit saves it."""
        quals, seen = [], set()
        for qual in self.session.quals:
            key = _compact(qual.get("exam") or qual.get("level"))
            if key and key not in seen:     # e.g. the 10th certificate added again as a degree row
                seen.add(key)
                quals.append(qual)
        if not quals:
            self.wait_until_moved("qualification", "No qualification details were read from the documents.")
            return
        chosen = self.fields.get("qualification", "")
        quals.sort(key=lambda q: (q.get("level") == chosen,
                                  LEVEL_ORDER.index(q["level"]) if q.get("level") in LEVEL_ORDER else 99))
        self.read_reg_no()
        buttons = self.cfg["buttons"]
        for qual in quals:
            self.check_stop()
            if _compact(qual.get("exam_chosen") or qual.get("exam")) in self.added_exams():
                continue
            self.qual = qual
            self.ui.status(f"Adding qualification {qual.get('exam') or qual.get('level')}...")
            self.run_steps(self.pages["qualification"])
            self.qual["exam_chosen"] = selected_text(self.page, self.locate(
                {"label": "Exam passed", "after": "Educational Qualification(Enter"}, "select")) or qual.get("exam", "")
            self.add_subjects()
            self.check_stop()
            add = self.page.locator(buttons["add_qualification"]).filter(visible=True)
            if not add.count():
                self.ui.status("This is the qualification chosen on Select Exchange - saving it with Submit...")
                if not self.press_next("qualification", buttons["qualification_submit"]):
                    self.wait_until_moved("qualification", "The site did not accept this qualification.")
                return
            before = len(self.added_exams())
            add.first.click()
            settle(self.page, 6000)
            self.page.wait_for_timeout(1500)
            if len(self.added_exams()) <= before:
                self.wait_for_added(before)
        # every qualification is already in the table
        if self.detect() == "qualification":
            if not self.press_next("qualification", buttons["qualification_submit"]):
                self.wait_until_moved("qualification", "All qualifications are added; the site did not move on.")

    def wait_for_added(self, before):
        self.ui.start_wait("Please add the qualification",
                           "The site did not add this qualification (see its message).\n\nPlease correct it in "
                           "Firefox and press 'Add Qualification'. The tool continues when it is in the table.")
        try:
            while True:
                choice = "stop" if self.ui.stop_requested() else self.ui.wait_choice()
                if choice == "stop":
                    raise Cancelled()
                if choice == "skip" or len(self.added_exams()) > before:
                    return
                self.page.wait_for_timeout(1000)
        finally:
            self.ui.end_wait()

    def add_subjects(self):
        """Choose each subject from the certificate in the Subjects list and press its Add button."""
        sub = self.cfg["subjects"]
        listed = self.page.evaluate("""() => [...document.querySelectorAll('select[multiple] option, select[size] option')]
            .map(o => o.text)""")
        have = {_compact(t) for t in listed}
        wanted = [s.strip() for s in re.split(r"[,;/]", self.qual.get("subjects", "")) if s.strip()]
        added = 0
        for subject in wanted:   # the first few (config "max") that are in the site's list
            self.check_stop()
            if added >= sub.get("max", 2):
                break
            if _compact(subject) in have:
                added += 1
                continue
            loc = self.mark(sub["label"], "select", sub.get("after"))
            if loc is None:
                return
            matched, score, best = choose_fuzzy(self.page, loc, subject, sub.get("threshold", 85), 2000)
            if not matched:
                # "FIRST LANGUAGE ODIA" / "MIL ODIA" -> ODIA
                matched, score, best = choose_fuzzy(self.page, loc, subject, 100, 1000, fuzz.token_set_ratio)
            if not matched:
                continue
            add = self.mark(sub["add_after"], f"button:{sub['add_button']}", sub.get("after"))
            if add is not None:
                add.click()
                settle(self.page, 3000)
                added += 1
                self.summary.append(f"Subject: {matched}")
        if not added and not have:
            loc = self.mark(sub["label"], "select", sub.get("after"))
            if loc is not None:
                self.wait_for_user("Subjects", "No subject from the certificate matched the site's list. "
                                               "Choose one and press Add.", loc)

    def languages_page(self):
        lang = self.cfg["languages"]
        for language in [l.strip() for l in self.fields.get("languages", "").split(",") if l.strip()]:
            self.check_stop()
            listed = self.page.evaluate("""() => [...document.querySelectorAll('option, td')]
                .filter(e => e.tagName === 'TD' ? !e.querySelector('*') : (e.parentElement.multiple || e.parentElement.size > 1))
                .map(e => e.innerText)""")
            if _compact(language) in {_compact(t) for t in listed}:
                continue
            self.ui.status(f"Adding language {language}...")
            self.run_steps([{"select": f"={language}", "label": lang["label"], "replace": True, "optional": True}]
                           + [{"check": True, "label": box} for box in lang["proficiency"]]
                           + [{"click": True, "button": lang["add_button"], "after_label": lang["label"]}])
        if not self.press_next("languages", self.cfg["buttons"]["next"]):
            self.wait_until_moved("languages", "The site did not accept the languages.")

    def simple_page(self, name):
        steps = self.pages.get(name, [])
        if steps:
            self.ui.status(f"Filling {self.cfg['page_titles'][name]}...")
            self.run_steps(steps)
        self.check_stop()
        if not self.press_next(name, self.cfg["buttons"]["next"]):
            self.wait_until_moved(name, f"The site did not accept {self.cfg['page_titles'][name]}.")

    def open_registration(self):
        page = self.page
        self.ui.status("Opening JobSeeker Registration...")
        page.goto(self.cfg["entry_url"], wait_until="domcontentloaded")
        page.get_by_text(self.cfg["registration_link"], exact=True).first.click()
        settle(page, 5000)

    def loop(self):
        """Fill whatever page is open, move on, and repeat until the Willingness page."""
        stuck = 0
        while True:
            self.check_stop()
            name = self.detect()
            if name == "willingness":
                self.finish()
                return
            if name == "declaration":
                self.page.locator(self.cfg["buttons"]["accept"]).filter(visible=True).first.click()
                settle(self.page, 5000)
                continue
            handler = {"exchange": self.exchange_page, "personal": self.personal_page,
                       "qualification": self.qualification_page, "languages": self.languages_page}.get(name)
            if name is None:
                stuck += 1
                if stuck > 3:
                    self.wait_until_moved(None, "The tool does not recognise this page.")
                    stuck = 0
                self.page.wait_for_timeout(2000)
                continue
            stuck = 0
            if handler:
                handler()
            else:
                self.simple_page(name)

    def finish(self):
        if self.mismatches:
            self.ui.warn("Prefilled data mismatch",
                         "These fields were already filled on the site and differ from your documents. "
                         "They were NOT changed:\n\n" +
                         "\n".join(f"- {k}: site='{cur}'  documents='{doc}'" for k, cur, doc in self.mismatches))
        self.ui.status("Reached Willingness Details. In Firefox: choose the willingness options if needed, "
                       "tick the declaration and click Submit yourself. Close Firefox when finished.")

    def refill(self):
        self.ui.status("Filling the remaining fields from your documents...")
        self.loop()


def run_employment(config, session, ui):
    result = {"status": "started", "filled": [], "mismatches": [], "skipped": [], "site_messages": [],
              "error": ""}
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
        run = EmploymentRun(config, session, ui, page, result["site_messages"])
        try:
            run.open_registration()
            run.loop()
            result["status"] = "ready_to_submit"
        except Cancelled:
            result["status"] = "cancelled_by_user"
            ui.status("Stopped. Use 'Fill remaining fields' to continue, or finish in Firefox.")
        except Exception as exc:
            result["status"] = "error"
            result["error"] = f"{type(exc).__name__}: {exc}".splitlines()[0][:500]
            saved = save_debug(page)
            if saved:
                result["error"] += f" (screenshot: {saved})"
            ui.warn("Automation error", result["error"] + "\n\nThe browser stays open so you can continue manually.")
            ui.status("Error - see popup. 'Fill remaining fields' continues from the open page.")
        result["filled"] = [(line, "") for line in run.summary]
        result["mismatches"] = run.mismatches
        result["skipped"] = run.manual
        wait_for_close_or_refill(context, page, run, ui)
        result["registration_number"] = session.reg_no
    return result
