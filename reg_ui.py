"""One Time Registration screens: upload and read documents, review details, run in Firefox."""
import os
import re
import threading
import time
import tkinter as tk
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from extractors import MissingApiKey, run_extractor
from runlog import log_run
from ui_common import PrimaryButton, RunButtons, UIBridge, ask_api_key, load_config, reader_note

# document key -> (label, extractor kind or None, required)
DOCUMENTS = {
    "app_aadhaar": ("Applicant's Aadhaar", "aadhaar", True),
    "app_photo": ("Applicant's Photo", None, True),
    "app_bank": ("Applicant's Bank Passbook", "bank", True),
    "nom_aadhaar": ("Nominee's Aadhaar", "aadhaar", True),
    "nom_bank": ("Nominee's Bank Passbook", "bank", True),
    "job_card": ("Job Card", "job_card", True),
    "signature": ("Signature / Thumb (optional)", None, False),
}

RELATIONSHIPS = ["Husband", "Wife", "Unmarried son", "Unmarried daughter", "Dependent father", "Dependent mother"]

# tab -> [(field key, label, dropdown choices or None)]
FORM = {
    "Applicant": [
        ("app_first_name", "First Name", None),
        ("app_surname", "Surname", None),
        ("app_sex", "Sex", ["Male", "Female", "Other"]),
        ("app_dob", "Date of Birth (DD-MM-YYYY)", None),
        ("app_father_name", "Father's Name", None),
        ("app_mobile", "Mobile (from Job Card)", None),
        ("app_aadhaar", "Aadhaar Number", None),
        ("username", "Username (if already registered)", None),
    ],
    "Address (Job Card)": [
        ("addr_line", "Address", None),
        ("addr_state", "State", None),
        ("addr_district", "District", None),
        ("addr_block", "Block", None),
        ("addr_gp", "Gram Panchayat", None),
        ("addr_village", "Village", None),
        ("addr_pin", "Pincode", None),
    ],
    "Nominee": [
        ("nom_name", "Name", None),
        ("nom_relationship", "Relationship", RELATIONSHIPS),
        ("nom_aadhaar", "Aadhaar Number", None),
        ("nom_dob", "Date of Birth (DD-MM-YYYY)", None),
        ("nom_address", "Address", None),
        ("nom_share", "Share %", None),
        ("nom_bank_name", "Bank Name", None),
        ("nom_bank_branch", "Branch", None),
        ("nom_bank_ifsc", "IFSC", None),
        ("nom_bank_account", "Account Number", None),
    ],
    "Applicant Bank": [
        ("app_bank_holder", "Account Holder", None),
        ("app_bank_name", "Bank Name", None),
        ("app_bank_branch", "Branch", None),
        ("app_bank_ifsc", "IFSC", None),
        ("app_bank_account", "Account Number", None),
    ],
    "Employment": [
        ("employer_name", "Employer Name", None),
        ("employer_contact", "Employer Contact", None),
        ("employer_address", "Employer Address", None),
        ("est_district", "Establishment District", None),
        ("est_block", "Establishment Block", None),
        ("est_gp", "Establishment Gram Panchayat", None),
        ("est_village", "Establishment Village", None),
        ("est_pin", "Establishment Pincode", None),
        ("employment_from", "Employed From", None),
        ("employment_to", "Employed Upto", None),
        ("establishment_type", "Govt / Private", ["Private", "Government"]),
        ("employment_certified_by", "Certified By", ["Self certification", "Employer", "Inspector", "Trade Union"]),
    ],
    "Other": [
        ("marital_status", "Marital Status", ["Married", "Single", "Divorced", "Widowed"]),
        ("spouse_name", "Spouse's Name", None),
        ("nature_of_work", "Nature of Work", None),
        ("education", "Education", ["Below 5th class", "Below 7th class", "Below Matriculation", "Matriculation",
                                     "Intermediate", "Graduation", "ITI", "Diploma", "Others", "Iliterate"]),
        ("category", "Category", ["General", "OBC", "SC", "ST", "SEBC", "Other"]),
        ("religion", "Religion", ["Hindu", "Muslim", "Christian", "Sikhs", "Buddhist", "Other"]),
        ("physically_challenged", "Physically Challenged", ["No", "Yes"]),
    ],
}

