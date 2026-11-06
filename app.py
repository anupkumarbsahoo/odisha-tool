"""Odisha construction worker helper - Tkinter front end."""
import json
import os
import threading
import time
import tkinter as tk
from dataclasses import asdict, dataclass, field
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import pypdfium2 as pdfium
from PIL import Image, ImageTk

from extractors import FIELDS, MissingApiKey, run_extractor
from runlog import log_run
from ui_common import BASE_DIR, RunButtons, UIBridge, ask_api_key, load_config, reader_note

SESSIONS_DIR = BASE_DIR / "sessions"
IMAGE_TYPES = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"}


@dataclass
class Session:
    registration_number: str = ""
    document_path: str = ""
    fields: dict = field(default_factory=dict)


def session_file(registration_number):
    safe = "".join(c for c in registration_number if c.isalnum() or c in "-_")
    return SESSIONS_DIR / f"{safe}.json"


def save_session(session):
    SESSIONS_DIR.mkdir(exist_ok=True)
    session_file(session.registration_number).write_text(
        json.dumps(asdict(session), indent=2, ensure_ascii=False), encoding="utf-8")


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Odisha Construction Worker Tool")
        # fit the screen (e.g. 1366x768 laptops) so the bottom buttons are never under the taskbar
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"{min(1250, sw - 40)}x{min(800, sh - 80)}+10+10")
        self.minsize(min(900, sw - 40), min(600, sh - 80))
        try:
            self.state("zoomed")
        except tk.TclError:
            pass
        self.session = Session()
        style = ttk.Style(self)
        style.configure("Title.TLabel", font=("Segoe UI", 18, "bold"))
        style.configure("Big.TButton", font=("Segoe UI", 12), padding=12)
        self.container = ttk.Frame(self, padding=20)
        self.container.pack(fill="both", expand=True)
        self.screen = None
        self.reg_session = None
        self.emp_session = None
        self.show(HomeScreen)

    def go_home(self):
        self.show(HomeScreen)

    def compact(self, on):
        """While Firefox works, shrink to a small status panel in the bottom-right corner that stays
        on top of Firefox, so its log and buttons are always visible."""
        if on:
            self._was_zoomed = self.state() == "zoomed"
            self.state("normal")
            self._full_geometry = self.geometry()
            self.minsize(400, 250)
            w, h = 560, 330
            self.geometry(f"{w}x{h}+{self.winfo_screenwidth() - w - 20}+{self.winfo_screenheight() - h - 80}")
            self.container.config(padding=8)
            self.attributes("-topmost", True)
        elif getattr(self, "_full_geometry", None):
            self.attributes("-topmost", False)
            sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
            self.minsize(min(900, sw - 40), min(600, sh - 80))
            self.geometry(self._full_geometry)
            if getattr(self, "_was_zoomed", False):
                self.state("zoomed")
            self.container.config(padding=20)
            self._full_geometry = None

    def show(self, screen_cls):
        if self.screen is not None:
            self.screen.destroy()
        self.screen = screen_cls(self.container, self)
        self.screen.pack(fill="both", expand=True)


