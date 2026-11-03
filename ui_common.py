"""Pieces shared by the Tk screens: config loading, the automation-thread bridge and popups."""
import io
import json
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk

from PIL import Image, ImageTk

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"


def load_config():
    with CONFIG_FILE.open(encoding="utf-8") as f:
        return json.load(f)


def ask_api_key(parent, key_file):
    key = simpledialog.askstring(
        "Gemini API key",
        "Paste your free Gemini API key.\n"
        "Get one at https://aistudio.google.com/apikey (sign in with a Google account, "
        "click 'Create API key').\n\nIt is saved in " + Path(key_file).name + " on this PC.",
        parent=parent)
    if not key or not key.strip():
        return False
    Path(key_file).write_text(key.strip(), encoding="utf-8")
    return True


class TextPrompt(tk.Toplevel):
    """Modal popup asking for one line of text, optionally showing an image (e.g. a captcha)."""

    def __init__(self, root, title, message, image_bytes=None):
        super().__init__(root)
        self.title(title)
        self.transient(root)
        self.resizable(False, False)
        self.result = None
        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=message, wraplength=420, justify="left").pack(anchor="w")
        self.photo = None
        if image_bytes:
            img = Image.open(io.BytesIO(image_bytes))
            img = img.resize((img.width * 2, img.height * 2))
            self.photo = ImageTk.PhotoImage(img)
            ttk.Label(frame, image=self.photo, relief="solid").pack(pady=10)
        self.var = tk.StringVar()
        entry = ttk.Entry(frame, textvariable=self.var, width=30, font=("Segoe UI", 14))
        entry.pack(pady=10)
        entry.focus_set()
        entry.bind("<Return>", lambda e: self.ok())
        buttons = ttk.Frame(frame)
        buttons.pack()
        ttk.Button(buttons, text="OK", command=self.ok).pack(side="left", padx=6)
        ttk.Button(buttons, text="Cancel run", command=self.destroy).pack(side="left", padx=6)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.lift()
        self.attributes("-topmost", True)
        self.grab_set()
        root.wait_window(self)

    def ok(self):
        if self.var.get().strip():
            self.result = self.var.get().strip()
            self.destroy()


class UsernamePopup(tk.Toplevel):
    def __init__(self, root, username,
                 text="The account has been created. Keep this username - it is needed to log in."):
        super().__init__(root)
        self.title("Registration successful")
        self.transient(root)
        root.clipboard_clear()
        root.clipboard_append(username)
        frame = ttk.Frame(self, padding=20)
        frame.pack()
        ttk.Label(frame, text=text, wraplength=380).pack(anchor="w")
        var = tk.StringVar(value=username)
        ttk.Entry(frame, textvariable=var, state="readonly", width=24,
                  font=("Segoe UI", 18, "bold"), justify="center").pack(pady=12)
        ttk.Label(frame, text="It has been copied to the clipboard and saved in logs/log.csv.").pack()
        ttk.Button(frame, text="Copy again", command=lambda: (root.clipboard_clear(),
                                                               root.clipboard_append(username))).pack(pady=(10, 4))
        ttk.Button(frame, text="OK - continue", command=self.destroy).pack()
        self.attributes("-topmost", True)
        self.grab_set()
        root.wait_window(self)


class ChoicePrompt(tk.Toplevel):
    """Modal popup to confirm or change a dropdown value, showing all the site's options."""

    def __init__(self, root, title, message, options, current):
        super().__init__(root)
        self.title(title)
        self.transient(root)
        self.resizable(False, False)
        self.result = None
        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=message, wraplength=460, justify="left").pack(anchor="w")
        self.var = tk.StringVar(value=current if current in options else "")
        box = ttk.Combobox(frame, textvariable=self.var, values=options, state="readonly", width=60, height=25)
        box.pack(pady=12)
        box.focus_set()
        buttons = ttk.Frame(frame)
        buttons.pack()
        ttk.Button(buttons, text="OK - continue", command=self.ok).pack(side="left", padx=6)
        ttk.Button(buttons, text="Stop run", command=self.destroy).pack(side="left", padx=6)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.lift()
        self.attributes("-topmost", True)
        self.grab_set()
        root.wait_window(self)

    def ok(self):
        if self.var.get():
            self.result = self.var.get()
            self.destroy()


