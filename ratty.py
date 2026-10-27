"""Ratty - lightweight mouse & keyboard macro recorder.

Default hotkeys (change them in Settings): F9 = record w/ move, F7 = record click coords,
F10 = play/stop, F8 = stop everything, Esc = abort playback.
"""
import ctypes
import json
import os
import random
import shutil
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

import ratty_util as U
from ratty_util import (combo_label, hk_token, key_to_str, keystr_label, str_to_key,
                        token_label, MODIFIERS, WIN)
from ratty_overlay import ColorPicker, RegionPicker

U.set_dpi_aware()

from pynput import keyboard, mouse
from pynput.mouse import Button

APP_DIR = os.path.dirname(os.path.abspath(__file__))
MACRO_DIR = os.path.join(APP_DIR, "macros")
SETTINGS_FILE = os.path.join(APP_DIR, "settings.json")
MACRO_EXT = ".rat"
UNSAVED = "(unsaved recording)"

ACTIONS = [
    ("record_move", "Record w/ Move"),
    ("record_clicks", "Record Click Coords"),
    ("play", "Play / Stop playback"),
    ("stop", "Stop everything"),
    ("abort", "Abort playback"),
]
DEFAULT_HOTKEYS = {
    "record_move": ["Key.f9"], "record_clicks": ["Key.f7"],
    "play": ["Key.f10"], "stop": ["Key.f8"], "abort": ["Key.esc"],
}
DEFAULTS = {
    "hotkeys": DEFAULT_HOTKEYS,
    "macro_hotkeys": {},          # macro name -> combo
    "default_speed": 1.0,
    "default_repeat": 1,
    "jitter": 0.0,                # 0..1 humanize amount
    "play_countdown": 0,          # seconds
    "restore_mouse": False,
    "loop_delay": 0.0,            # seconds between repeats
    "stop_after_min": 0,          # 0 = no limit
    "move_interval": 10,          # ms between recorded moves
    "rec_countdown": 0,
    "trim_idle": True,
    "ignore_keyboard": False,
    "relative_coords": False,
    "always_on_top": True,
    "start_with_windows": False,
    "minimize_to_tray": False,
    "hide_on_play": True,
}


def load_settings():
    s = json.loads(json.dumps(DEFAULTS))
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        for k, v in data.items():
            if k in s:
                if k == "hotkeys":
                    for a, c in (v or {}).items():
                        if a in s["hotkeys"] and isinstance(c, list):
                            s["hotkeys"][a] = c
                else:
                    s[k] = v
    except Exception:
        pass
    return s


def save_settings(s):
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(s, fh, indent=2)
    os.replace(tmp, SETTINGS_FILE)


