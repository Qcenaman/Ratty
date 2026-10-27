"""Ratty - lightweight mouse & keyboard macro recorder.

Hotkeys (global): F9 = start/stop recording, F10 = play/stop, F8 = stop everything, Esc = abort playback.
"""
import json
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

from pynput import keyboard, mouse
from pynput.keyboard import Key, KeyCode
from pynput.mouse import Button

HK_RECORD = Key.f9
HK_PLAY = Key.f10
HK_ABORT = Key.esc
HK_STOP = Key.f8
HOTKEYS = {HK_RECORD, HK_PLAY, HK_ABORT, HK_STOP}
MOVE_MIN_INTERVAL = 0.01  # seconds between recorded mouse moves


def key_to_str(k):
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


class Ratty:
    def __init__(self, root):
        self.root = root
        self.events = []  # [delay_since_prev, type, *args]
        self.recording = False
        self.playing = False
        self.stop_play = threading.Event()
        self.last_t = 0.0
        self.last_move_t = 0.0
        self.status_text = "Ready"
        self.kb = keyboard.Controller()
        self.ms = mouse.Controller()
        self._build_ui()
        self.kl = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)
        self.ml = mouse.Listener(on_move=self._on_move, on_click=self._on_click, on_scroll=self._on_scroll)
        self.kl.start()
        self.ml.start()
        self._poll()

    # ---------- UI ----------
    def _build_ui(self):
        r = self.root
        r.title("Ratty")
        r.resizable(False, False)
        r.attributes("-topmost", True)
        f = ttk.Frame(r, padding=8)
        f.grid()

        self.rec_btn = ttk.Button(f, text="Record (F9)", command=self.toggle_record)
        self.rec_btn.grid(row=0, column=0, sticky="ew", padx=2, pady=2)
        self.play_btn = ttk.Button(f, text="Play (F10)", command=self.toggle_play)
        self.play_btn.grid(row=0, column=1, sticky="ew", padx=2, pady=2)
        self.stop_btn = ttk.Button(f, text="Stop (F8)", command=self.stop_all)
        self.stop_btn.grid(row=1, column=0, columnspan=2, sticky="ew", padx=2, pady=2)
        ttk.Button(f, text="Save", command=self.save).grid(row=2, column=0, sticky="ew", padx=2, pady=2)
        ttk.Button(f, text="Load", command=self.load).grid(row=2, column=1, sticky="ew", padx=2, pady=2)

        self.rec_moves = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text="Record mouse movement", variable=self.rec_moves).grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(6, 2))

        ttk.Label(f, text="Repeat (0 = loop):").grid(row=4, column=0, sticky="w")
        self.repeat = tk.IntVar(value=1)
        ttk.Spinbox(f, from_=0, to=9999, textvariable=self.repeat, width=7).grid(row=4, column=1, sticky="w")

        ttk.Label(f, text="Speed:").grid(row=5, column=0, sticky="w")
        self.speed = tk.DoubleVar(value=1.0)
        self.speed_lbl = ttk.Label(f, text="1.0x", width=5)
        self.speed_lbl.grid(row=6, column=1, sticky="e")
        ttk.Scale(f, from_=0.25, to=5.0, variable=self.speed, length=170,
                  command=lambda v: self.speed_lbl.config(text=f"{float(v):.2f}x")).grid(
            row=5, column=1, sticky="ew")

        self.status = ttk.Label(f, text="Ready", foreground="gray")
        self.status.grid(row=6, column=0, sticky="w", pady=(6, 0))

    def _poll(self):
        self.status.config(text=self.status_text)
        self.rec_btn.config(text="Stop (F9)" if self.recording else "Record (F9)")
        self.play_btn.config(text="Stop (F10)" if self.playing else "Play (F10)")
        self.stop_btn.state(["!disabled"] if (self.recording or self.playing) else ["disabled"])
        self.root.after(100, self._poll)

    # ---------- Recording ----------
    def _add(self, *ev):
        now = time.perf_counter()
        self.events.append([now - self.last_t, *ev])
        self.last_t = now

    def stop_all(self):
        if self.recording:
            self.recording = False
            self.status_text = f"Recorded {len(self.events)} events"
        if self.playing:
            self.stop_play.set()

    def toggle_record(self):
        if self.playing:
            return
        if self.recording:
            self.recording = False
            self.status_text = f"Recorded {len(self.events)} events"
        else:
            self.events = []
            self.last_t = self.last_move_t = time.perf_counter()
            self.recording = True
            self.status_text = "Recording..."

    def _on_press(self, k):
        if k == HK_RECORD:
            self.root.after(0, self.toggle_record)
            return
        if k == HK_PLAY:
            self.root.after(0, self.toggle_play)
            return
        if k == HK_STOP:
            self.root.after(0, self.stop_all)
            return
        if k == HK_ABORT:
            if self.playing:
                self.stop_play.set()
            return
        if self.recording:
            s = key_to_str(k)
            if s:
                self._add("kd", s)

    def _on_release(self, k):
        if k in HOTKEYS:
            return
        if self.recording:
            s = key_to_str(k)
            if s:
                self._add("ku", s)

    def _on_move(self, x, y):
        if self.recording and self.rec_moves.get():
            now = time.perf_counter()
            if now - self.last_move_t >= MOVE_MIN_INTERVAL:
                self.last_move_t = now
                self._add("mv", x, y)

    def _on_click(self, x, y, button, pressed):
        if self.recording:
            self._add("md" if pressed else "mu", x, y, button.name)

    def _on_scroll(self, x, y, dx, dy):
        if self.recording:
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
                self.status_text = f"Playing {n}" + ("" if reps == 0 else f"/{reps}") + "  (Esc stops)"
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

    # ---------- Save / Load ----------
    def save(self):
        if not self.events:
            messagebox.showinfo("Ratty", "Nothing recorded yet.")
            return
        p = filedialog.asksaveasfilename(defaultextension=".rat", filetypes=[("Ratty macro", "*.rat"), ("JSON", "*.json")])
        if p:
            with open(p, "w", encoding="utf-8") as fh:
                json.dump({"version": 1, "events": self.events}, fh)
            self.status_text = "Saved"

    def load(self):
        if self.recording or self.playing:
            return
        p = filedialog.askopenfilename(filetypes=[("Ratty macro", "*.rat"), ("JSON", "*.json"), ("All", "*.*")])
        if p:
            try:
                with open(p, encoding="utf-8") as fh:
                    self.events = json.load(fh)["events"]
                self.status_text = f"Loaded {len(self.events)} events"
            except Exception as e:
                messagebox.showerror("Ratty", f"Couldn't load macro:\n{e}")


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
