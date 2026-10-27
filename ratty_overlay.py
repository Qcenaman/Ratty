"""Fullscreen overlays: drag a box over the screen, and pick a color from a pixel."""
import tkinter as tk

import ratty_util as U


class RegionPicker:
    """Drag a rectangle across all monitors. Calls on_done((l, t, r, b)) or on_done(None)."""

    def __init__(self, master, on_done):
        self.on_done = on_done
        vx, vy, vw, vh = U.virtual_screen()
        self.ox, self.oy = vx, vy
        w = tk.Toplevel(master)
        self.win = w
        w.overrideredirect(True)
        w.geometry(f"{vw}x{vh}+{vx}+{vy}")
        w.attributes("-topmost", True)
        try:
            w.attributes("-alpha", 0.25)
        except tk.TclError:
            pass
        w.config(cursor="crosshair", bg="black")
        self.c = tk.Canvas(w, bg="black", highlightthickness=0, cursor="crosshair")
        self.c.pack(fill="both", expand=True)
        self.c.create_text(vw // 2, 24, fill="white",
                            text="Drag a box over the area  ·  Esc to cancel", font=("Segoe UI", 12))
        self.sx = self.sy = 0
        self.rid = None
        self.c.bind("<Button-1>", self._down)
        self.c.bind("<B1-Motion>", self._move)
        self.c.bind("<ButtonRelease-1>", self._up)
        w.bind("<Escape>", lambda e: self._finish(None))
        w.focus_force()
        self.c.focus_set()

    def _down(self, e):
        self.sx, self.sy = e.x, e.y
        if self.rid:
            self.c.delete(self.rid)
        self.rid = self.c.create_rectangle(e.x, e.y, e.x, e.y, outline="#38bdf8", width=2)

    def _move(self, e):
        if self.rid:
            self.c.coords(self.rid, self.sx, self.sy, e.x, e.y)

    def _up(self, e):
        l, t = min(self.sx, e.x), min(self.sy, e.y)
        r, b = max(self.sx, e.x), max(self.sy, e.y)
        if r - l < 3 or b - t < 3:
            self._finish(None)
            return
        self._finish((l + self.ox, t + self.oy, r + self.ox, b + self.oy))

    def _finish(self, rect):
        try:
            self.win.destroy()
        except tk.TclError:
            pass
        self.on_done(rect)


class ColorPicker:
    """Move the mouse over the screen and click to grab a pixel color.
    Calls on_done((r, g, b)) or on_done(None)."""

    def __init__(self, master, on_done):
        self.on_done = on_done
        vx, vy, vw, vh = U.virtual_screen()
        self.ox, self.oy = vx, vy
        w = tk.Toplevel(master)
        self.win = w
        w.overrideredirect(True)
        w.geometry(f"{vw}x{vh}+{vx}+{vy}")
        w.attributes("-topmost", True)
        try:
            w.attributes("-alpha", 0.01)
        except tk.TclError:
            pass
        w.config(cursor="crosshair")
        self.tip = tk.Toplevel(master)
        self.tip.overrideredirect(True)
        self.tip.attributes("-topmost", True)
        self.sw = tk.Label(self.tip, width=18, font=("Consolas", 10), relief="solid", bd=1)
        self.sw.pack()
        w.bind("<Motion>", self._move)
        w.bind("<Button-1>", self._click)
        w.bind("<Escape>", lambda e: self._finish(None))
        w.focus_force()

    def _move(self, e):
        sx, sy = e.x_root, e.y_root
        c = U.pixel_at(sx, sy)
        if c:
            self.sw.config(text=f" {U.hex_color(c)}  {c} ", bg=U.hex_color(c),
                           fg="white" if sum(c) < 384 else "black")
        self.tip.geometry(f"+{sx + 16}+{sy + 16}")

    def _click(self, e):
        self._finish(U.pixel_at(e.x_root, e.y_root))

    def _finish(self, color):
        for wdg in (self.tip, self.win):
            try:
                wdg.destroy()
            except tk.TclError:
                pass
        self.on_done(color)