class Ratty:
    def __init__(self, root):
        self.root = root
        os.makedirs(MACRO_DIR, exist_ok=True)
        self.s = load_settings()
        self.events = []
        self.current_name = None
        self.recording = False
        self.rec_mode = "move"
        self.rec_window = None          # window origin info captured at record start
        self.playing = False
        self.stop_play = threading.Event()
        self.last_t = 0.0
        self.last_move_t = 0.0
        self.status_text = "Ready"
        self.held = set()
        self.suppress = set()
        self.rec_marks = {}
        self.own_rects = []
        self.capture = None
        self.lock = threading.RLock()
        self.settings_win = None
        self.editor_win = None
        self.trigger_win = None
        self.tray = None
        self.mini = False
        self.kb = keyboard.Controller()
        self.ms = mouse.Controller()
        self._build_ui()
        self._apply_window_opts()
        self.kl = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)
        self.ml = mouse.Listener(on_move=self._on_move, on_click=self._on_click, on_scroll=self._on_scroll)
        self.kl.start()
        self.ml.start()
        self.refresh_macros()
        self._sync_startup()
        if self.s["minimize_to_tray"]:
            self._init_tray()
        self._poll()

    # ---------- UI ----------
    def _build_ui(self):
        r = self.root
        r.title("Ratty")
        r.resizable(False, False)
        self.full = ttk.Frame(r, padding=8)
        self.full.grid(row=0, column=0)
        f = self.full
        f.columnconfigure(0, weight=1, uniform="c")
        f.columnconfigure(1, weight=1, uniform="c")

        top = ttk.Frame(f)
        top.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        top.columnconfigure(1, weight=1)
        ttk.Label(top, text="Macro:").grid(row=0, column=0, padx=(2, 4))
        self.macro_var = tk.StringVar()
        self.macro_box = ttk.Combobox(top, textvariable=self.macro_var, state="readonly",
                                      width=20, postcommand=self.refresh_macros)
        self.macro_box.grid(row=0, column=1, sticky="ew", padx=2)
        self.macro_box.bind("<<ComboboxSelected>>", self._on_macro_selected)
        self.menu_btn = ttk.Button(top, text="⋮", width=3, command=self._macro_menu)
        self.menu_btn.grid(row=0, column=2, padx=(2, 0))
        self.mmenu = tk.Menu(r, tearoff=0)
        self.mmenu.add_command(label="Rename...", command=self._rename_macro)
        self.mmenu.add_command(label="Delete", command=self._delete_macro)
        self.mmenu.add_command(label="Set hotkey...", command=self._set_macro_hotkey)
        self.mmenu.add_separator()
        self.mmenu.add_command(label="Edit steps...", command=self.open_editor)

        self.move_btn = ttk.Button(f, command=lambda: self.toggle_record("move"))
        self.move_btn.grid(row=1, column=0, sticky="ew", padx=2, pady=2)
        self.click_btn = ttk.Button(f, command=lambda: self.toggle_record("clicks"))
        self.click_btn.grid(row=1, column=1, sticky="ew", padx=2, pady=2)

        self.play_btn = tk.Button(f, command=self.toggle_play, font=("Segoe UI", 11, "bold"),
                                  bg="#16a34a", fg="white", activebackground="#15803d",
                                  relief="flat", height=2, bd=0)
        self.play_btn.grid(row=2, column=0, columnspan=2, sticky="ew", padx=2, pady=(4, 2))
        self.stop_btn = ttk.Button(f, command=self.stop_all)
        self.stop_btn.grid(row=3, column=0, sticky="ew", padx=2, pady=2)
        ttk.Button(f, text="Save", command=self.save).grid(row=3, column=1, sticky="ew", padx=2, pady=2)
        ttk.Button(f, text="Import...", command=self.import_macro).grid(row=4, column=0, sticky="ew", padx=2, pady=2)
        ttk.Button(f, text="Color Trigger...", command=self.open_trigger).grid(
            row=4, column=1, sticky="ew", padx=2, pady=2)
        ttk.Button(f, text="Settings", command=self.open_settings).grid(row=5, column=0, sticky="ew", padx=2, pady=2)
        ttk.Button(f, text="Mini ▸", command=self.toggle_mini).grid(row=5, column=1, sticky="ew", padx=2, pady=2)

        ttk.Separator(f, orient="horizontal").grid(row=6, column=0, columnspan=2, sticky="ew", pady=6)
        ttk.Label(f, text="Repeat (0 = loop):").grid(row=7, column=0, sticky="w")
        self.repeat = tk.IntVar(value=int(self.s["default_repeat"]))
        ttk.Spinbox(f, from_=0, to=99999, textvariable=self.repeat, width=8).grid(row=7, column=1, sticky="w")
        ttk.Label(f, text="Speed:").grid(row=8, column=0, sticky="w")
        self.speed = tk.DoubleVar(value=float(self.s["default_speed"]))
        sp = ttk.Frame(f)
        sp.grid(row=8, column=1, sticky="ew")
        sp.columnconfigure(0, weight=1)
        ttk.Scale(sp, from_=0.25, to=5.0, variable=self.speed,
                  command=lambda v: self.speed_ent.delete(0, "end") or self.speed_ent.insert(0, f"{float(v):.2f}")
                  ).grid(row=0, column=0, sticky="ew")
        self.speed_ent = ttk.Entry(sp, width=5)
        self.speed_ent.insert(0, f"{float(self.s['default_speed']):.2f}")
        self.speed_ent.grid(row=0, column=1, padx=(4, 0))
        self.speed_ent.bind("<Return>", self._speed_typed)
        self.speed_ent.bind("<FocusOut>", self._speed_typed)

        self.status = ttk.Label(f, text="Ready", foreground="gray")
        self.status.grid(row=9, column=0, sticky="w", pady=(6, 0))
        self.coords = ttk.Label(f, text="", foreground="gray", font=("Consolas", 9))
        self.coords.grid(row=9, column=1, sticky="e", pady=(6, 0))

        # mini bar
        self.minibar = ttk.Frame(r, padding=4)
        self.mini_play = tk.Button(self.minibar, text="▶", command=self.toggle_play,
                                   bg="#16a34a", fg="white", relief="flat", width=3)
        self.mini_play.grid(row=0, column=0, padx=1)
        ttk.Button(self.minibar, text="■", width=3, command=self.stop_all).grid(row=0, column=1, padx=1)
        ttk.Button(self.minibar, text="●", width=3, command=lambda: self.toggle_record("move")).grid(
            row=0, column=2, padx=1)
        self.mini_status = ttk.Label(self.minibar, text="Ready", width=10, font=("Segoe UI", 8))
        self.mini_status.grid(row=0, column=3, padx=2)
        ttk.Button(self.minibar, text="◂", width=3, command=self.toggle_mini).grid(row=0, column=4, padx=1)

    def _apply_window_opts(self):
        self.root.attributes("-topmost", bool(self.s["always_on_top"]))

    def _speed_typed(self, _e=None):
        try:
            v = max(0.05, min(5.0, float(self.speed_ent.get())))
            self.speed.set(v)
        except ValueError:
            v = self.speed.get()
        self.speed_ent.delete(0, "end")
        self.speed_ent.insert(0, f"{v:.2f}")

    def _hk(self, a):
        return combo_label(self.s["hotkeys"].get(a, []))

    def toggle_mini(self):
        self.mini = not self.mini
        if self.mini:
            self.full.grid_remove()
            self.minibar.grid(row=0, column=0)
        else:
            self.minibar.grid_remove()
            self.full.grid(row=0, column=0)
        self.root.geometry("")

    def _poll(self):
        self.status.config(text=self.status_text)
        self.mini_status.config(text=self.status_text[:12])
        rec_move = self.recording and self.rec_mode == "move"
        rec_clicks = self.recording and self.rec_mode == "clicks"
        self.move_btn.config(text=("■ Stop" if rec_move else "● Record w/ Move") + f" ({self._hk('record_move')})")
        self.click_btn.config(text=("■ Stop" if rec_clicks else "● Record Clicks") + f" ({self._hk('record_clicks')})")
        if self.playing:
            self.play_btn.config(text=f"■ Stop ({self._hk('play')})", bg="#dc2626", activebackground="#b91c1c")
            self.mini_play.config(text="■", bg="#dc2626")
        else:
            self.play_btn.config(text=f"▶ Play ({self._hk('play')})", bg="#16a34a", activebackground="#15803d")
            self.mini_play.config(text="▶", bg="#16a34a")
        self.stop_btn.config(text=f"Stop ({self._hk('stop')})")
        busy = self.recording or self.playing
        self.stop_btn.state(["!disabled"] if busy else ["disabled"])
        self.macro_box.state(["disabled"] if busy else ["!disabled", "readonly"])
        try:
            self.coords.config(text="x:%d y:%d" % self.ms.position)
        except Exception:
            pass
        self.own_rects = [r for r in (self._rect(self.root), self._rect(self.settings_win),
                                      self._rect(self.editor_win), self._rect(self.trigger_win)) if r]
        self.root.after(80, self._poll)

    def _rect(self, w):
        try:
            if w is None or not w.winfo_exists() or not w.winfo_viewable():
                return None
            if WIN:
                hwnd = int(w.wm_frame(), 16)
                rc = ctypes.wintypes.RECT()
                if ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rc)):
                    return (rc.left, rc.top, rc.right, rc.bottom)
            x, y = w.winfo_rootx(), w.winfo_rooty()
            return (x, y, x + w.winfo_width(), y + w.winfo_height())
        except Exception:
            return None

    def _in_own_window(self, x, y):
        return any(l <= x < r and t <= y < b for l, t, r, b in self.own_rects)

    # ---------- Macro list / files ----------
    def _macro_names(self):
        try:
            return sorted((os.path.splitext(n)[0] for n in os.listdir(MACRO_DIR)
                           if n.lower().endswith(MACRO_EXT)), key=str.lower)
        except OSError:
            return []

    def refresh_macros(self):
        self.macro_box["values"] = self._macro_names()

    def _path(self, name):
        return os.path.join(MACRO_DIR, name + MACRO_EXT)

    def _read_macro(self, name):
        with open(self._path(name), encoding="utf-8") as fh:
            return json.load(fh)

    def _on_macro_selected(self, _e=None):
        name = self.macro_var.get()
        if self.recording or self.playing or name == self.current_name:
            return
        try:
            self.events = self._read_macro(name)["events"]
            self.current_name = name
            self.status_text = f"Loaded {len(self.events)} events"
        except Exception as e:
            messagebox.showerror("Ratty", f"Couldn't load macro:\n{e}", parent=self.root)
            self.macro_var.set(self.current_name or "")

    def _macro_menu(self):
        if self.recording or self.playing:
            return
        self.mmenu.tk_popup(self.menu_btn.winfo_rootx(),
                            self.menu_btn.winfo_rooty() + self.menu_btn.winfo_height())

    def _clean_name(self, name):
        return "".join(c for c in (name or "").strip() if c not in '<>:"/\\|?*').strip()

    def save(self):
        if self.recording or self.playing or not self._ensure_events():
            return
        name = self._clean_name(simpledialog.askstring(
            "Save macro", "Macro name:", parent=self.root, initialvalue=self.current_name or ""))
        if not name:
            return
        if os.path.exists(self._path(name)) and name != self.current_name and not messagebox.askyesno(
                "Ratty", f'"{name}" already exists. Overwrite it?', parent=self.root):
            return
        self._write_macro(name, self.events)
        self.current_name = name
        self.refresh_macros()
        self.macro_var.set(name)
        self.status_text = f'Saved "{name}"'

    def _write_macro(self, name, events):
        with open(self._path(name), "w", encoding="utf-8") as fh:
            json.dump({"version": 3, "events": events}, fh)

    def _ensure_events(self):
        if not self.events:
            messagebox.showinfo("Ratty", "Nothing recorded yet.", parent=self.root)
            return False
        return True

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
        dest = self._path(name)
        if os.path.abspath(dest) != os.path.abspath(p):
            if os.path.exists(dest) and not messagebox.askyesno(
                    "Ratty", f'"{name}" already exists. Overwrite it?', parent=self.root):
                return
            shutil.copyfile(p, dest)
        self.events, self.current_name = events, name
        self.refresh_macros()
        self.macro_var.set(name)
        self.status_text = f"Loaded {len(events)} events"

    def _rename_macro(self):
        name = self.macro_var.get()
        if not name or name == UNSAVED or not os.path.exists(self._path(name)):
            return
        new = self._clean_name(simpledialog.askstring("Rename", "New name:", parent=self.root, initialvalue=name))
        if not new or new == name:
            return
        if os.path.exists(self._path(new)) and not messagebox.askyesno(
                "Ratty", f'"{new}" exists. Overwrite?', parent=self.root):
            return
        os.replace(self._path(name), self._path(new))
        mh = self.s["macro_hotkeys"]
        if name in mh:
            mh[new] = mh.pop(name)
            self._save_settings()
        if self.current_name == name:
            self.current_name = new
        self.refresh_macros()
        self.macro_var.set(new)

    def _delete_macro(self):
        name = self.macro_var.get()
        if not name or name == UNSAVED or not os.path.exists(self._path(name)):
            return
        if not messagebox.askyesno("Ratty", f'Delete "{name}"?', parent=self.root):
            return
        os.remove(self._path(name))
        if self.s["macro_hotkeys"].pop(name, None) is not None:
            self._save_settings()
        if self.current_name == name:
            self.current_name = None
            self.macro_var.set("")
        self.refresh_macros()

    def _set_macro_hotkey(self):
        name = self.macro_var.get()
        if not name or name == UNSAVED or not os.path.exists(self._path(name)):
            messagebox.showinfo("Ratty", "Select a saved macro first.", parent=self.root)
            return
        HotkeyCapture(self, f'Hotkey to play "{name}"',
                      self.s["macro_hotkeys"].get(name, []), lambda c: self._save_macro_hotkey(name, c))

    def _save_macro_hotkey(self, name, combo):
        mh = self.s["macro_hotkeys"]
        if combo:
            for other in list(mh):
                if set(mh[other]) == set(combo):
                    del mh[other]
            mh[name] = combo
        else:
            mh.pop(name, None)
        self._save_settings()
        self.status_text = f'Hotkey {combo_label(combo) if combo else "cleared"} for "{name}"'

    def _save_settings(self):
        try:
            save_settings(self.s)
        except Exception as e:
            self.status_text = f"Settings save failed: {e}"

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
        if self.s["trim_idle"]:
            self._trim_idle()
        self.current_name = None
        self.macro_var.set(UNSAVED)
        self.status_text = f"Recorded {len(self.events)} events"

    def _trim_idle(self):
        if self.events:
            self.events[0][0] = 0.0

    def toggle_record(self, mode="move"):
        if self.playing:
            return
        with self.lock:
            if self.recording:
                self._finish_recording()
                return
        if self.s["rec_countdown"] > 0:
            self._countdown(self.s["rec_countdown"], lambda: self._begin_record(mode), "Recording in")
        else:
            self._begin_record(mode)

    def _begin_record(self, mode):
        with self.lock:
            self.rec_mode = mode
            self.events = []
            self.rec_marks.clear()
            self.rec_window = None
            if self.s["relative_coords"] and WIN:
                self.rec_window = U.window_info_at(*self.ms.position)
            if mode == "move":
                x, y = self.ms.position
                self.events.append([0.0, "mv", x, y])
            self.last_t = self.last_move_t = time.perf_counter()
            self.recording = True
            tag = " (relative)" if self.rec_window else ""
            self.status_text = ("Recording w/ move" if mode == "move" else "Recording clicks") + tag + "..."

    def _rel_for(self, x, y):
        if not self.rec_window:
            return None
        return [self.rec_window["exe"], self.rec_window["title"],
                x - self.rec_window["wx"], y - self.rec_window["wy"]]

    def _drop_combo_presses(self, combo):
        idxs = sorted({self.rec_marks.pop(t) for t in list(combo) if t in self.rec_marks}, reverse=True)
        for i in idxs:
            if i < len(self.events):
                d = self.events[i][0]
                del self.events[i]
                if i < len(self.events):
                    self.events[i][0] += d
                self.rec_marks = {t: (j - 1 if j > i else j) for t, j in self.rec_marks.items()}

    # ---------- input handling ----------
    def _match_hotkey(self, token):
        best = None
        pool = list(self.s["hotkeys"].items())
        for name, combo in self.s["macro_hotkeys"].items():
            pool.append(("macro:" + name, combo))
        for action, combo in pool:
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
        elif action.startswith("macro:"):
            self._play_named(action[6:])

    def _on_press(self, k, injected=False):
        if injected:
            return
        t, s = hk_token(k), key_to_str(k)
        if t and s:
            self._press(t, (lambda: None) if self.s["ignore_keyboard"] else (lambda: self._add("kd", s)))

    def _on_release(self, k, injected=False):
        if injected:
            return
        t, s = hk_token(k), key_to_str(k)
        if t and s:
            self._release(t, (lambda: None) if self.s["ignore_keyboard"] else (lambda: self._add("ku", s)))

    def _on_click(self, x, y, button, pressed, injected=False):
        if injected or button == Button.unknown:
            return
        t = "mouse:" + button.name
        rec = lambda: self._add("md" if pressed else "mu", x, y, button.name, self._rel_for(x, y))
        if pressed:
            if self._in_own_window(x, y) and self.capture is None:
                with self.lock:
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
        if now - self.last_move_t >= self.s["move_interval"] / 1000.0:
            self.last_move_t = now
            with self.lock:
                self._add("mv", x, y)

    def _on_scroll(self, x, y, dx, dy, injected=False):
        if injected or not self.recording or self._in_own_window(x, y):
            return
        with self.lock:
            self._add("sc", x, y, dx, dy)

    # ---------- countdown ----------
    def _countdown(self, secs, then, prefix="Starting in"):
        ov = tk.Toplevel(self.root)
        ov.overrideredirect(True)
        ov.attributes("-topmost", True)
        try:
            ov.attributes("-alpha", 0.85)
        except tk.TclError:
            pass
        lbl = tk.Label(ov, font=("Segoe UI", 28, "bold"), bg="#111", fg="white", padx=40, pady=20)
        lbl.pack()
        sw, sh = ov.winfo_screenwidth(), ov.winfo_screenheight()
        ov.geometry(f"+{sw // 2 - 120}+{sh // 2 - 60}")

        def tick(n):
            if n <= 0:
                ov.destroy()
                then()
                return
            lbl.config(text=f"{prefix} {n}")
            ov.after(1000, tick, n - 1)
        tick(int(secs))

    # ---------- playback ----------
    def toggle_play(self):
        if self.recording:
            return
        if self.playing:
            self.stop_play.set()
            return
        if not self.events:
            self.status_text = "Nothing to play"
            return
        self._speed_typed()
        if self.s["play_countdown"] > 0:
            self._countdown(self.s["play_countdown"], self._start_play, "Playing in")
        else:
            self._start_play()

    def _play_named(self, name):
        if self.recording or self.playing:
            return
        try:
            self.events = self._read_macro(name)["events"]
            self.current_name = name
            self.macro_var.set(name)
        except Exception as e:
            self.status_text = f"Load failed: {e}"
            return
        self._start_play()

    def _start_play(self):
        try:
            reps = max(0, int(self.repeat.get()))
        except (tk.TclError, ValueError):
            reps = 1
        speed = max(0.05, float(self.speed.get()))
        self.stop_play.clear()
        self.playing = True
        start_pos = self.ms.position if self.s["restore_mouse"] else None
        if self.s["hide_on_play"]:
            self.root.withdraw()
        threading.Thread(target=self._play, args=(list(self.events), reps, speed, start_pos), daemon=True).start()

    def _resolve(self, ev):
        x, y = ev[2], ev[3]
        if len(ev) >= 6 and ev[5]:
            exe, title, dx, dy = ev[5]
            org = U.find_window_origin(exe, title)
            if org:
                return org[0] + dx, org[1] + dy
        return x, y

    def _jittered(self, delay):
        j = self.s["jitter"]
        if j <= 0:
            return delay
        return max(0.0, delay * (1 + random.uniform(-0.5, 0.5) * j))

    def _play(self, events, reps, speed, start_pos):
        held_keys, held_btns = set(), set()
        jit = self.s["jitter"]
        loop_delay = self.s["loop_delay"]
        deadline = time.time() + self.s["stop_after_min"] * 60 if self.s["stop_after_min"] else None
        n = 0
        try:
            while not self.stop_play.is_set() and (reps == 0 or n < reps):
                if deadline and time.time() >= deadline:
                    break
                n += 1
                self.status_text = f"Playing {n}" + ("" if reps == 0 else f"/{reps}")
                for ev in events:
                    if self.stop_play.wait(self._jittered(ev[0]) / speed):
                        break
                    t = ev[1]
                    if t == "mv":
                        self.ms.position = (ev[2], ev[3])
                    elif t in ("md", "mu"):
                        px, py = self._resolve(ev)
                        if jit > 0:
                            px += int(random.uniform(-2, 2) * jit)
                            py += int(random.uniform(-2, 2) * jit)
                        self.ms.position = (px, py)
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
                    elif t == "txt":
                        self.kb.type(ev[2])
                    elif t in ("wait_color", "wait_change"):
                        self._wait_condition(ev)
                if loop_delay and not self.stop_play.is_set() and (reps == 0 or n < reps):
                    if self.stop_play.wait(loop_delay):
                        break
        except Exception as e:
            self.status_text = f"Error: {e}"
        finally:
            for k in held_keys:
                try: self.kb.release(k)
                except Exception: pass
            for b in held_btns:
                try: self.ms.release(b)
                except Exception: pass
            if start_pos:
                try: self.ms.position = start_pos
                except Exception: pass
            self.playing = False
            if self.s["hide_on_play"]:
                self.root.after(0, self._show_main)
            if not self.status_text.startswith("Error"):
                self.status_text = "Stopped" if self.stop_play.is_set() else "Done"

    def _show_main(self):
        if not self.s["minimize_to_tray"] or self.root.state() != "withdrawn" or True:
            self.root.deiconify()
            self._apply_window_opts()

    def _wait_condition(self, ev):
        rect = ev[2]
        tol = ev[-1]
        base = U.b64_to_img(ev[3]) if ev[1] == "wait_change" else None
        color = ev[3] if ev[1] == "wait_color" else None
        while not self.stop_play.is_set():
            img = U.grab(rect)
            if color is not None and U.has_color(img, color, tol):
                return
            if base is not None and U.has_changed(base, img, tol):
                return
            if self.stop_play.wait(0.05):
                return

    # ---------- windows ----------
    def open_settings(self):
        if self.settings_win and self.settings_win.winfo_exists():
            self.settings_win.deiconify(); self.settings_win.lift(); self.settings_win.focus_force()
            return
        SettingsWindow(self)

    def open_editor(self):
        if not self.events:
            messagebox.showinfo("Ratty", "Record or load a macro first.", parent=self.root)
            return
        if self.editor_win and self.editor_win.winfo_exists():
            self.editor_win.deiconify(); self.editor_win.lift(); return
        StepEditor(self)

    def open_trigger(self):
        if self.trigger_win and self.trigger_win.winfo_exists():
            self.trigger_win.deiconify(); self.trigger_win.lift(); self.trigger_win.focus_force()
            return
        ColorTrigger(self)

    # ---------- tray / startup ----------
    def _sync_startup(self):
        if not WIN:
            return
        try:
            pyw = sys.executable.replace("python.exe", "pythonw.exe")
            cmd = f'"{pyw}" "{os.path.join(APP_DIR, "ratty.py")}"'
            U.set_startup(bool(self.s["start_with_windows"]), cmd)
        except Exception:
            pass

    def _init_tray(self):
        try:
            import pystray
            from PIL import Image, ImageDraw
        except Exception:
            return
        img = Image.new("RGB", (64, 64), "#111")
        d = ImageDraw.Draw(img)
        d.ellipse((12, 20, 52, 52), fill="#38bdf8")
        d.ellipse((20, 6, 32, 20), fill="#38bdf8")
        d.ellipse((34, 6, 46, 20), fill="#38bdf8")
        menu = pystray.Menu(
            pystray.MenuItem("Show", lambda: self.root.after(0, self._tray_show), default=True),
            pystray.MenuItem("Play / Stop", lambda: self.root.after(0, self.toggle_play)),
            pystray.MenuItem("Quit", lambda: self.root.after(0, self.quit)))
        self.tray = pystray.Icon("Ratty", img, "Ratty", menu)
        threading.Thread(target=self.tray.run, daemon=True).start()

    def _tray_show(self):
        self.root.deiconify()
        self.root.lift()
        self._apply_window_opts()

    def hide_to_tray(self):
        self.root.withdraw()

    def quit(self):
        self.stop_play.set()
        try: self.kl.stop()
        except Exception: pass
        try: self.ml.stop()
        except Exception: pass
        if self.tray:
            try: self.tray.stop()
            except Exception: pass
        self.root.destroy()

    def on_close(self):
        if self.s["minimize_to_tray"] and self.tray:
            self.hide_to_tray()
        else:
            self.quit()