class WaitNotice(tk.Toplevel):
    """Non-blocking notice: the user works in Firefox while the tool watches the field."""

    def __init__(self, root, title, message, on_choice):
        super().__init__(root)
        self.title(title)
        self.resizable(False, False)
        frame = ttk.Frame(self, padding=14)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=message, wraplength=380, justify="left").pack(anchor="w")
        buttons = ttk.Frame(frame)
        buttons.pack(pady=(12, 0))
        ttk.Button(buttons, text="Skip this field", command=lambda: on_choice("skip")).pack(side="left", padx=6)
        ttk.Button(buttons, text="Stop the run", command=lambda: on_choice("stop")).pack(side="left", padx=6)
        self.protocol("WM_DELETE_WINDOW", lambda: on_choice("skip"))
        self.update_idletasks()
        x = self.winfo_screenwidth() - self.winfo_reqwidth() - 20
        y = self.winfo_screenheight() - 330 - 80 - self.winfo_reqheight() - 40
        self.geometry(f"+{x}+{max(y, 0)}")
        self.attributes("-topmost", True)


class RunButtons:
    """"Stop run" (always available) and "Fill remaining fields" (whenever the tool is not filling)."""

    def __init__(self, parent, bridge, add_status):
        self.bridge, self.add_status = bridge, add_status
        self.stop_btn = ttk.Button(parent, text="Stop run", command=self.request_stop)
        self.stop_btn.pack(side="left", padx=6)
        self.refill_btn = ttk.Button(parent, text="Fill remaining fields", state="disabled",
                                     command=self.request_refill)
        self.refill_btn.pack(side="left", padx=6)
        bridge.on_refill_state = self.set_refill_state

    def set_refill_state(self, available):
        self.refill_btn.config(state="normal" if available else "disabled")

    def request_stop(self):
        self.add_status("Stopping after the current field...")
        self.bridge.request_stop()
        # a refill pressed now starts as soon as the current run has stopped
        self.refill_btn.config(state="normal")

    def request_refill(self):
        self.refill_btn.config(state="disabled")
        self.add_status("Fill remaining fields requested...")
        self.bridge.request_refill()

    def finished(self):
        self.stop_btn.config(state="disabled")
        self.refill_btn.config(state="disabled")


class UIBridge:
    """Lets the automation thread talk to Tk safely through a queue polled on the main thread."""

    def __init__(self, root, on_status):
        self.root = root
        self.on_status = on_status
        self.calls = queue.Queue()
        self.active = True
        self._poll()

    def _poll(self):
        while not self.calls.empty():
            fn, args, holder, done = self.calls.get()
            try:
                holder.append(fn(*args))
            finally:
                done.set()
        if self.active:
            self.root.after(100, self._poll)

    def _call(self, fn, *args):
        holder, done = [], threading.Event()
        self.calls.put((fn, args, holder, done))
        done.wait()
        return holder[0] if holder else None

    def status(self, msg):
        self._call(self.on_status, msg)

    def warn(self, title, msg):
        self._call(lambda: messagebox.showwarning(title, msg, parent=self.root))

    def ask(self, title, msg):
        return bool(self._call(lambda: messagebox.askyesno(title, msg, parent=self.root)))

    def ask_text(self, title, msg, image=None):
        """Returns the text typed, or None if the user cancelled."""
        return self._call(lambda: TextPrompt(self.root, title, msg, image).result)

    def ask_pick(self, title, msg, options, current):
        """The option chosen in a dropdown popup, or None if the user stopped the run."""
        return self._call(lambda: ChoicePrompt(self.root, title, msg, options, current).result)

    def show_username(self, username, text=None):
        self._call(lambda: UsernamePopup(self.root, username, *([text] if text else [])))

    def start_wait(self, title, msg):
        self._wait_choice = None

        def choose(value):
            self._wait_choice = value

        self._notice = self._call(lambda: WaitNotice(self.root, title, msg, choose))

    def wait_choice(self):
        """None while waiting, "skip" or "stop" once the user presses a button."""
        return getattr(self, "_wait_choice", None)

    def end_wait(self):
        notice = getattr(self, "_notice", None)
        if notice is not None:
            self._call(notice.destroy)
            self._notice = None

    # "Fill remaining fields" button: the screen sets on_refill_state to enable/disable it
    on_refill_state = None

    def set_refill_available(self, available):
        if self.on_refill_state:
            self._call(self.on_refill_state, available)

    def request_refill(self):
        self._refill_requested = True

    # "Stop run" button: the automation checks this before every field
    def request_stop(self):
        self._stop_requested = True

    def stop_requested(self):
        return getattr(self, "_stop_requested", False)

    def clear_stop(self):
        self._stop_requested = False

    def take_refill_request(self):
        requested = getattr(self, "_refill_requested", False)
        self._refill_requested = False
        return requested

    def ask_choice(self, title, msg):
        """Yes / No / Cancel: returns True, False or None."""
        return self._call(lambda: messagebox.askyesnocancel(title, msg, parent=self.root))

    def wait_user(self, title, msg):
        self._call(lambda: messagebox.showinfo(title, msg, parent=self.root))