class HomeScreen(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        ttk.Label(self, text="Choose an option", style="Title.TLabel").pack(pady=(10, 30))
        options = [
            ("Pay Annual Contribution", self.pay_annual),
            ("One Time Registration", self.one_time_registration),
            ("Employment Registration", self.employment_registration),
            ("Resumption", self.placeholder),
        ]
        for label, command in options:
            ttk.Button(self, text=label, style="Big.TButton", width=32,
                       command=command).pack(pady=8)
        self.app = app

    def pay_annual(self):
        self.app.session = Session()
        self.app.show(RegistrationScreen)

    def one_time_registration(self):
        from reg_ui import RegDocsScreen, RegSession

        self.app.reg_session = RegSession()
        self.app.show(RegDocsScreen)

    def employment_registration(self):
        from emp_ui import EmpDocsScreen, EmpSession

        self.app.emp_session = EmpSession()
        self.app.show(EmpDocsScreen)

    def placeholder(self):
        messagebox.showinfo("Coming soon", "This option is not available yet.", parent=self)


class RegistrationScreen(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        ttk.Label(self, text="Pay Annual Contribution", style="Title.TLabel").pack(pady=(10, 30))
        ttk.Label(self, text="Registration Number", font=("Segoe UI", 11)).pack()
        self.reg = tk.StringVar(value=app.session.registration_number)
        entry = ttk.Entry(self, textvariable=self.reg, width=36, font=("Segoe UI", 13))
        entry.pack(pady=8)
        entry.focus_set()
        entry.bind("<Return>", lambda e: self.next())
        buttons = ttk.Frame(self)
        buttons.pack(pady=20)
        ttk.Button(buttons, text="Back", command=lambda: app.show(HomeScreen)).pack(side="left", padx=6)
        ttk.Button(buttons, text="Continue", command=self.next).pack(side="left", padx=6)

    def next(self):
        reg = self.reg.get().strip()
        if not reg:
            messagebox.showwarning("Missing", "Please enter the Registration Number.", parent=self)
            return
        self.app.session.registration_number = reg
        self.app.show(DocumentScreen)


class DocumentScreen(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.config_data = load_config()
        self.photo = None
        session = app.session
        ttk.Label(self, text=f"Upload document  -  Reg. No. {session.registration_number}",
                  style="Title.TLabel").pack(anchor="w", pady=(0, 10))

        top = ttk.Frame(self)
        top.pack(fill="x")
        ttk.Button(top, text="Browse...", command=self.browse).pack(side="left")
        self.read_btn = ttk.Button(top, text="Read document", command=self.read_document)
        self.read_btn.pack(side="left", padx=(6, 0))
        self.file_label = ttk.Label(top, text=session.document_path or "No file selected")
        self.file_label.pack(side="left", padx=10)
        self.status = ttk.Label(top, text="", foreground="#1a5fb4")
        self.status.pack(side="right")

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, pady=10)
        self.preview = ttk.Label(body, text="Preview", anchor="center", relief="groove", width=50)
        self.preview.pack(side="left", fill="both", expand=True)
        self.preview.bind("<Button-1>", lambda e: self.open_document())

        form = ttk.LabelFrame(body, text="Document fields (check and correct)", padding=10)
        form.pack(side="left", fill="y", padx=(12, 0))
        self.vars = {}
        for row, (key, label) in enumerate(FIELDS.items()):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", pady=2)
            var = tk.StringVar(value=session.fields.get(key, ""))
            ttk.Entry(form, textvariable=var, width=34).grid(row=row, column=1, pady=2, padx=(8, 0))
            self.vars[key] = var

        buttons = ttk.Frame(self)
        # packed before the body so it keeps its space when the window is short
        buttons.pack(side="bottom", fill="x", before=body)
        ttk.Button(buttons, text="Back", command=lambda: app.show(RegistrationScreen)).pack(side="left")
        self.continue_btn = ttk.Button(buttons, text="Continue", command=self.next)
        self.continue_btn.pack(side="right")

        if session.document_path:
            self.file_label.config(text=session.document_path)
            self.after(50, self.show_preview)

    def browse(self):
        path = filedialog.askopenfilename(
            parent=self, title="Select scanned document",
            filetypes=[("Documents", "*.pdf *.jpg *.jpeg *.png"), ("All files", "*.*")])
        if not path:
            return
        self.app.session.document_path = path
        self.file_label.config(text=path)
        for var in self.vars.values():
            var.set("")
        self.show_preview()
        self.read_document()

    def read_document(self):
        path = self.app.session.document_path
        if not path:
            messagebox.showwarning("Missing", "Please choose the scanned document first.", parent=self)
            return
        extractor = self.config_data.get("extractor", "extractors.manual")
        self.set_busy(True, "Reading document... (up to a minute if Google is busy)")
        outcome = {}

        def work():
            try:
                outcome["fields"] = run_extractor(extractor, path)
            except Exception as exc:
                outcome["error"] = exc

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        self.wait_for(thread, outcome)

    def wait_for(self, thread, outcome):
        if thread.is_alive():
            self.after(200, self.wait_for, thread, outcome)
            return
        self.set_busy(False, "")
        error = outcome.get("error")
        if isinstance(error, MissingApiKey):
            if ask_api_key(self, error):
                self.read_document()
            return
        if error:
            messagebox.showerror("Could not read document", str(error), parent=self)
            return
        note = reader_note(outcome["fields"])
        found = 0
        for key, value in outcome["fields"].items():
            if value and key in self.vars:
                self.vars[key].set(value)
                found += 1
        self.status.config(text=f"Read {found} of {len(FIELDS)} fields - please check them{note}")

    def set_busy(self, busy, message):
        state = "disabled" if busy else "normal"
        self.read_btn.config(state=state)
        self.continue_btn.config(state=state)
        self.status.config(text=message)

    def show_preview(self):
        path = Path(self.app.session.document_path)
        suffix = path.suffix.lower()
        try:
            if suffix == ".pdf":
                img = pdfium.PdfDocument(str(path))[0].render(scale=2).to_pil()
            elif suffix in IMAGE_TYPES:
                img = Image.open(path)
            else:
                raise ValueError("no preview")
        except Exception:
            self.photo = None
            self.preview.config(image="", text=f"{path.name}\n(click to open)")
            return
        self.update_idletasks()
        box_w = max(self.preview.winfo_width(), 300)
        box_h = max(self.preview.winfo_height(), 400)
        img.thumbnail((box_w, box_h))
        self.photo = ImageTk.PhotoImage(img)
        self.preview.config(image=self.photo, text="")

    def open_document(self):
        if self.app.session.document_path:
            os.startfile(self.app.session.document_path)

    def next(self):
        session = self.app.session
        if not session.document_path or not Path(session.document_path).exists():
            messagebox.showwarning("Missing", "Please upload the scanned document.", parent=self)
            return
        session.fields = {key: var.get().strip() for key, var in self.vars.items()}
        save_session(session)
        self.app.show(RunScreen)


class RunScreen(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        app.compact(True)
        ttk.Label(self, text="Pay Annual Contribution - running in Firefox",
                  font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(0, 6))
        self.log = tk.Text(self, height=10, state="disabled", font=("Consolas", 9), wrap="word")
        self.log.pack(fill="both", expand=True)
        buttons = ttk.Frame(self)
        buttons.pack(pady=10)
        self.bridge = UIBridge(app, self.add_status)
        self.controls = RunButtons(buttons, self.bridge, self.add_status)
        self.home_btn = ttk.Button(buttons, text="Home", state="disabled", command=lambda: app.show(HomeScreen))
        self.home_btn.pack(side="left", padx=6)
        threading.Thread(target=self.worker, daemon=True).start()

    def add_status(self, msg):
        self.log.config(state="normal")
        self.log.insert("end", f"{time.strftime('%H:%M:%S')}  {msg}\n")
        self.log.see("end")
        self.log.config(state="disabled")

    def worker(self):
        from automation import run_annual_contribution

        session = self.app.session
        started = time.monotonic()
        try:
            result = run_annual_contribution(load_config(), session, self.bridge)
        except Exception as exc:
            result = {"status": "error", "error": f"{type(exc).__name__}: {exc}".splitlines()[0][:500]}
            self.bridge.warn("Could not run automation", result["error"])
        log_run({
            "action": "pay_annual_contribution",
            "registration_number": session.registration_number,
            "document": session.document_path,
            "status": result.get("status"),
            "village_input": session.fields.get("village", ""),
            "village_match": result.get("village_match", ""),
            "village_score": result.get("village_score", ""),
            "filled": "; ".join(f"{k}={v}" for k, v in result.get("filled", [])),
            "mismatches": "; ".join(f"{k}: site={c} doc={d}" for k, c, d in result.get("mismatches", [])),
            "skipped": "; ".join(result.get("skipped", [])),
            "site_messages": " | ".join(result.get("site_messages", [])),
            "error": result.get("error", ""),
            "duration_s": round(time.monotonic() - started, 1),
        })
        self.bridge.status(f"Finished: {result.get('status')}. Logged to logs/log.csv")
        self.bridge._call(lambda: (self.app.compact(False), self.home_btn.config(state="normal"),
                                   self.controls.finished()))
        self.bridge.active = False


if __name__ == "__main__":
    App().mainloop()