# =================== Hotkey capture (shared) ===================
class HotkeyCapture:
    """Modal-ish prompt that captures a key/mouse combo and calls on_done(list)."""

    def __init__(self, app, title, current, on_done):
        self.app = app
        self.on_done = on_done
        if app.recording or app.playing:
            messagebox.showinfo("Ratty", "Stop recording/playback first.", parent=app.root)
            return
        w = self.win = tk.Toplevel(app.root)
        w.title(title)
        w.resizable(False, False)
        w.attributes("-topmost", True)
        w.protocol("WM_DELETE_WINDOW", self._cancel)
        ttk.Label(w, text=title, padding=10).grid(row=0, column=0, columnspan=2)
        self.lbl = ttk.Label(w, text=combo_label(current), relief="sunken", width=28,
                             anchor="center", padding=(4, 6))
        self.lbl.grid(row=1, column=0, columnspan=2, padx=10)
        ttk.Button(w, text="Clear", command=lambda: self._done([])).grid(row=2, column=0, pady=8, padx=10, sticky="ew")
        ttk.Button(w, text="Cancel", command=self._cancel).grid(row=2, column=1, pady=8, padx=10, sticky="ew")
        self.msg = ttk.Label(w, text="Press a key, combo, or mouse button...", foreground="gray")
        self.msg.grid(row=3, column=0, columnspan=2, pady=(0, 8))
        with app.lock:
            app.held.clear(); app.suppress.clear()
            app.capture = {"tokens": set(), "cb": self._update}
        w.focus_force()

    def _update(self, done):
        cap = self.app.capture
        if cap is None:
            return
        if not done:
            self.lbl.config(text=combo_label(cap["tokens"]) + " ...")
            return
        toks = set(cap["tokens"])
        with self.app.lock:
            self.app.capture = None
            self.app.suppress |= self.app.held
        if toks in ({"mouse:left"}, {"mouse:right"}):
            self.msg.config(text="Left/right click alone isn't allowed. Add a key (e.g. Ctrl).")
            self._rearm()
            return
        self._done(sorted(toks))

    def _rearm(self):
        with self.app.lock:
            self.app.held.clear()
            self.app.capture = {"tokens": set(), "cb": self._update}

    def _done(self, combo):
        self._clear_capture()
        self.on_done(combo)
        self.win.destroy()

    def _cancel(self):
        self._clear_capture()
        self.win.destroy()

    def _clear_capture(self):
        with self.app.lock:
            self.app.capture = None