REQUIRED = {"app_first_name": "First Name", "app_surname": "Surname", "app_sex": "Sex",
            "app_mobile": "Mobile", "app_aadhaar": "Aadhaar Number", "nom_relationship": "Nominee's Relationship"}


@dataclass
class RegSession:
    docs: dict = field(default_factory=dict)
    fields: dict = field(default_factory=dict)
    username: str = ""


def split_name(full):
    parts = full.split()
    if len(parts) < 2:
        return full, ""
    return " ".join(parts[:-1]), parts[-1]


def map_extracted(doc, data, current):
    """Turn one document's extracted values into form fields (only non-empty values)."""
    out = {}
    if doc == "app_aadhaar":
        first, last = split_name(data["name"])
        relation = data["relation_type"].upper().replace(" ", "")
        out.update(app_first_name=first, app_surname=last, app_dob=data["dob"],
                   app_sex=data["sex"].title(), app_aadhaar=re.sub(r"\D", "", data["aadhaar_number"]),
                   app_father_name=data["relative_name"] if relation in ("S/O", "D/O") else "")
        if relation == "W/O" and not current.get("spouse_name"):
            out["spouse_name"] = data["relative_name"]
    elif doc == "nom_aadhaar":
        out.update(nom_name=data["name"], nom_aadhaar=re.sub(r"\D", "", data["aadhaar_number"]),
                   nom_dob=data["dob"], nom_address=data["address"])
        if not current.get("nom_relationship"):
            applicant_sex = current.get("app_sex", "").lower()
            if data["sex"].upper() == "MALE" and applicant_sex == "female":
                out["nom_relationship"] = "Husband"
            elif data["sex"].upper() == "FEMALE" and applicant_sex == "male":
                out["nom_relationship"] = "Wife"
    elif doc in ("app_bank", "nom_bank"):
        prefix = doc
        out.update({f"{prefix}_holder": data["account_holder"], f"{prefix}_name": data["bank_name"],
                    f"{prefix}_branch": data["branch"], f"{prefix}_ifsc": data["ifsc"],
                    f"{prefix}_account": data["account_number"]})
        out.pop("nom_bank_holder", None)
    elif doc == "job_card":
        # the applicant's address comes from the top section of the job card, not the Aadhaar
        address = ", ".join(f"{prefix} {data[key]}" for prefix, key in
                            (("AT", "village"), ("PO", "post_office"), ("PS", "police_station")) if data[key])
        out.update(addr_line=address, addr_district=data["district"], addr_block=data["block"],
                   addr_gp=data["gp_municipality"], addr_village=data["village"], addr_pin=data["pin"])
        out.update(app_mobile=data["mobile"], employer_name=data["employer_name"],
                   employer_contact=data["employer_contact"], employer_address=data["employer_address"],
                   employment_from=data["employment_from"], employment_to=data["employment_to"],
                   # employer column first, else the worker's address written on the same card
                   est_district=data["est_district"] or data["district"],
                   est_block=data["est_block"] or data["block"],
                   est_gp=data["gp_municipality"] or data["est_village"] or data["village"],
                   est_village=data["est_village"] or data["village"],
                   est_pin=data["est_pin"] or data["pin"])
    return {k: v for k, v in out.items() if v}


