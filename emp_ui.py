"""Employment Registration screens: upload and read documents, check the details, run in Firefox."""
import os
import re
import threading
import time
import tkinter as tk
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from rapidfuzz import fuzz, process, utils

from extractors import MissingApiKey, run_extractor
from runlog import log_run
from ui_common import RunButtons, UIBridge, ask_api_key, load_config, reader_note

# document key -> (label, extractor kind or None, required)
DOCUMENTS = {
    "card": ("Sample Card (X-I)", "emp_card", True),
    "photo": ("Photo", None, True),
}
DEGREES = 5
for _n in range(1, DEGREES + 1):
    DOCUMENTS[f"deg{_n}_cert"] = (f"Highest Degree Certificate {_n}", "certificate", False)
    DOCUMENTS[f"deg{_n}_marks"] = (f"Highest Degree Marksheet {_n}", "certificate", False)

QUAL_TABS = {f"deg{n}": f"Degree {n}" for n in range(1, DEGREES + 1)}
LEVELS = ["10TH PASS", "12TH PASS", "ITI CERTIFICATE HOLDER", "DIPLOMA HOLDER", "GRADUATE", "GRADUATE(TECH.)",
          "HONOURS GRADUATE", "POST GRADUATE", "DOCTORATE"]
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
QUAL_FIELDS = [("level", "Qualification Level", LEVELS), ("exam", "Exam Passed", None),
               ("board", "Board / University", None), ("school", "School / Institute", None),
               ("medium", "Medium of Instruction", ["ODIA", "ENGLISH", "HINDI"]), ("year", "Passing Year", None),
               ("month", "Passing Month", MONTHS), ("cert_no", "Certificate Number", None),
               ("division", "Division", ["", "FIRST", "SECOND", "THIRD"]), ("percentage", "Percentage", None),
               ("grade", "Grade", None), ("subjects", "Subjects (comma separated)", None)]


@dataclass
class EmpSession:
    docs: dict = field(default_factory=dict)
    fields: dict = field(default_factory=dict)
    quals: list = field(default_factory=list)
    reg_no: str = ""


def split_name(full):
    parts = full.split()
    if len(parts) < 2:
        return full, "", ""
    return parts[0], " ".join(parts[1:-1]), parts[-1]


def site_district(name, cfg):
    """The card's district written the way the portal lists it (KENDUJHAR -> KEONJHAR)."""
    name = re.sub(r"\s+", " ", name or "").strip().upper()
    if not name:
        return ""
    name = cfg.get("district_aliases", {}).get(name, name)
    best = process.extractOne(name, list(cfg["exchanges"]), scorer=fuzz.QRatio, processor=utils.default_process)
    return best[0] if best and best[1] >= 75 else name


def whole_percent(text):
    """'69.17' / '69.17%' / marks '415/600' -> '69' (the portal takes a whole number)."""
    marks = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)\s*", text or "")
    if marks and float(marks.group(2)):
        return str(int(float(marks.group(1)) / float(marks.group(2)) * 100 + 0.5))
    match = re.search(r"\d+(?:\.\d+)?", text or "")
    return str(int(float(match.group()) + 0.5)) if match else ""


def site_caste(text):
    text = re.sub(r"[^A-Z ]", "", (text or "").upper()).strip()
    return {"SC": "SCHEDULE CASTE", "ST": "SCHEDULE TRIBE", "GEN": "GENERAL", "UR": "GENERAL",
            "GENERAL": "GENERAL", "OBC": "OBC", "SEBC": "SEBC"}.get(text, text)