# =================== Settings window ===================
class SettingsWindow:
    def __init__(self, app):
        self.app = app
        self.s = app.s
        w = self.win = tk.Toplevel(app.root)
        app.settings_win = w
        w.title("Ratty Settings")
        w.resizable(False, False)
        w.attributes("-topmost", True)
        w.protocol("WM_DELETE_WINDOW", self.close)
        nb = ttk.Notebook(w)
        nb.grid(padx=8, pady=8)
        self._hotkeys_tab(nb)
        self._playback_tab(nb)
        self._recording_tab(nb)
        self._general_tab(nb)

    def _hotkeys_tab(self, nb):
        hk = ttk.Frame(nb, padding=10)
        nb.add(hk, text="Hotkeys")
        ttk.Label(hk, text="Click Set, then press a key, combo (Ctrl+Shift+F1) or mouse button (Ctrl+Mouse 4).",
                  foreground="gray").grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 8))
        self.labels = {}
        for i, (action, text) in enumerate(ACTIONS, start=1):
            ttk.Label(hk, text=text).grid(row=i, column=0, sticky="w", padx=(0, 10), pady=2)
            lbl = ttk.Label(hk, width=22, relief="sunken", anchor="center", padding=(4, 2))
            lbl.grid(row=i, column=1, pady=2)
            self.labels[action] = lbl
            ttk.Button(hk, text="Set", width=6, command=lambda a=action: self._set(a)).grid(row=i, column=2, padx=(6, 2))
            ttk.Button(hk, text="Clear", width=6, command=lambda a=action: self._apply(a, [])).grid(row=i, column=3)
        ttk.Button(hk, text="Reset to defaults", command=self._reset).grid(
            row=len(ACTIONS) + 1, column=0, columnspan=4, sticky="e", pady=(10, 0))
        self.msg = ttk.Label(hk, text="", foreground="#b05000")
        self.msg.grid(row=len(ACTIONS) + 2, column=0, columnspan=4, sticky="w", pady=(6, 0))
        self._refresh()

    def _refresh(self):
        for a, l in self.labels.items():
            l.config(text=combo_label(self.s["hotkeys"].get(a, [])))

    def _set(self, action):
        HotkeyCapture(self.app, f'Set hotkey: {dict(ACTIONS)[action]}',
                      self.s["hotkeys"].get(action, []), lambda c: self._apply(action, c))

    def _apply(self, action, combo):
        msg = ""
        if combo:
            for other, c in self.s["hotkeys"].items():
                if other != action and set(c) == set(combo):
                    self.s["hotkeys"][other] = []
                    msg = f'{combo_label(combo)} taken from "{dict(ACTIONS)[other]}".'
            for mname, c in list(self.s["macro_hotkeys"].items()):
                if set(c) == set(combo):
                    del self.s["macro_hotkeys"][mname]
                    msg = f'{combo_label(combo)} taken from macro "{mname}".'
        self.s["hotkeys"][action] = combo
        self.app._save_settings()
        if self.win.winfo_exists():
            self.msg.config(text=msg)
            self._refresh()

    def _reset(self):
        self.s["hotkeys"] = json.loads(json.dumps(DEFAULT_HOTKEYS))
        self.app._save_settings()
        self.msg.config(text="")
        self._refresh()

    def _num_row(self, parent, row, text, key, frm, to, inc, is_float=False):
        ttk.Label(parent, text=text).grid(row=row, column=0, sticky="w", pady=2)
        var = (tk.DoubleVar if is_float else tk.IntVar)(value=self.s[key])
        sb = ttk.Spinbox(parent, from_=frm, to=to, increment=inc, textvariable=var, width=8)
        sb.grid(row=row, column=1, sticky="w", padx=6, pady=2)

        def on_change(*_):
            try:
                self.s[key] = (float if is_float else int)(var.get())
                self.app._save_settings()
            except (ValueError, tk.TclError):
                pass
        var.trace_add("write", on_change)
        return var

    def _check_row(self, parent, row, text, key, cb=None):
        var = tk.BooleanVar(value=bool(self.s[key]))

        def on_change(*_):
            self.s[key] = bool(var.get())
            self.app._save_settings()
            if cb:
                cb()
        var.trace_add("write", on_change)
        ttk.Checkbutton(parent, text=text, variable=var).grid(row=row, column=0, columnspan=2, sticky="w", pady=1)
        return var

    def _playback_tab(self, nb):
        f = ttk.Frame(nb, padding=10)
        nb.add(f, text="Playback")
        self._num_row(f, 0, "Default speed (x):", "default_speed", 0.05, 5.0, 0.05, True)
        self._num_row(f, 1, "Default repeat (0 = loop):", "default_repeat", 0, 99999, 1)
        self._num_row(f, 2, "Loop delay (seconds):", "loop_delay", 0, 3600, 0.5, True)
        self._num_row(f, 3, "Stop after (minutes, 0 = off):", "stop_after_min", 0, 10000, 1)
        self._num_row(f, 4, "Humanize jitter (0 = off, 1 = max):", "jitter", 0.0, 1.0, 0.05, True)
        self._num_row(f, 5, "Countdown before play (sec):", "play_countdown", 0, 30, 1)
        self._check_row(f, 6, "Restore mouse position after playback", "restore_mouse")
        self._check_row(f, 7, "Hide Ratty window during playback", "hide_on_play")

    def _recording_tab(self, nb):
        f = ttk.Frame(nb, padding=10)
        nb.add(f, text="Recording")
        self._num_row(f, 0, "Mouse move sample interval (ms):", "move_interval", 1, 200, 1)
        self._num_row(f, 1, "Countdown before record (sec):", "rec_countdown", 0, 30, 1)
        self._check_row(f, 2, "Trim idle time at start", "trim_idle")
        self._check_row(f, 3, "Ignore keyboard input", "ignore_keyboard")
        self._check_row(f, 4, "Record coordinates relative to window", "relative_coords")
        ttk.Label(f, text="Relative coords replay a macro even if the window moved.",
                  foreground="gray").grid(row=5, column=0, columnspan=2, sticky="w", pady=(4, 0))

    def _general_tab(self, nb):
        f = ttk.Frame(nb, padding=10)
        nb.add(f, text="General")
        self._check_row(f, 0, "Always on top", "always_on_top", self.app._apply_window_opts)
        self._check_row(f, 1, "Start with Windows", "start_with_windows", self.app._sync_startup)
        self._check_row(f, 2, "Minimize to tray on close", "minimize_to_tray", self._tray_changed)
        ttk.Separator(f, orient="horizontal").grid(row=3, column=0, columnspan=2, sticky="ew", pady=8)
        ttk.Label(f, text=f"Macros folder:\n{MACRO_DIR}", foreground="gray").grid(
            row=4, column=0, columnspan=2, sticky="w")
        ttk.Button(f, text="Open macros folder", command=self._open_folder).grid(
            row=5, column=0, sticky="w", pady=(6, 0))
        if WIN:
            ttk.Button(f, text="Restart as administrator", command=self._run_as_admin).grid(
                row=5, column=1, sticky="e", pady=(6, 0))
            ttk.Label(f, text="Admin lets Ratty control apps that run elevated.",
                      foreground="gray").grid(row=6, column=0, columnspan=2, sticky="w", pady=(4, 0))

    def _tray_changed(self):
        if self.s["minimize_to_tray"] and not self.app.tray:
            self.app._init_tray()

    def _open_folder(self):
        try:
            if WIN:
                os.startfile(MACRO_DIR)
            else:
                import subprocess
                subprocess.Popen(["xdg-open", MACRO_DIR])
        except Exception:
            pass

    def _run_as_admin(self):
        try:
            pyw = sys.executable.replace("python.exe", "pythonw.exe")
            params = f'"{os.path.join(APP_DIR, "ratty.py")}"'
            rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", pyw, params, APP_DIR, 1)
            if rc > 32:
                self.app.quit()
        except Exception as e:
            messagebox.showerror("Ratty", f"Couldn't elevate:\n{e}", parent=self.win)

    def close(self):
        with self.app.lock:
            self.app.capture = None
        self.app.settings_win = None
        self.win.destroy()