class RegDocsScreen(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.session = app.reg_session
        self.config_data = load_config()
        defaults = self.config_data.get("registration", {}).get("defaults", {})
        for key, value in defaults.items():
            self.session.fields.setdefault(key, value)

        ttk.Label(self, text="One Time Registration", style="Title.TLabel").pack(anchor="w", pady=(0, 10))
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)

        docs = ttk.LabelFrame(body, text="Documents", padding=10)
        docs.pack(side="left", fill="y")
        self.doc_labels, self.doc_status = {}, {}
        # shared with the Nominee tab: chosen here while adding the nominee's documents
        self.vars = {"nom_relationship": tk.StringVar(value=self.session.fields.get("nom_relationship", ""))}
        row = 0
        for key, (label, kind, required) in DOCUMENTS.items():
            ttk.Label(docs, text=label).grid(row=row, column=0, sticky="w", pady=(8, 0))
            ttk.Button(docs, text="Browse...", command=lambda k=key: self.browse(k)).grid(
                row=row, column=1, padx=6, pady=(8, 0))
            name = Path(self.session.docs[key]).name if key in self.session.docs else "No file"
            file_label = ttk.Label(docs, text=name, foreground="#555", width=34, cursor="hand2")
            file_label.grid(row=row + 1, column=0, sticky="w")
            file_label.bind("<Button-1>", lambda e, k=key: self.open_doc(k))
            status = ttk.Label(docs, text="", foreground="#1a5fb4")
            status.grid(row=row + 1, column=1, sticky="w")
            self.doc_labels[key], self.doc_status[key] = file_label, status
            row += 2
            if key == "nom_aadhaar":
                ttk.Label(docs, text="Nominee's Relationship").grid(row=row, column=0, sticky="w", pady=(8, 0))
                ttk.Combobox(docs, textvariable=self.vars["nom_relationship"], values=RELATIONSHIPS,
                             state="readonly", width=31).grid(row=row + 1, column=0, columnspan=2, sticky="w")
                row += 2

        tabs = ttk.Notebook(body)
        tabs.pack(side="left", fill="both", expand=True, padx=(12, 0))
        for tab, rows in FORM.items():
            frame = ttk.Frame(tabs, padding=12)
            tabs.add(frame, text=tab)
            for row, (key, label, choices) in enumerate(rows):
                ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=4)
                var = self.vars.get(key) or tk.StringVar(
                    value=self.session.fields.get(key, "") if key != "username" else self.session.username)
                if choices:
                    widget = ttk.Combobox(frame, textvariable=var, values=choices, width=42)
                else:
                    widget = ttk.Entry(frame, textvariable=var, width=45)
                widget.grid(row=row, column=1, sticky="w", pady=4, padx=(10, 0))
                self.vars[key] = var
        tip = ttk.Label(self, text="Tip: click a file name to open it. Fields read from documents can be corrected "
                                   "before continuing.", foreground="#555")
        tip.pack(side="bottom", anchor="w", pady=(8, 0), before=body)

        buttons = ttk.Frame(self)
        # packed before the body so it keeps its space when the window is short
        buttons.pack(side="bottom", fill="x", pady=(10, 0), before=tip)
        ttk.Button(buttons, text="Back", command=app.go_home).pack(side="left")
        self.continue_btn = PrimaryButton(buttons, text="Continue", command=self.next)
        self.continue_btn.pack(side="right")
        self.pending = 0

    def browse(self, key):
        label, kind, _ = DOCUMENTS[key]
        types = [("Images", "*.jpg *.jpeg *.png")] if key in ("app_photo", "signature") else []
        path = filedialog.askopenfilename(parent=self, title=f"Select {label}",
                                          filetypes=types + [("Documents", "*.pdf *.jpg *.jpeg *.png"),
                                                             ("All files", "*.*")])
        if not path:
            return
        self.session.docs[key] = path
        self.doc_labels[key].config(text=Path(path).name)
        if kind:
            self.read(key, kind, path)
        else:
            self.doc_status[key].config(text="Added")

    def read(self, key, kind, path):
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
        self.wait_for(thread, outcome, key, kind, path)

    def wait_for(self, thread, outcome, key, kind, path):
        if thread.is_alive():
            self.after(200, self.wait_for, thread, outcome, key, kind, path)
            return
        self.pending -= 1
        if not self.pending:
            self.continue_btn.config(state="normal")
        error = outcome.get("error")
        if isinstance(error, MissingApiKey):
            self.doc_status[key].config(text="")
            if ask_api_key(self, error):
                self.read(key, kind, path)
            return
        if error:
            self.doc_status[key].config(text="Not read")
            messagebox.showerror("Could not read document", f"{DOCUMENTS[key][0]}:\n\n{error}", parent=self)
            return
        note = " (Groq)" if reader_note(outcome["data"]) else ""
        current = {k: v.get().strip() for k, v in self.vars.items()}
        values = map_extracted(key, outcome["data"], current)
        for field_key, value in values.items():
            self.vars[field_key].set(value)
        if key == "nom_aadhaar" and not self.vars["spouse_name"].get().strip() \
                and self.vars["nom_relationship"].get() in ("Husband", "Wife"):
            self.vars["spouse_name"].set(self.vars["nom_name"].get())
        self.doc_status[key].config(text=f"Read {len(values)} fields{note}")

    def open_doc(self, key):
        if key in self.session.docs:
            os.startfile(self.session.docs[key])

    def next(self):
        missing_docs = [label for key, (label, _, required) in DOCUMENTS.items()
                        if required and key not in self.session.docs]
        values = {k: v.get().strip() for k, v in self.vars.items()}
        missing = [label for key, label in REQUIRED.items() if not values.get(key)]
        problems = []
        if missing_docs:
            problems.append("Documents not added: " + ", ".join(missing_docs))
        if missing:
            problems.append("Required fields empty: " + ", ".join(missing))
        if values.get("app_mobile") and len(re.sub(r"\D", "", values["app_mobile"])) != 10:
            problems.append("Mobile must be 10 digits")
        if values.get("app_aadhaar") and len(re.sub(r"\D", "", values["app_aadhaar"])) != 12:
            problems.append("Aadhaar Number must be 12 digits")
        if problems:
            messagebox.showwarning("Please check", "\n".join(problems), parent=self)
            return
        if not values.get("category") or not values.get("education"):
            if not messagebox.askyesno("Missing details", "Category and/or Education are empty. The tool will "
                                       "ask you to choose them in Firefox when it gets there.\n\nContinue?",
                                       parent=self):
                return
        from registration import load_saved_username, save_session

        values["app_mobile"] = re.sub(r"\D", "", values["app_mobile"])
        values["app_aadhaar"] = re.sub(r"\D", "", values["app_aadhaar"])
        username = values.pop("username")
        previous = load_saved_username(values["app_aadhaar"])
        if not username and previous:
            if messagebox.askyesno(
                    "Already registered",
                    f"This Aadhaar was already registered by this tool as username {previous}.\n\n"
                    "Yes = log in as that user and continue the application (recommended).\n"
                    "No = create another new account.", parent=self):
                username = previous
                self.vars["username"].set(previous)
        self.session.username = username
        self.session.fields = values
        save_session(self.session)
        self.app.show(RegRunScreen)


