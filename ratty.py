"""Ratty - lightweight mouse & keyboard macro recorder.

Default hotkeys (change them in Settings): F9 = record w/ move, F7 = record click coords,
F10 = play/stop, F8 = stop everything, Esc = abort playback.
"""
import json
import os
import shutil
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

from pynput import keyboard, mouse
from pynput.keyboard import Key, KeyCode
from pynput.mouse import Button

APP_DIR = os.path.dirname(os.path.abspath(__file__))
MACRO_DIR = os.path.join(APP_DIR, "macros")
SETTINGS_FILE = os.path.join(APP_DIR, "settings.json")
MACRO_EXT = ".rat"
MOVE_MIN_INTERVAL = 0.01  # seconds between recorded mouse moves
UNSAVED = "(unsaved recording)"

ACTIONS = [  # (id, label)
    ("record_move", "Record w/ Move"),
    ("record_clicks", "Record Click Coords"),
    ("play", "Play / Stop playback"),
    ("stop", "Stop everything"),
    ("abort", "Abort playback"),
]
DEFAULT_HOTKEYS = {
    "record_move": ["Key.f9"],
    "record_clicks": ["Key.f7"],
    "play": ["Key.f10"],
    "stop": ["Key.f8"],
    "abort": ["Key.esc"],
}
MODIFIERS = ("Key.ctrl", "Key.alt", "Key.shift", "Key.cmd")


# ---------- key helpers ----------
def key_to_str(k):
    """Exact key id used inside recorded macros."""
    if isinstance(k, Key):
        return "Key." + k.name
    if isinstance(k, KeyCode):
        if k.char is not None:
            return "c:" + k.char
        if k.vk is not None:
            return "vk:" + str(k.vk)
    return None


def str_to_key(s):
    if s.startswith("Key."):
        return Key[s[4:]]
    if s.startswith("c:"):
        return KeyCode.from_char(s[2:])
    if s.startswith("vk:"):
        return KeyCode.from_vk(int(s[3:]))
    raise ValueError(s)


def hk_token(k):
    """Normalized key id used for hotkeys (left/right modifiers merged, layout-independent)."""
    if isinstance(k, Key):
        name = k.name
        for m in ("ctrl", "alt", "shift", "cmd"):
            if name == m or name.startswith(m + "_"):
                return "Key." + m
        return "Key." + name
    if isinstance(k, KeyCode):
        if k.vk is not None:
            return f"vk:{k.vk}"
        if k.char:
            return "c:" + k.char.lower()
    return None


def btn_token(button):
    return "mouse:" + button.name


def token_label(t):
    if t.startswith("Key."):
        n = t[4:]
        special = {"esc": "Esc", "cmd": "Win", "page_up": "PgUp", "page_down": "PgDn",
                   "caps_lock": "CapsLock", "num_lock": "NumLock", "scroll_lock": "ScrollLock",
                   "print_screen": "PrtSc", "backspace": "Backspace"}
        if n in special:
            return special[n]
        if n[0] == "f" and n[1:].isdigit():
            return n.upper()
        return n.replace("_", " ").title()
    if t.startswith("vk:"):
        v = int(t[3:])
        if 48 <= v <= 57 or 65 <= v <= 90:
            return chr(v)
        if 96 <= v <= 105:
            return f"Num{v - 96}"
        return f"VK{v}"
    if t.startswith("c:"):
        return t[2:].upper()
    if t.startswith("mouse:"):
        return {"left": "Mouse Left", "right": "Mouse Right", "middle": "Mouse Middle",
                "x1": "Mouse 4", "x2": "Mouse 5"}.get(t[6:], "Mouse " + t[6:])
    return t


def combo_label(combo):
    if not combo:
        return "(none)"

    def order(t):
        if t in MODIFIERS:
            return (0, MODIFIERS.index(t))
        if t.startswith("mouse:"):
            return (2, t)
        return (1, t)
    return "+".join(token_label(t) for t in sorted(combo, key=order))


# ---------- settings ----------
def load_settings():
    s = {"hotkeys": dict(DEFAULT_HOTKEYS)}
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        for a, combo in data.get("hotkeys", {}).items():
            if a in s["hotkeys"] and isinstance(combo, list):
                s["hotkeys"][a] = combo
    except Exception:
        pass
    return s