# =================== Step editor ===================
class StepEditor:
    def __init__(self, app):
        self.app = app
        w = self.win = tk.Toplevel(app.root)
        app.editor_win = w
        w.title("Edit Steps")
        w.attributes("-topmost", True)
        w.protocol("WM_DELETE_WINDOW", self.close)
        cols = ("#", "delay", "type", "detail")
        self.tree = ttk.Treeview(w, columns=cols, show="headings", height=16, selectmode="extended")
        for c, width in zip(cols, (40, 70, 70, 260)):
            self.tree.heading(c, text=c.title())
            self.tree.column(c, width=width, anchor="w")
        self.tree.grid(row=0, column=0, columnspan=5, padx=8, pady=8, sticky="nsew")
        sb = ttk.Scrollbar(w, orient="vertical", command=self.tree.yview)
        sb.grid(row=0, column=5, sticky="ns", pady=8)
        self.tree.configure(yscrollcommand=sb.set)
        ttk.Button(w, text="Delete", command=self._delete).grid(row=1, column=0, padx=4, pady=(0, 8))
        ttk.Button(w, text="Edit delay...", command=self._edit_delay).grid(row=1, column=1, padx=4, pady=(0, 8))
        ttk.Button(w, text="Add text step...", command=self._add_text).grid(row=1, column=2, padx=4, pady=(0, 8))
        ttk.Button(w, text="Revert", command=self._reload).grid(row=1, column=3, padx=4, pady=(0, 8))
        ttk.Button(w, text="Apply", command=self._apply).grid(row=1, column=4, padx=4, pady=(0, 8))
        self.work = [list(e) for e in app.events]
        self._fill()

    def _detail(self, ev):
        t = ev[1]
        if t == "mv":
            return f"move to {ev[2]},{ev[3]}"
        if t in ("md", "mu"):
            rel = "  (rel)" if len(ev) >= 6 and ev[5] else ""
            return f"{'press' if t == 'md' else 'release'} {ev[4]} @ {ev[2]},{ev[3]}{rel}"
        if t == "sc":
            return f"scroll {ev[4]},{ev[5]} @ {ev[2]},{ev[3]}"
        if t in ("kd", "ku"):
            return f"{'down' if t == 'kd' else 'up'} {keystr_label(ev[2])}"
        if t == "txt":
            return f'type "{ev[2]}"'
        if t == "wait_color":
            return f"wait until {U.hex_color(ev[3])} in box"
        if t == "wait_change":
            return "wait for change in box"
        return t

    def _fill(self):
        self.tree.delete(*self.tree.get_children())
        for i, ev in enumerate(self.work):
            self.tree.insert("", "end", iid=str(i), values=(i + 1, f"{ev[0]:.3f}", ev[1], self._detail(ev)))

    def _sel(self):
        return sorted(int(i) for i in self.tree.selection())

    def _delete(self):
        for i in reversed(self._sel()):
            del self.work[i]
        self._fill()

    def _edit_delay(self):
        sel = self._sel()
        if not sel:
            return
        v = simpledialog.askfloat("Delay", "Seconds before this step:", parent=self.win,
                                  initialvalue=self.work[sel[0]][0], minvalue=0.0)
        if v is None:
            return
        for i in sel:
            self.work[i][0] = v
        self._fill()

    def _add_text(self):
        txt = simpledialog.askstring("Add text step", "Text to type:", parent=self.win)
        if not txt:
            return
        sel = self._sel()
        at = sel[-1] + 1 if sel else len(self.work)
        self.work.insert(at, [0.0, "txt", txt])
        self._fill()

    def _reload(self):
        self.work = [list(e) for e in self.app.events]
        self._fill()

    def _apply(self):
        self.app.events = [list(e) for e in self.work]
        if self.app.current_name:
            self.app.macro_var.set(UNSAVED)
            self.app.current_name = None
        self.app.status_text = f"Edited: {len(self.app.events)} events (Save to keep)"
        self.close()

    def close(self):
        self.app.editor_win = None
        self.win.destroy()