class EmpDocsScreen(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.session = app.emp_session
        self.config_data = load_config()
        self.cfg = self.config_data["employment"]
        for key, value in self.cfg.get("defaults", {}).items():
            self.session.fields.setdefault(key, value)

        ttk.Label(self, text="Employment Registration", style="Title.TLabel").pack(anchor="w", pady=(0, 10))
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        left = ttk.Frame(body)
        left.pack(side="left", fill="y")

        docs = ttk.LabelFrame(left, text="Documents", padding=8)
        docs.pack(fill="x")
        self.doc_labels, self.doc_status = {}, {}
        # one row per document; a degree's certificate and marksheet share a row to fit the screen
        layout = [("Sample Card (X-I)", ["card"]), ("Photo", ["photo"])] + [
            (f"Highest Degree {n}", [f"deg{n}_cert", f"deg{n}_marks"]) for n in range(1, DEGREES + 1)]
        ttk.Label(docs, text="Certificate", foreground="#555").grid(row=0, column=1, columnspan=2, sticky="w")
        ttk.Label(docs, text="Marksheet", foreground="#555").grid(row=0, column=3, columnspan=2, sticky="w")
        for row, (label, keys) in enumerate(layout, start=1):
            ttk.Label(docs, text=label).grid(row=row, column=0, sticky="w", pady=2)
            for col, key in zip((1, 3), keys):
                ttk.Button(docs, text="Browse...", width=9, command=lambda k=key: self.browse(k)).grid(
                    row=row, column=col, padx=4, pady=2)
                name = Path(self.session.docs[key]).name if key in self.session.docs else "No file"
                file_label = ttk.Label(docs, text=name, foreground="#555", width=15, cursor="hand2")
                file_label.grid(row=row, column=col + 1, sticky="w")
                file_label.bind("<Button-1>", lambda e, k=key: self.open_doc(k))
                self.doc_labels[key] = file_label
            status = ttk.Label(docs, text="", foreground="#1a5fb4", width=19)
            status.grid(row=row, column=5, sticky="w")
            for key in keys:
                self.doc_status[key] = status

        self.vars = {}
        manual = ttk.LabelFrame(left, text="Enter these yourself", padding=8)
        manual.pack(fill="x", pady=(10, 0))
        districts = list(self.cfg["exchanges"])
        rows = [("qualification", "Qualification", self.cfg["qualification_levels"]),
                ("district", "District (for Exchange)", districts),
                ("exchange", "Exchange", []),
                ("email", "Email", None),
                ("gender", "Gender", self.cfg["genders"]),
                ("caste", "Caste", self.cfg["castes"]),
                ("sub_caste", "Sub-Caste", []),
                ("marital_status", "Marital Status", self.cfg["marital_statuses"])]
        for row, (key, label, choices) in enumerate(rows):
            ttk.Label(manual, text=label).grid(row=row, column=0, sticky="w", pady=3)
            var = tk.StringVar(value=self.session.fields.get(key, ""))
            if choices is None:
                widget = ttk.Entry(manual, textvariable=var, width=40)
            else:
                widget = ttk.Combobox(manual, textvariable=var, values=choices, width=37,
                                      state="normal" if key == "district" else "readonly")
            widget.grid(row=row, column=1, sticky="w", pady=3, padx=(8, 0))
            self.vars[key] = var
            if key == "exchange":
                self.exchange_box = widget
            if key == "sub_caste":
                widget.config(state="normal")   # type it when the site's list is not known yet
                self.sub_caste_box = widget
        self.vars["district"].trace_add("write", lambda *a: self.update_exchanges())
        self.update_exchanges()

        tabs = ttk.Notebook(body)
        tabs.pack(side="left", fill="both", expand=True, padx=(12, 0))
        form = {
            "Applicant": [("first_name", "First Name", None), ("middle_name", "Middle Name", None),
                          ("last_name", "Last Name", None), ("father_name", "Father's Name", None),
                          ("mother_name", "Mother's Name", None), ("dob", "Date of Birth (DD-MM-YYYY)", None),
                          ("aadhaar", "Aadhaar Number", None), ("mobile", "Mobile", None),
                          ("religion", "Religion", None)],
            "Address": [("addr_at", "Address line 1 (At)", None), ("addr_po", "Address line 2 (Po)", None),
                        ("tahasil", "Tahasil / City", None), ("police_station", "Police Station", None),
                        ("pin", "PIN", None)],
            "Certificates": [("res_cert_no", "Residence Certificate No", None),
                             ("res_cert_date", "Residence Certificate Date", None),
                             ("res_tahasil", "Certificate Tahasil (Block)", None),
                             ("caste_cert_no", "Caste Certificate No", None),
                             ("caste_cert_date", "Caste Certificate Date", None),
                             ("cert_authority", "Certificate Authority", None)],
            "Physical & Languages": [("height", "Height (5.4 = 5 ft 4 in)", None), ("weight", "Weight (kg)", None),
                                     ("chest", "Chest", None), ("blood_group", "Blood Group", None),
                                     ("languages", "Languages (comma separated)", None)],
        }
        for tab, rows in form.items():
            frame = ttk.Frame(tabs, padding=12)
            tabs.add(frame, text=tab)
            for row, (key, label, choices) in enumerate(rows):
                self.add_row(frame, row, key, label, choices, self.session.fields.get(key, ""))
        saved = {q.get("tab"): q for q in self.session.quals}
        for tab_key, title in QUAL_TABS.items():
            frame = ttk.Frame(tabs, padding=12)
            tabs.add(frame, text=title)
            for row, (key, label, choices) in enumerate(QUAL_FIELDS):
                self.add_row(frame, row, f"{tab_key}.{key}", label, choices, saved.get(tab_key, {}).get(key, ""))

        self.vars["caste"].trace_add("write", lambda *a: self.update_sub_castes())
        self.update_sub_castes()

        tip = ttk.Label(self, text="Tip: click a file name to open it. Fields read from documents can be corrected "
                                   "before continuing. Put each qualification (10th, 12th, graduation...) in its own "
                                   "Highest Degree row - the tool enters them lowest first.",
                        foreground="#555")
        tip.pack(side="bottom", anchor="w", pady=(8, 0), before=body)
        buttons = ttk.Frame(self)
        # packed before the body so it keeps its space when the window is short
        buttons.pack(side="bottom", fill="x", pady=(10, 0), before=tip)
        ttk.Button(buttons, text="Back", command=app.go_home).pack(side="left")
        self.continue_btn = ttk.Button(buttons, text="Continue", command=self.next)
        self.continue_btn.pack(side="right")
        self.pending = 0

    def add_row(self, frame, row, key, label, choices, value):
        ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=3)
        var = tk.StringVar(value=value)
        widget = (ttk.Combobox(frame, textvariable=var, values=choices, width=42) if choices
                  else ttk.Entry(frame, textvariable=var, width=45))
        widget.grid(row=row, column=1, sticky="w", pady=3, padx=(10, 0))
        self.vars[key] = var

    def update_exchanges(self):
        district = self.vars["district"].get().strip().upper()
        values = self.cfg["exchanges"].get(district, [])
        self.exchange_box.config(values=values)
        if self.vars["exchange"].get() not in values:
            self.vars["exchange"].set(values[0] if values else "")

    def sub_caste_options(self, caste):
        """Every sub-caste of this caste the tool knows: from config, plus the site's full list once a run
        has reached the caste certificate tab (the site shows it only after the captcha)."""
        from employment import known_options

        caste = caste.strip().upper()
        return known_options("sub_castes", caste, self.cfg.get("sub_castes", {}).get(caste, []))

    def best_sub_caste(self, wanted, options):
        from employment import names_ratio

        best = process.extractOne(wanted, options, scorer=names_ratio, processor=utils.default_process) \
            if wanted and options else None
        return best[0] if best and best[1] >= 70 else wanted

    def update_sub_castes(self):
        options = self.sub_caste_options(self.vars["caste"].get())
        self.sub_caste_box.config(values=options)
        current = self.vars["sub_caste"].get().strip()
        if current and current not in options:
            self.vars["sub_caste"].set(self.best_sub_caste(current, options))

    # --- documents ------------------------------------------------------------
    def browse(self, key):
        label, kind, _ = DOCUMENTS[key]
        types = [("Images", "*.jpg *.jpeg *.png")] if key == "photo" else []
        path = filedialog.askopenfilename(parent=self, title=f"Select {label}",
                                          filetypes=types + [("Documents", "*.pdf *.jpg *.jpeg *.png"),
                                                             ("All files", "*.*")])
        if not path:
            return
        self.session.docs[key] = path
        self.doc_labels[key].config(text=Path(path).name)
        if not kind:
            self.doc_status[key].config(text="Added")
            return
        if key.startswith("deg"):
            # certificate and marksheet are read together into the same Degree tab
            row = key.split("_")[0]
            paths = [self.session.docs[k] for k in (f"{row}_cert", f"{row}_marks") if k in self.session.docs]
            self.read(key, kind, paths, row)
        else:
            self.read(key, kind, path, None)

    def read(self, key, kind, path, qual_tab):
        self.doc_status[key].config(text="Reading...")
        self.pending += 1
        self.continue_btn.config(state="disabled")
        outcome = {}

        def work():
            try:
                outcome["data"] = run_extractor(self.config_data.get("extractor", "extractors.manual"), path, kind)
            except Exception as exc:
                outcome["error"] = exc

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        self.wait_for(thread, outcome, key, kind, path, qual_tab)

    def wait_for(self, thread, outcome, key, kind, path, qual_tab):
        if thread.is_alive():
            self.after(200, self.wait_for, thread, outcome, key, kind, path, qual_tab)
            return
        self.pending -= 1
        if not self.pending:
            self.continue_btn.config(state="normal")
        error = outcome.get("error")
        if isinstance(error, MissingApiKey):
            self.doc_status[key].config(text="")
            if ask_api_key(self, error):
                self.read(key, kind, path, qual_tab)
            return
        if error:
            self.doc_status[key].config(text="Not read")
            messagebox.showerror("Could not read document", f"{DOCUMENTS[key][0]}:\n\n{error}", parent=self)
            return
        note = " (Groq)" if reader_note(outcome["data"]) else ""
        values = self.map_card(outcome["data"]) if kind == "emp_card" else self.map_qual(outcome["data"], qual_tab)
        for field_key, value in values.items():
            if value and field_key in self.vars:
                self.vars[field_key].set(value)
        self.doc_status[key].config(text=f"Read {sum(1 for v in values.values() if v)} fields{note}")
        self.update_highest()

    def map_card(self, data):
        first, middle, last = split_name(data["name"])
        out = dict(first_name=first, middle_name=middle, last_name=last, father_name=data["father_name"],
                   mother_name=data["mother_name"], dob=data["dob"], aadhaar=re.sub(r"\D", "", data["aadhaar"]),
                   mobile=re.sub(r"\D", "", data["mobile"]), caste=site_caste(data["caste"]),
                   sub_caste=data["sub_caste"], addr_at=data["addr_at"], addr_po=data["addr_po"],
                   tahasil=data["tahasil"], res_tahasil=data["tahasil"], police_station=data["police_station"],
                   pin=data["pin"], res_cert_no=data["res_cert_no"], res_cert_date=data["res_cert_date"],
                   caste_cert_no=data["caste_cert_no"], caste_cert_date=data["caste_cert_date"],
                   height=data["height"], weight=data["weight"], chest=data["chest"],
                   blood_group=data["blood_group"], district=site_district(data["district"], self.cfg))
        # default to the closest of the site's sub-castes; the user can pick another in the dropdown
        out["sub_caste"] = self.best_sub_caste(
            out["sub_caste"], self.sub_caste_options(out["caste"] or self.vars["caste"].get()))
        if not self.vars["email"].get().strip():
            out["email"] = data["email"].lower()
        return out

    def map_qual(self, data, tab):
        out = {f"{tab}.{key}": value for key, value in data.items()}
        if not data["medium"]:
            defaults = self.cfg.get("defaults", {})
            out[f"{tab}.medium"] = defaults.get("medium_10th" if data["level"] == "10TH PASS" else "medium_higher", "")
        if data["month"]:
            out[f"{tab}.month"] = data["month"][:3].title()
        out[f"{tab}.percentage"] = whole_percent(data["percentage"])
        return out

    def update_highest(self):
        """Set Qualification (Select Exchange page) from the last filled degree row - documents are added
        lowest first, so the last one is the highest. A value the user picked by hand is kept."""
        levels = [self.vars[f"{tab}.level"].get() for tab in QUAL_TABS]
        levels = [lv for lv in levels if lv in LEVELS]
        current = self.vars["qualification"].get()
        if levels and (not current or current == getattr(self, "auto_qualification", None)):
            self.vars["qualification"].set(levels[-1])
            self.auto_qualification = levels[-1]

    def open_doc(self, key):
        if key in self.session.docs:
            os.startfile(self.session.docs[key])

    # --- continue -----------------------------------------------------------
    def next(self):
        values = {k: v.get().strip() for k, v in self.vars.items() if "." not in k}
        problems = []
        missing_docs = [label for key, (label, _, required) in DOCUMENTS.items()
                        if required and key not in self.session.docs]
        if missing_docs:
            problems.append("Documents not added: " + ", ".join(missing_docs))
        labels = {"qualification": "Qualification", "exchange": "Exchange", "email": "Email", "gender": "Gender",
                  "marital_status": "Marital Status", "first_name": "First Name", "aadhaar": "Aadhaar Number",
                  "mobile": "Mobile", "caste": "Caste", "dob": "Date of Birth"}
        missing = [label for key, label in labels.items() if not values.get(key)]
        if missing:
            problems.append("Required fields empty: " + ", ".join(missing))
        if values.get("email") and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", values["email"]):
            problems.append("Email does not look right")
        if values.get("mobile") and len(re.sub(r"\D", "", values["mobile"])) != 10:
            problems.append("Mobile must be 10 digits")
        if values.get("aadhaar") and len(re.sub(r"\D", "", values["aadhaar"])) != 12:
            problems.append("Aadhaar Number must be 12 digits")
        quals = []
        for tab, title in QUAL_TABS.items():
            qual = {key: self.vars[f"{tab}.{key}"].get().strip() for key, _, _ in QUAL_FIELDS}
            qual["percentage"] = whole_percent(qual["percentage"])
            # a 10th certificate that also carries the marks can be added as certificate only
            qual["cert_file"] = self.session.docs.get(f"{tab}_cert") or self.session.docs.get(f"{tab}_marks", "")
            qual["marksheet_file"] = self.session.docs.get(f"{tab}_marks") or qual["cert_file"]
            if not (qual["level"] or qual["exam"]):
                continue
            if not qual["cert_file"]:
                problems.append(f"{title}: add its certificate or marksheet")
            qual["tab"] = tab
            quals.append(qual)
        if not quals:
            problems.append("Add at least one Highest Degree certificate (e.g. the 10th in Highest Degree 1)")
        if problems:
            messagebox.showwarning("Please check", "\n".join(problems), parent=self)
            return
        from employment import load_saved_reg_no, save_emp_session

        values["mobile"] = re.sub(r"\D", "", values["mobile"])
        values["aadhaar"] = re.sub(r"\D", "", values["aadhaar"])
        values["district"] = values["district"].upper()
        previous = load_saved_reg_no(values["aadhaar"])
        if previous and not messagebox.askyesno(
                "Already registered",
                f"This Aadhaar was already registered by this tool as {previous}.\n\n"
                "Registering again creates a second registration. Continue anyway?", parent=self):
            return
        self.session.fields = {**self.cfg.get("defaults", {}), **{k: v for k, v in values.items() if v}}
        self.session.quals = quals
        self.session.reg_no = ""
        save_emp_session(self.session)
        self.app.show(EmpRunScreen)