def save_settings(s):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as fh:
        json.dump(s, fh, indent=2)


class Ratty:
    def __init__(self, root):
        self.root = root
        os.makedirs(MACRO_DIR, exist_ok=True)
        self.settings = load_settings()
        self.events = []  # [delay_since_prev, type, *args]
        self.current_name = None
        self.recording = False
        self.rec_mode = "move"
        self.playing = False
        self.stop_play = threading.Event()
        self.last_t = 0.0
        self.last_move_t = 0.0
        self.status_text = "Ready"
        self.held = set()        # hotkey tokens physically held right now
        self.suppress = set()    # tokens consumed by a hotkey; their releases aren't recorded
        self.rec_marks = {}      # token -> index of its still-held press event in self.events
        self.own_rects = []      # screen rects of Ratty windows (clicks there aren't recorded)
        self.capture = None      # settings hotkey capture: {"action", "tokens", "cb"}
        self.lock = threading.RLock()
        self.settings_win = None
        self.kb = keyboard.Controller()
        self.ms = mouse.Controller()
        self._build_ui()
        self.kl = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)
        self.ml = mouse.Listener(on_move=self._on_move, on_click=self._on_click, on_scroll=self._on_scroll)
        self.kl.start()
        self.ml.start()
        self.refresh_macros()
        self._poll()

    # ---------- UI ----------
    def _build_ui(self):
        r = self.root
        r.title("Ratty")
        r.resizable(False, False)
        r.attributes("-topmost", True)
        f = ttk.Frame(r, padding=8)
        f.grid()
        f.columnconfigure(0, weight=1, uniform="c")
        f.columnconfigure(1, weight=1, uniform="c")

        top = ttk.Frame(f)
        top.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        top.columnconfigure(1, weight=1)
        ttk.Label(top, text="Macro:").grid(row=0, column=0, padx=(2, 4))
        self.macro_var = tk.StringVar()
        self.macro_box = ttk.Combobox(top, textvariable=self.macro_var, state="readonly",
                                      postcommand=self.refresh_macros)
        self.macro_box.grid(row=0, column=1, sticky="ew", padx=2)
        self.macro_box.bind("<<ComboboxSelected>>", self._on_macro_selected)

        self.move_btn = ttk.Button(f, command=lambda: self.toggle_record("move"))
        self.move_btn.grid(row=1, column=0, sticky="ew", padx=2, pady=2)
        self.click_btn = ttk.Button(f, command=lambda: self.toggle_record("clicks"))
        self.click_btn.grid(row=1, column=1, sticky="ew", padx=2, pady=2)
        self.play_btn = ttk.Button(f, command=self.toggle_play)
        self.play_btn.grid(row=2, column=0, sticky="ew", padx=2, pady=2)
        self.stop_btn = ttk.Button(f, command=self.stop_all)
        self.stop_btn.grid(row=2, column=1, sticky="ew", padx=2, pady=2)
        ttk.Button(f, text="Save", command=self.save).grid(row=3, column=0, sticky="ew", padx=2, pady=2)
        ttk.Button(f, text="Import...", command=self.import_macro).grid(row=3, column=1, sticky="ew", padx=2, pady=2)
        ttk.Button(f, text="Settings", command=self.open_settings).grid(
            row=4, column=0, columnspan=2, sticky="ew", padx=2, pady=2)

        ttk.Label(f, text="Repeat (0 = loop):").grid(row=5, column=0, sticky="w", pady=(6, 0))
        self.repeat = tk.IntVar(value=1)
        ttk.Spinbox(f, from_=0, to=9999, textvariable=self.repeat, width=7).grid(
            row=5, column=1, sticky="w", pady=(6, 0))

        ttk.Label(f, text="Speed:").grid(row=6, column=0, sticky="w")
        self.speed = tk.DoubleVar(value=1.0)
        ttk.Scale(f, from_=0.25, to=5.0, variable=self.speed,
                  command=lambda v: self.speed_lbl.config(text=f"{float(v):.2f}x")).grid(
            row=6, column=1, sticky="ew")
        self.status = ttk.Label(f, text="Ready", foreground="gray")
        self.status.grid(row=7, column=0, sticky="w", pady=(6, 0))
        self.speed_lbl = ttk.Label(f, text="1.00x")
        self.speed_lbl.grid(row=7, column=1, sticky="e", pady=(6, 0))

    def _hk(self, action):
        return combo_label(self.settings["hotkeys"].get(action, []))

    def _poll(self):
        self.status.config(text=self.status_text)
        rec_move = self.recording and self.rec_mode == "move"
        rec_clicks = self.recording and self.rec_mode == "clicks"
        self.move_btn.config(text=("Stop" if rec_move else "Record w/ Move") + f" ({self._hk('record_move')})")
        self.click_btn.config(text=("Stop" if rec_clicks else "Record Click Coords") + f" ({self._hk('record_clicks')})")
        self.play_btn.config(text=("Stop" if self.playing else "Play") + f" ({self._hk('play')})")
        self.stop_btn.config(text=f"Stop ({self._hk('stop')})")
        busy = self.recording or self.playing
        self.stop_btn.state(["!disabled"] if busy else ["disabled"])
        self.macro_box.state(["disabled"] if busy else ["!disabled", "readonly"])
        self.own_rects = [r for r in (self._rect(self.root), self._rect(self.settings_win)) if r]
        self.root.after(100, self._poll)

    def _rect(self, w):
        try:
            if w is None or not w.winfo_exists() or not w.winfo_viewable():
                return None
            if sys.platform == "win32":
                hwnd = int(w.wm_frame(), 16)
                rc = wintypes.RECT()
                if ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rc)):
                    return (rc.left, rc.top, rc.right, rc.bottom)
            x, y = w.winfo_rootx(), w.winfo_rooty()
            return (x, y, x + w.winfo_width(), y + w.winfo_height())
        except Exception:
            return None

    def _in_own_window(self, x, y):
        return any(l <= x < r and t <= y < b for l, t, r, b in self.own_rects)

    # ---------- Macro list ----------
    def _macro_names(self):
        try:
            return sorted((os.path.splitext(n)[0] for n in os.listdir(MACRO_DIR)
                           if n.lower().endswith(MACRO_EXT)), key=str.lower)
        except OSError:
            return []

    def refresh_macros(self):
        self.macro_box["values"] = self._macro_names()

    def _on_macro_selected(self, _e=None):
        name = self.macro_var.get()
        if self.recording or self.playing or name == self.current_name:
            return
        path = os.path.join(MACRO_DIR, name + MACRO_EXT)
        try:
            with open(path, encoding="utf-8") as fh:
                self.events = json.load(fh)["events"]
            self.current_name = name
            self.status_text = f"Loaded {len(self.events)} events"
        except Exception as e:
            messagebox.showerror("Ratty", f"Couldn't load macro:\n{e}", parent=self.root)
            self.macro_var.set(self.current_name or "")

    def save(self):
        if self.recording or self.playing:
            return
        if not self.events:
            messagebox.showinfo("Ratty", "Nothing recorded yet.", parent=self.root)
            return
        name = simpledialog.askstring("Save macro", "Macro name:", parent=self.root,
                                      initialvalue=self.current_name or "")
        if not name:
            return
        name = "".join(c for c in name.strip() if c not in '<>:"/\\|?*').strip()
        if not name:
            return
        path = os.path.join(MACRO_DIR, name + MACRO_EXT)
        if os.path.exists(path) and name != self.current_name:
            if not messagebox.askyesno("Ratty", f'"{name}" already exists. Overwrite it?', parent=self.root):
                return
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"version": 2, "events": self.events}, fh)
        self.current_name = name
        self.refresh_macros()
        self.macro_var.set(name)
        self.status_text = f'Saved "{name}"'

    def import_macro(self):
        if self.recording or self.playing:
            return
        p = filedialog.askopenfilename(parent=self.root,
                                       filetypes=[("Ratty macro", "*.rat"), ("JSON", "*.json"), ("All", "*.*")])
        if not p:
            return
        try:
            with open(p, encoding="utf-8") as fh:
                events = json.load(fh)["events"]
        except Exception as e:
            messagebox.showerror("Ratty", f"Couldn't load macro:\n{e}", parent=self.root)
            return
        name = os.path.splitext(os.path.basename(p))[0]
        dest = os.path.join(MACRO_DIR, name + MACRO_EXT)
        if os.path.abspath(dest) != os.path.abspath(p):
            if os.path.exists(dest) and not messagebox.askyesno(
                    "Ratty", f'"{name}" already exists. Overwrite it?', parent=self.root):
                return
            shutil.copyfile(p, dest)
        self.events = events
        self.current_name = name
        self.refresh_macros()
        self.macro_var.set(name)
        self.status_text = f"Loaded {len(events)} events"

    # ---------- Recording ----------
    def _add(self, *ev):
        now = time.perf_counter()
        self.events.append([now - self.last_t, *ev])
        self.last_t = now

    def stop_all(self):
        if self.recording:
            self._finish_recording()
        if self.playing:
            self.stop_play.set()

    def _finish_recording(self):
        self.recording = False
        self.rec_marks.clear()
        self.current_name = None
        self.macro_var.set(UNSAVED)
        self.status_text = f"Recorded {len(self.events)} events"

    def toggle_record(self, mode="move"):
        if self.playing:
            return
        with self.lock:
            if self.recording:
                self._finish_recording()
                return
            self.rec_mode = mode
            self.events = []
            self.rec_marks.clear()
            if mode == "move":
                # playback always starts from where the mouse was when recording began
                x, y = self.ms.position
                self.events.append([0.0, "mv", x, y])
            self.last_t = self.last_move_t = time.perf_counter()
            self.recording = True
            self.status_text = "Recording w/ move..." if mode == "move" else "Recording clicks..."

    def _drop_combo_presses(self, combo):
        """Remove already-recorded presses that turned out to be part of a hotkey combo."""
        idxs = sorted({self.rec_marks.pop(t) for t in list(combo) if t in self.rec_marks}, reverse=True)
        for i in idxs:
            if i < len(self.events):
                d = self.events[i][0]
                del self.events[i]
                if i < len(self.events):
                    self.events[i][0] += d
                self.rec_marks = {t: (j - 1 if j > i else j) for t, j in self.rec_marks.items()}

    # ---------- Input handling (listener threads) ----------
    def _match_hotkey(self, token):
        best = None
        for action, combo in self.settings["hotkeys"].items():
            c = set(combo)
            if not c or token not in c or not c <= self.held:
                continue
            if any(m in self.held - c for m in MODIFIERS):
                continue
            if action == "abort" and not self.playing:
                continue
            if best is None or len(c) > len(best[1]):
                best = (action, c)
        return best

    def _press(self, token, record):
        """Common press handling. `record` is a callable that records the event."""
        with self.lock:
            new = token not in self.held
            self.held.add(token)
            if self.capture is not None:
                self.capture["tokens"].add(token)
                self.root.after(0, self.capture["cb"], False)
                return
            if new:
                m = self._match_hotkey(token)
                if m:
                    action, combo = m
                    self.suppress |= combo
                    if self.recording:
                        self._drop_combo_presses(combo)
                    self.root.after(0, self._run_action, action)
                    return
            if token in self.suppress or not self.recording:
                return
            record()
            if new:
                self.rec_marks[token] = len(self.events) - 1

    def _release(self, token, record):
        with self.lock:
            self.held.discard(token)
            if self.capture is not None:
                if not self.held & self.capture["tokens"]:
                    self.root.after(0, self.capture["cb"], True)
                return
            if token in self.suppress:
                self.suppress.discard(token)
                return
            if self.recording:
                self.rec_marks.pop(token, None)
                record()

    def _run_action(self, action):
        if action == "record_move":
            self.toggle_record("move")
        elif action == "record_clicks":
            self.toggle_record("clicks")
        elif action == "play":
            self.toggle_play()
        elif action == "stop":
            self.stop_all()
        elif action == "abort":
            if self.playing:
                self.stop_play.set()

    def _on_press(self, k, injected=False):
        if injected:
            return
        t, s = hk_token(k), key_to_str(k)
        if t and s:
            self._press(t, lambda: self._add("kd", s))

    def _on_release(self, k, injected=False):
        if injected:
            return
        t, s = hk_token(k), key_to_str(k)
        if t and s:
            self._release(t, lambda: self._add("ku", s))

    def _on_click(self, x, y, button, pressed, injected=False):
        if injected or button == Button.unknown:
            return
        t = btn_token(button)
        rec = lambda: self._add("md" if pressed else "mu", x, y, button.name)
        if pressed:
            if self._in_own_window(x, y) and self.capture is None:
                with self.lock:  # clicks on Ratty itself: never recorded, never hotkeys
                    self.held.add(t)
                    self.suppress.add(t)
                return
            self._press(t, rec)
        else:
            self._release(t, rec)

    def _on_move(self, x, y, injected=False):
        if injected or not self.recording or self.rec_mode != "move":
            return
        now = time.perf_counter()
        if now - self.last_move_t >= MOVE_MIN_INTERVAL:
            self.last_move_t = now
            with self.lock:
                self._add("mv", x, y)

    def _on_scroll(self, x, y, dx, dy, injected=False):
        if injected or not self.recording or self._in_own_window(x, y):
            return
        with self.lock:
            self._add("sc", x, y, dx, dy)

    # ---------- Playback ----------
    def toggle_play(self):
        if self.recording:
            return
        if self.playing:
            self.stop_play.set()
            return
        if not self.events:
            self.status_text = "Nothing to play"
            return
        try:
            reps = max(0, int(self.repeat.get()))
        except (tk.TclError, ValueError):
            reps = 1
        speed = max(0.05, float(self.speed.get()))
        self.stop_play.clear()
        self.playing = True
        threading.Thread(target=self._play, args=(list(self.events), reps, speed), daemon=True).start()

    def _play(self, events, reps, speed):
        held_keys, held_btns = set(), set()
        n = 0
        try:
            while not self.stop_play.is_set() and (reps == 0 or n < reps):
                n += 1
                self.status_text = f"Playing {n}" + ("" if reps == 0 else f"/{reps}")
                for ev in events:
                    if self.stop_play.wait(ev[0] / speed):
                        break
                    t = ev[1]
                    if t == "mv":
                        self.ms.position = (ev[2], ev[3])
                    elif t in ("md", "mu"):
                        self.ms.position = (ev[2], ev[3])
                        b = Button[ev[4]]
                        if t == "md":
                            self.ms.press(b); held_btns.add(b)
                        else:
                            self.ms.release(b); held_btns.discard(b)
                    elif t == "sc":
                        self.ms.position = (ev[2], ev[3])
                        self.ms.scroll(ev[4], ev[5])
                    elif t in ("kd", "ku"):
                        k = str_to_key(ev[2])
                        if t == "kd":
                            self.kb.press(k); held_keys.add(k)
                        else:
                            self.kb.release(k); held_keys.discard(k)
        except Exception as e:
            self.status_text = f"Error: {e}"
        finally:
            for k in held_keys:
                try: self.kb.release(k)
                except Exception: pass
            for b in held_btns:
                try: self.ms.release(b)
                except Exception: pass
            self.playing = False
            if not self.status_text.startswith("Error"):
                self.status_text = "Stopped" if self.stop_play.is_set() else "Done"

    # ---------- Settings window ----------
    def open_settings(self):
        if self.settings_win is not None and self.settings_win.winfo_exists():
            self.settings_win.deiconify()
            self.settings_win.lift()
            self.settings_win.focus_force()
            return
        SettingsWindow(self)