# =================== Color / area trigger ===================
class ColorTrigger:
    def __init__(self, app):
        self.app = app
        self.rect = None
        self.target_color = None
        self.base_img = None
        self.watch = None
        w = self.win = tk.Toplevel(app.root)
        app.trigger_win = w
        w.title("Color Trigger")
        w.resizable(False, False)
        w.attributes("-topmost", True)
        w.protocol("WM_DELETE_WINDOW", self.close)
        p = ttk.Frame(w, padding=10)
        p.grid()

        ttk.Label(p, text="1. Watch area").grid(row=0, column=0, sticky="w")
        self.area_lbl = ttk.Label(p, text="(none)", width=24, relief="sunken", anchor="center")
        self.area_lbl.grid(row=0, column=1, padx=6, pady=2)
        ttk.Button(p, text="Draw box", command=self._draw).grid(row=0, column=2)

        self.mode = tk.StringVar(value="color")
        ttk.Radiobutton(p, text="Fire when area becomes this color:", variable=self.mode,
                        value="color").grid(row=1, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self.color_lbl = tk.Label(p, text="(pick)", width=12, relief="sunken")
        self.color_lbl.grid(row=1, column=1, sticky="e", pady=(10, 0))
        ttk.Button(p, text="Pick color", command=self._pick).grid(row=1, column=2, pady=(10, 0))
        ttk.Radiobutton(p, text="Fire on any color change in the area", variable=self.mode,
                        value="change").grid(row=2, column=0, columnspan=3, sticky="w")

        ttk.Label(p, text="Tolerance (0-255):").grid(row=3, column=0, sticky="w", pady=(10, 0))
        self.tol = tk.IntVar(value=30)
        ttk.Spinbox(p, from_=0, to=255, textvariable=self.tol, width=6).grid(row=3, column=1, sticky="w", pady=(10, 0))

        ttk.Label(p, text="Then:").grid(row=4, column=0, sticky="w", pady=(10, 0))
        self.action = tk.StringVar(value="click")
        ttk.Radiobutton(p, text="Click center of box", variable=self.action, value="click").grid(
            row=4, column=1, columnspan=2, sticky="w", pady=(10, 0))
        row5 = ttk.Frame(p)
        row5.grid(row=5, column=1, columnspan=2, sticky="w")
        ttk.Radiobutton(row5, text="Play macro:", variable=self.action, value="macro").grid(row=0, column=0)
        self.macro_sel = ttk.Combobox(row5, state="readonly", width=16, values=self.app._macro_names())
        self.macro_sel.grid(row=0, column=1, padx=4)

        self.repeat_v = tk.BooleanVar(value=False)
        ttk.Checkbutton(p, text="Keep watching after it fires (repeat)", variable=self.repeat_v).grid(
            row=6, column=0, columnspan=3, sticky="w", pady=(10, 0))

        bar = ttk.Frame(p)
        bar.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(12, 0))
        self.start_btn = ttk.Button(bar, text="Start watching", command=self._toggle)
        self.start_btn.grid(row=0, column=0, sticky="w")
        self.state_lbl = ttk.Label(bar, text="", foreground="gray")
        self.state_lbl.grid(row=0, column=1, padx=10)

    def _draw(self):
        self.win.withdraw()
        RegionPicker(self.app.root, self._got_rect)

    def _got_rect(self, rect):
        self.win.deiconify()
        self.win.lift()
        if rect:
            self.rect = rect
            self.area_lbl.config(text=f"{rect[2]-rect[0]}x{rect[3]-rect[1]} @ {rect[0]},{rect[1]}")
            try:
                self.base_img = U.grab(rect)
            except Exception:
                self.base_img = None

    def _pick(self):
        self.win.withdraw()
        ColorPicker(self.app.root, self._got_color)

    def _got_color(self, color):
        self.win.deiconify()
        self.win.lift()
        if color:
            self.target_color = color
            self.color_lbl.config(text=U.hex_color(color), bg=U.hex_color(color),
                                  fg="white" if sum(color) < 384 else "black")
            self.mode.set("color")

    def _toggle(self):
        if self.watch:
            self.watch.set()
            self.watch = None
            self.start_btn.config(text="Start watching")
            self.state_lbl.config(text="Stopped")
            return
        if not self.rect:
            self.state_lbl.config(text="Draw a box first.")
            return
        if self.mode.get() == "color" and not self.target_color:
            self.state_lbl.config(text="Pick a color first.")
            return
        if self.mode.get() == "change":
            try:
                self.base_img = U.grab(self.rect)
            except Exception:
                pass
        self.watch = threading.Event()
        self.start_btn.config(text="Stop watching")
        self.state_lbl.config(text="Watching...")
        threading.Thread(target=self._run, args=(self.watch,), daemon=True).start()

    def _run(self, stop):
        rect = self.rect
        tol = self.tol.get()
        mode = self.mode.get()
        color = self.target_color
        base = self.base_img
        while not stop.is_set():
            try:
                img = U.grab(rect)
            except Exception:
                break
            hit = (U.has_color(img, color, tol) if mode == "color"
                   else (base is not None and U.has_changed(base, img, tol)))
            if hit:
                self.app.root.after(0, self._fire)
                if not self.repeat_v.get():
                    break
                while not stop.is_set():  # wait until condition clears, then resume
                    try:
                        img2 = U.grab(rect)
                    except Exception:
                        break
                    cleared = (not U.has_color(img2, color, tol) if mode == "color"
                               else (base is not None and not U.has_changed(base, img2, tol)))
                    if cleared:
                        break
                    if stop.wait(0.1):
                        break
            if stop.wait(0.05):
                break
        self.app.root.after(0, lambda: self.state_lbl.config(text="Stopped")
                            if self.win.winfo_exists() else None)
        if not stop.is_set():
            self.watch = None
            self.app.root.after(0, lambda: self.start_btn.config(text="Start watching")
                                if self.win.winfo_exists() else None)

    def _fire(self):
        if self.action.get() == "macro":
            name = self.macro_sel.get()
            if name:
                self.app._play_named(name)
        else:
            l, t, r, b = self.rect
            self.app.ms.position = ((l + r) // 2, (t + b) // 2)
            self.app.ms.click(Button.left)
        if self.win.winfo_exists():
            self.state_lbl.config(text="Fired " + time.strftime("%H:%M:%S"))

    def close(self):
        if self.watch:
            self.watch.set()
            self.watch = None
        self.app.trigger_win = None
        self.win.destroy()


def main():
    root = tk.Tk()
    app = Ratty(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