class RegRunScreen(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        app.compact(True)
        ttk.Label(self, text="One Time Registration - running in Firefox",
                  font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(0, 6))
        self.log = tk.Text(self, height=10, state="disabled", font=("Consolas", 9), wrap="word")
        self.log.pack(fill="both", expand=True)
        buttons = ttk.Frame(self)
        buttons.pack(pady=10)
        self.bridge = UIBridge(app, self.add_status)
        self.controls = RunButtons(buttons, self.bridge, self.add_status)
        self.back_btn = ttk.Button(buttons, text="Back to details", state="disabled",
                                   command=lambda: app.show(RegDocsScreen))
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
        from registration import run_registration

        session = self.app.reg_session
        started = time.monotonic()
        try:
            result = run_registration(load_config(), session, self.bridge)
        except Exception as exc:
            result = {"status": "error", "error": f"{type(exc).__name__}: {exc}".splitlines()[0][:500]}
            self.bridge.warn("Could not run automation", result["error"])
        log_run({
            "action": "one_time_registration",
            "registration_number": "",
            "username": session.username,
            "document": "; ".join(f"{k}={Path(v).name}" for k, v in session.docs.items()),
            "status": result.get("status"),
            "village_input": session.fields.get("addr_village", ""),
            "filled": "; ".join(k for k, _ in result.get("filled", [])),
            "mismatches": "; ".join(f"{k}: site={c} doc={d}" for k, c, d in result.get("mismatches", [])),
            "skipped": "; ".join(result.get("skipped", [])),
            "site_messages": " | ".join(result.get("site_messages", [])),
            "error": result.get("error", ""),
            "duration_s": round(time.monotonic() - started, 1),
        })
        self.bridge.status(f"Finished: {result.get('status')}. Logged to logs/log.csv"
                           + (f". Username: {session.username}" if session.username else ""))
        self.bridge._call(lambda: (self.app.compact(False), self.home_btn.config(state="normal"),
                                   self.back_btn.config(state="normal"), self.controls.finished()))
        self.bridge.active = False