class SettingsWindow:
    def __init__(self, app):
        self.app = app
        w = self.win = tk.Toplevel(app.root)
        app.settings_win = w
        w.title("Ratty Settings")
        w.resizable(False, False)
        w.attributes("-topmost", True)
        w.protocol("WM_DELETE_WINDOW", self.close)

        nb = ttk.Notebook(w)
        nb.grid(padx=8, pady=8, sticky="nsew")

        # --- Hotkeys tab ---
        hk = ttk.Frame(nb, padding=10)
        nb.add(hk, text="Hotkeys")
        ttk.Label(hk, text="Click Set, then press a key, a key combination or a mouse button.\n"
                           "Combos like Ctrl+Shift+F1 or Ctrl+Mouse 4 work.",
                  foreground="gray").grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 8))
        self.labels = {}
        self.set_btns = {}
        for i, (action, text) in enumerate(ACTIONS, start=1):
            ttk.Label(hk, text=text).grid(row=i, column=0, sticky="w", padx=(0, 10), pady=2)
            lbl = ttk.Label(hk, width=24, relief="sunken", anchor="center", padding=(4, 2))
            lbl.grid(row=i, column=1, pady=2)
            self.labels[action] = lbl
            b = ttk.Button(hk, text="Set", width=6, command=lambda a=action: self.start_capture(a))
            b.grid(row=i, column=2, padx=(6, 2))
            self.set_btns[action] = b
            ttk.Button(hk, text="Clear", width=6, command=lambda a=action: self.set_combo(a, [])).grid(
                row=i, column=3, padx=2)
        ttk.Button(hk, text="Reset to defaults", command=self.reset).grid(
            row=len(ACTIONS) + 1, column=0, columnspan=4, sticky="e", pady=(10, 0))
        self.msg = ttk.Label(hk, text="", foreground="#b05000")
        self.msg.grid(row=len(ACTIONS) + 2, column=0, columnspan=4, sticky="w", pady=(6, 0))
        self.refresh()

        # --- Placeholder tabs ---
        self._placeholder(nb, "Playback", [
            "Default speed", "Default repeat count", "Random delay jitter (humanize)",
            "Countdown before playback", "Restore mouse position after playback"])
        self._placeholder(nb, "Recording", [
            "Mouse movement sample rate", "Countdown before recording",
            "Trim idle time at start/end", "Ignore keyboard input"])
        self._placeholder(nb, "General", [
            "Always on top", "Start with Windows", "Minimize to tray",
            "Macros folder location", "Theme"])

    def _placeholder(self, nb, title, items):
        f = ttk.Frame(nb, padding=10)
        nb.add(f, text=title)
        ttk.Label(f, text="Coming soon", foreground="gray").grid(row=0, column=0, sticky="w", pady=(0, 8))
        for i, t in enumerate(items, start=1):
            cb = ttk.Checkbutton(f, text=t)
            cb.state(["disabled", "!alternate"])
            cb.grid(row=i, column=0, sticky="w", pady=1)

    def refresh(self):
        for action, lbl in self.labels.items():
            lbl.config(text=combo_label(self.app.settings["hotkeys"].get(action, [])))

    def start_capture(self, action):
        app = self.app
        if app.recording or app.playing:
            self.msg.config(text="Stop recording/playback first.")
            return
        self.msg.config(text="")
        with app.lock:
            app.held.clear()
            app.suppress.clear()
            app.capture = {"action": action, "tokens": set(), "cb": self._capture_update}
        self.labels[action].config(text="Press keys / mouse...")
        for b in self.set_btns.values():
            b.state(["disabled"])

    def _capture_update(self, done):
        app = self.app
        cap = app.capture
        if cap is None:
            return
        action = cap["action"]
        if not done:
            self.labels[action].config(text=combo_label(cap["tokens"]) + " ...")
            return
        with app.lock:
            app.capture = None
            app.suppress |= app.held  # anything still down shouldn't leak into recordings
        for b in self.set_btns.values():
            b.state(["!disabled"])
        tokens = cap["tokens"]
        if tokens in ({"mouse:left"}, {"mouse:right"}):
            self.msg.config(text="Left/right click alone can't be a hotkey (add a key, e.g. Ctrl). Cancelled.")
            self.refresh()
            return
        self.set_combo(action, sorted(tokens))

    def set_combo(self, action, combo):
        hot = self.app.settings["hotkeys"]
        msg = ""
        if combo:
            for other, c in hot.items():
                if other != action and set(c) == set(combo):
                    hot[other] = []
                    msg = f"{combo_label(combo)} was taken from \"{dict(ACTIONS)[other]}\"."
        hot[action] = combo
        self._save()
        self.msg.config(text=msg)
        self.refresh()

    def reset(self):
        self.app.settings["hotkeys"] = {a: list(c) for a, c in DEFAULT_HOTKEYS.items()}
        self._save()
        self.msg.config(text="")
        self.refresh()

    def _save(self):
        try:
            save_settings(self.app.settings)
        except Exception as e:
            messagebox.showerror("Ratty", f"Couldn't save settings:\n{e}", parent=self.win)

    def close(self):
        with self.app.lock:
            self.app.capture = None
        self.app.settings_win = None
        self.win.destroy()


def main():
    root = tk.Tk()
    app = Ratty(root)

    def on_close():
        app.stop_play.set()
        app.kl.stop()
        app.ml.stop()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