class EmpRunScreen(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        app.compact(True)
        ttk.Label(self, text="Employment Registration - running in Firefox",
                  font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(0, 6))
        self.log = tk.Text(self, height=10, state="disabled", font=("Consolas", 9), wrap="word")
        self.log.pack(fill="both", expand=True)
        buttons = ttk.Frame(self)
        buttons.pack(pady=10)
        self.bridge = UIBridge(app, self.add_status)
        self.controls = RunButtons(buttons, self.bridge, self.add_status)
        self.back_btn = ttk.Button(buttons, text="Back to details", state="disabled",
                                   command=lambda: app.show(EmpDocsScreen))
        self.back_btn.pack(side="left", padx=6)
        self.home_btn = ttk.Button(buttons, text="Home", state="disabled", command=app.go_home)
        self.home_btn.pack(side="left", padx=6)
        threading.Thread(target=self.worker, daemon=True).start()

    def add_status(self, msg):
        self.log.config(state="normal")
        self.log.insert("end", f"{time.strftime('%H:%M:%S')}  {msg}\n")
        self.log.see("end")
        self.log.config(state="disabled")

    def worker(self):
        from employment import run_employment

        session = self.app.emp_session
        started = time.monotonic()
        try:
            result = run_employment(load_config(), session, self.bridge)
        except Exception as exc:
            result = {"status": "error", "error": f"{type(exc).__name__}: {exc}".splitlines()[0][:500]}
            self.bridge.warn("Could not run automation", result["error"])
        log_run({
            "action": "employment_registration",
            "registration_number": session.reg_no,
            "document": "; ".join(f"{k}={Path(v).name}" for k, v in session.docs.items()),
            "status": result.get("status"),
            "filled": "; ".join(k for k, _ in result.get("filled", [])),
            "mismatches": "; ".join(f"{k}: site={c} doc={d}" for k, c, d in result.get("mismatches", [])),
            "skipped": "; ".join(result.get("skipped", [])),
            "site_messages": " | ".join(result.get("site_messages", [])),
            "error": result.get("error", ""),
            "duration_s": round(time.monotonic() - started, 1),
        })
        self.bridge.status(f"Finished: {result.get('status')}. Logged to logs/log.csv"
                           + (f". Registration No: {session.reg_no}" if session.reg_no else ""))
        self.bridge._call(lambda: (self.app.compact(False), self.home_btn.config(state="normal"),
                                   self.back_btn.config(state="normal"), self.controls.finished()))
        self.bridge.active = False
