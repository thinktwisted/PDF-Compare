"""PDF Compare - lightweight two-document comparison tool.

Usage:
    python app.py [a.pdf [b.pdf]] [--auto]
    python app.py --selftest a.pdf b.pdf [--text-only]

Design notes (low RAM):
  * Pages are rasterized lazily; only pages intersecting the viewport
    (+/- 1) keep canvas items, and PhotoImages are LRU-capped.
  * Diff runs in a worker thread with its own document handles.
  * Change rectangles are stored in PDF points, so zoom never re-diffs.
"""
from __future__ import annotations

import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, simpledialog, ttk

import pymupdf

import engine

ZOOM_MIN, ZOOM_MAX = 0.4, 3.0
GAP = 14
PAGE_BG = "#6e6e6e"
IM_PIXEL_BUDGET = 20_000_000   # max cached raster pixels per pane

COLORS = {
    ("text", "replace"): ("#ffd54f", "#e65100"),
    ("text", "insert"):  ("#a5d6a7", "#1b5e20"),
    ("text", "delete"):  ("#ef9a9a", "#b71c1c"),
    ("visual", "replace"): ("#90caf9", "#0d47a1"),
    ("pages", "insert"): ("#ce93d8", "#4a148c"),
    ("pages", "delete"): ("#ef9a9a", "#b71c1c"),
    ("pages", "replace"): ("#ffd54f", "#e65100"),
}


def _fit_path(p):
    return os.path.abspath(os.path.expanduser(p))


class DocView(tk.Frame):
    """Scrollable canvas holding one PDF as stacked page bitmaps."""

    def __init__(self, master, app, side):
        super().__init__(master, bg=PAGE_BG, bd=1, relief="sunken")
        self.app = app
        self.side = side                     # 'a' | 'b'
        self.path = None
        self.doc = None
        self.sizes = []                      # (w, h) points per page
        self.ys = []
        self.total = 1
        self.zoom = 1.0
        self.canvas = tk.Canvas(self, bg=PAGE_BG, highlightthickness=0,
                                yscrollcommand=self._on_yview,
                                xscrollcommand=self._on_xview,
                                yscrollincrement=24, xscrollincrement=24)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<MouseWheel>", self._wheel)
        self.canvas.bind("<Configure>", lambda e: self.draw())
        self.items = {}                      # page -> [canvas ids]
        self.im_cache = {}                   # (page, zoom) -> PhotoImage
        self.im_order = []                   # LRU order, oldest first
        self.im_px = {}                      # key -> raster pixels
        self.hl = {}                         # page -> [(rect, colors, w)]
        self.password = None
        self.generation = 0                  # bumped on every doc change

    # -- document ------------------------------------------------------------
    def load(self, path, password=None):
        self.close_doc()
        doc = engine._open(path, password)   # validates PDF + password
        self.doc = doc
        self.path = path
        self.password = password
        self.sizes = [(p.rect.width, p.rect.height) for p in self.doc]
        self.relayout()

    def close_doc(self):
        if self.doc is not None:
            self.doc.close()
        self.generation += 1
        self.doc = None
        self.path = None
        self.password = None
        self.sizes = []
        self.items.clear()
        self.im_cache.clear()
        self.im_px.clear()
        self.im_order.clear()
        self.hl.clear()
        self.canvas.delete("all")

    # -- layout ----------------------------------------------------------------
    def relayout(self):
        self.canvas.delete("all")
        self.items.clear()
        z = self.zoom
        self.ys = []
        y = GAP
        for (w, h) in self.sizes:
            self.ys.append(y)
            y += h * z + GAP
        self.total = max(y, 1)
        maxw = max((w * z for w, _ in self.sizes), default=1)
        self.canvas.configure(scrollregion=(0, 0, maxw, self.total))
        self.draw()

    def set_zoom(self, z):
        z = max(ZOOM_MIN, min(ZOOM_MAX, z))
        if z == self.zoom:
            return
        top = self.canvas.yview()[0] * self.total
        self.zoom = z
        self.relayout()
        self.canvas.yview_moveto(min(1.0, top / self.total))

    def _px_h(self, p):
        return self.sizes[p][1] * self.zoom

    def page_at_y(self, y):
        if not self.ys:
            return 0, 0.0
        lo, hi = 0, len(self.ys) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.ys[mid] <= y:
                lo = mid
            else:
                hi = mid - 1
        return lo, y - self.ys[lo]

    # -- scrolling ---------------------------------------------------------------
    def _on_yview(self, first, last):
        self.app.vscroll.set(first, last)
        self.after_idle(self.draw)

    def _on_xview(self, first, last):
        self.app.hscroll.set(first, last)

    def _wheel(self, e):
        n = max(1, abs(e.delta) // 60)
        self.canvas.yview_scroll(n if e.delta < 0 else -n, "units")
        self.app.on_scroll(self)

    def view_frac_for(self, other, first):
        """Translate my scroll fraction into other's (page-aligned)."""
        if not self.sizes or not other.ys:
            return first
        y = first * self.total
        p, off = self.page_at_y(y)
        if p >= len(other.ys):
            return 1.0
        return min(1.0, (other.ys[p] + min(off, other._px_h(p))) / other.total)

    def scroll_to(self, page, rect=None):
        if not self.sizes:
            return
        page = min(page, len(self.sizes) - 1)
        y = self.ys[page]
        if rect:
            y += ((rect[1] + rect[3]) / 2.0) * self.zoom - 120
        self.canvas.yview_moveto(max(0.0, min(1.0, y / self.total)))
        self.draw()

    # -- drawing --------------------------------------------------------------
    def set_highlights(self, changes):
        self.hl = {}
        attr = "a_rects" if self.side == "a" else "b_rects"
        page_attr = "a_page" if self.side == "a" else "b_page"
        for c in changes:
            p = getattr(c, page_attr)
            if p is None:
                continue
            cols = COLORS[(c.kind, c.op)]
            for r in getattr(c, attr):
                self.hl.setdefault(p, []).append(
                    (r, cols, 3 if c.kind == "pages" else 1))
        # force recreation of on-canvas items so new rectangles appear even
        # on pages already drawn (PhotoImages stay cached)
        self.canvas.delete("all")
        self.items.clear()
        self.draw()

    def _ensure_page(self, p):
        if p in self.items:
            return
        z = self.zoom
        key = (p, z)
        img = self.im_cache.get(key)
        if img is None:
            pix = self.doc[p].get_pixmap(matrix=pymupdf.Matrix(z, z),
                                        colorspace=pymupdf.csRGB, alpha=False)
            img = tk.PhotoImage(data=pix.tobytes("png"))
            self.im_cache[key] = img
            self.im_px[key] = pix.width * pix.height
            self.im_order.append(key)
            del pix
            self._trim_cache(key)
        else:
            self.im_order.remove(key)
            self.im_order.append(key)
        ids = [self.canvas.create_image(0, self.ys[p], image=img, anchor="nw")]
        for (r, cols, w) in self.hl.get(p, ()):
            ids.append(self.canvas.create_rectangle(
                r[0] * z, self.ys[p] + r[1] * z, r[2] * z, self.ys[p] + r[3] * z,
                fill=cols[0], stipple="gray50", outline=cols[1], width=w,
                tags="hl"))
        self.items[p] = ids

    def _trim_cache(self, protect):
        """Enforce the per-pane raster budget, never evicting visible pages."""
        total = sum(self.im_px.values())
        if total <= IM_PIXEL_BUDGET:
            return
        inuse = set(self.items)
        for k in list(self.im_order):
            if total <= IM_PIXEL_BUDGET:
                break
            if k == protect or (k[1] == self.zoom and k[0] in inuse):
                continue
            total -= self.im_px.pop(k, 0)
            self.im_order.remove(k)
            self.im_cache.pop(k, None)

    def draw(self):
        if not self.winfo_exists() or not self.sizes:
            return
        v0, v1 = self.canvas.yview()
        y0, y1 = v0 * self.total, v1 * self.total
        lo, _ = self.page_at_y(max(0, y0 - 2 * GAP))
        hi, _ = self.page_at_y(min(self.total - 1, y1 + 2 * GAP))
        lo, hi = max(0, lo - 1), min(len(self.sizes) - 1, hi + 1)
        for p in list(self.items):
            if not lo <= p <= hi:
                for i in self.items.pop(p):
                    self.canvas.delete(i)
        for p in range(lo, hi + 1):
            self._ensure_page(p)

    def flash(self, page, rects):
        if not self.ys:
            return
        p = min(page, len(self.ys) - 1)
        ids = []
        for r in rects:
            ids.append(self.canvas.create_rectangle(
                r[0] * self.zoom, self.ys[p] + r[1] * self.zoom,
                r[2] * self.zoom, self.ys[p] + r[3] * self.zoom,
                outline="red", width=3))
        if ids:
            self.after(1300, lambda: [self.canvas.delete(i) for i in ids])


class ChangesWindow(tk.Toplevel):
    """Pop-out list of changes; selecting an item jumps to it."""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Changes")
        self.geometry("640x460")
        top = tk.Frame(self)
        top.pack(fill="x", padx=6, pady=4)
        tk.Label(top, text="Filter:").pack(side="left")
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_: self.refresh())
        tk.Entry(top, textvariable=self.filter_var, width=30).pack(side="left")
        self.count_lbl = tk.Label(top, text="")
        self.count_lbl.pack(side="right")
        tk.Label(self, text="Click an item to jump; double-click flashes the change. Ctrl+PgDn/PgUp cycles.",
                 anchor="w").pack(fill="x", padx=6)

        self.tree = ttk.Treeview(self, columns=("kind", "loc", "summary"),
                                 show="tree headings", selectmode="browse")
        self.tree.heading("#0", text="#")
        self.tree.heading("kind", text="Type")
        self.tree.heading("loc", text="Location")
        self.tree.heading("summary", text="Change")
        self.tree.column("#0", width=52, anchor="e", stretch=False)
        self.tree.column("kind", width=120, stretch=False)
        self.tree.column("loc", width=110, stretch=False)
        self.tree.column("summary", width=320)
        for key, col in (("text:replace", "#e65100"), ("text:insert", "#1b5e20"),
                         ("text:delete", "#b71c1c"), ("visual:replace", "#0d47a1"),
                         ("pages:insert", "#4a148c"), ("pages:delete", "#b71c1c")):
            self.tree.tag_configure(key, foreground=col)
        sb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.tree.bind("<Double-1>", lambda e: self._jump())
        self.tree.bind("<Return>", lambda e: self._jump())
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._preview())
        self.tree.bind("<MouseWheel>",
                       lambda e: self.tree.yview_scroll(-e.delta // 120, "units"))
        self.protocol("WM_DELETE_WINDOW", self.withdraw)
        self.row_to_change = {}

    def refresh(self):
        sel = self.tree.selection()
        sel_cid = None
        if sel:
            c = self.row_to_change.get(sel[0])
            sel_cid = c.cid if c else None
        self.tree.delete(*self.tree.get_children())
        self.row_to_change.clear()
        f = engine.norm(self.filter_var.get())
        shown = 0
        changes = self.app.result.changes if self.app.result else ()
        for c in changes:
            if f and f not in engine.norm(c.summary) \
                    and f not in engine.norm(c.kind_label):
                continue
            shown += 1
            iid = self.tree.insert("", "end", text=str(c.cid + 1),
                                   values=(c.kind_label, c.location, c.summary),
                                   tags=(f"{c.kind}:{c.op}",))
            self.row_to_change[iid] = c
            if sel_cid is not None and c.cid == sel_cid:
                self.tree.selection_set(iid)
                self.tree.see(iid)
        total = len(changes)
        self.count_lbl.configure(
            text=f"{shown} of {total} changes" if f else f"{total} changes")

    def _selected(self):
        sel = self.tree.selection()
        return self.row_to_change.get(sel[0]) if sel else None

    def _preview(self):
        c = self._selected()
        if c:
            self.app.jump_to(c, flash=False)

    def _jump(self):
        c = self._selected()
        if c:
            self.app.jump_to(c, flash=True)

    def jump_next(self, delta=1):
        kids = self.tree.get_children()
        if not kids:
            return
        sel = self.tree.selection()
        if sel:
            i = kids.index(sel[0]) + delta
        else:
            i = len(kids) - 1 if delta < 0 else 0
        i = max(0, min(len(kids) - 1, i))
        self.tree.selection_set(kids[i])
        self.tree.see(kids[i])
        self.app.jump_to(self.row_to_change[kids[i]], flash=True)


class App(tk.Tk):
    def __init__(self, a=None, b=None, auto=False, mode="side",
                 changes=False):
        super().__init__()
        self.title("PDF Compare")
        self.geometry("1280x800")
        self.result = None
        self.changes_win = None
        self.zoom = 1.0
        self._q = queue.Queue()
        self._worker = None
        self._cancel = threading.Event()
        self._t0 = 0.0
        self._open_changes = changes
        self._gen = None
        self._nav_i = -1

        self._menu()
        self._toolbar()
        body = tk.Frame(self)
        body.pack(fill="both", expand=True)
        right = tk.Frame(body)
        right.pack(side="right", fill="y")
        self.hscroll = tk.Scrollbar(body, orient="horizontal",
                                    command=self._on_hscrollbar)
        self.hscroll.pack(side="bottom", fill="x")
        self.vscroll = tk.Scrollbar(right, orient="vertical",
                                    command=self._on_vscrollbar)
        self.vscroll.pack(side="right", fill="y")
        self.panes = ttk.Panedwindow(body, orient="horizontal")
        self.panes.pack(side="left", fill="both", expand=True)
        self.va = DocView(self.panes, self, "a")
        self.vb2 = DocView(self.panes, self, "b")
        self.panes.add(self.va, weight=1)
        self.panes.add(self.vb2, weight=1)
        self.status = tk.Label(self, anchor="w", relief="groove",
                               text="Ready - open two PDFs and press Compare")
        self.status.pack(fill="x", side="bottom")

        if a:
            self._load(self.va, a)
        if b:
            self._load(self.vb2, b)
        if mode != "side":
            self.mode_var.set(mode)
            self._set_mode()
        if auto and a and b:
            self.after(200, self.compare)
        self.after(100, self._poll)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # -- chrome ---------------------------------------------------------
    def _menu(self):
        m = tk.Menu(self)
        f = tk.Menu(m, tearoff=0)
        f.add_command(label="Open PDF A...", command=lambda: self._open(self.va))
        f.add_command(label="Open PDF B...", command=lambda: self._open(self.vb2))
        f.add_separator()
        f.add_command(label="Compare", accelerator="Ctrl+G", command=self.compare)
        f.add_command(label="Cancel", command=self._cancel_cmp)
        f.add_separator()
        f.add_command(label="Quit", command=self._on_close)
        m.add_cascade(label="File", menu=f)
        v = tk.Menu(m, tearoff=0)
        self.mode_var = tk.StringVar(value="side")
        for lbl, val in (("Side by side", "side"), ("PDF A only", "a"),
                         ("PDF B only", "b")):
            v.add_radiobutton(label=lbl, value=val, variable=self.mode_var,
                              command=self._set_mode)
        v.add_separator()
        v.add_command(label="Zoom in", command=lambda: self._zoom(1.25))
        v.add_command(label="Zoom out", command=lambda: self._zoom(0.8))
        m.add_cascade(label="View", menu=v)
        w = tk.Menu(m, tearoff=0)
        w.add_command(label="Changes list", command=self.show_changes)
        w.add_command(label="Next change", accelerator="Ctrl+PgDn",
                      command=lambda: self._nav(1))
        w.add_command(label="Previous change", accelerator="Ctrl+PgUp",
                      command=lambda: self._nav(-1))
        m.add_cascade(label="Window", menu=w)
        self.config(menu=m)
        self.bind_all("<Control-g>", lambda e: self.compare())
        self.bind_all("<Control-Prior>", lambda e: self._nav(-1))
        self.bind_all("<Control-Next>", lambda e: self._nav(1))

    def _toolbar(self):
        t = tk.Frame(self)
        t.pack(fill="x")
        self.btn_a = tk.Button(t, text="Open A...",
                               command=lambda: self._open(self.va))
        self.btn_a.pack(side="left", padx=2, pady=2)
        self.btn_b = tk.Button(t, text="Open B...",
                               command=lambda: self._open(self.vb2))
        self.btn_b.pack(side="left", padx=2)
        self.cmp_btn = tk.Button(t, text="Compare", command=self.compare,
                                 bg="#d0e8ff")
        self.cmp_btn.pack(side="left", padx=8)
        self.text_var = tk.BooleanVar(value=True)
        self.img_var = tk.BooleanVar(value=True)
        tk.Checkbutton(t, text="Text", variable=self.text_var).pack(side="left")
        tk.Checkbutton(t, text="Images", variable=self.img_var).pack(side="left")
        tk.Button(t, text="Changes list", command=self.show_changes
                  ).pack(side="left", padx=8)
        tk.Button(t, text="<", width=2, command=lambda: self._nav(-1),
                  ).pack(side="left", padx=(0, 0))
        tk.Button(t, text=">", width=2, command=lambda: self._nav(1)
                  ).pack(side="left", padx=(2, 0))
        tk.Button(t, text="-", width=2, command=lambda: self._zoom(0.8)
                  ).pack(side="left", padx=(8, 0))
        self.zoom_lbl = tk.Label(t, text="100%", width=5)
        self.zoom_lbl.pack(side="left")
        tk.Button(t, text="+", width=2, command=lambda: self._zoom(1.25)
                  ).pack(side="left")

    # -- files -------------------------------------------------------------
    def _open(self, view):
        if self._worker and self._worker.is_alive():
            self._status("Wait for the compare to finish, or press Cancel.")
            return
        p = filedialog.askopenfilename(filetypes=[("PDF", "*.pdf"),
                                                  ("All", "*.*")])
        if p:
            self._load(view, p)

    def _load(self, view, path):
        path = _fit_path(path)
        pwd = None
        while True:
            try:
                view.load(path, pwd)
                break
            except engine.NeedsPassword:
                pwd = simpledialog.askstring(
                    "Password",
                    f"{os.path.basename(path)} is password protected:",
                    show="*", parent=self)
                if pwd is None:
                    self._status(f"{os.path.basename(path)}: "
                                 "not loaded (password required)")
                    return
            except (engine.WrongPassword, engine.NotPDF) as e:
                self._status(f"Cannot open {path}: {e}")
                return
            except Exception as e:
                self._status(f"Cannot open {path}: {e}")
                return
        self._status(f"{os.path.basename(path)}: {len(view.doc)} pages"
                     "   -   open the other PDF and press Compare")

    # -- compare ------------------------------------------------------------
    def compare(self):
        if not (self.va.path and self.vb2.path):
            self._status("Open both PDFs first.")
            return
        if self._worker and self._worker.is_alive():
            return
        self.result = None
        self._nav_i = -1
        self.va.set_highlights([])
        self.vb2.set_highlights([])
        self._gen = (self.va.generation, self.vb2.generation)
        self.cmp_btn.configure(text="Cancel", bg="#ffd0d0",
                               command=self._cancel_cmp)
        self.btn_a.configure(state="disabled")
        self.btn_b.configure(state="disabled")
        self._cancel.clear()
        self._status("Comparing...")
        self._t0 = time.time()
        self._worker = threading.Thread(
            target=self._cmp_thread,
            args=(self.va.path, self.vb2.path,
                  self.text_var.get(), self.img_var.get(),
                  self.va.password, self.vb2.password, self._gen),
            daemon=True)
        self._worker.start()

    def _cancel_cmp(self):
        self._cancel.set()

    def _cmp_thread(self, a, b, txt, img, pwa, pwb, gen):
        try:
            res = engine.compare(a, b, txt, img, progress=self._prog,
                                 cancel=self._cancel.is_set,
                                 pwd_a=pwa, pwd_b=pwb)
            self._q.put(("done", res, gen))
        except Exception as e:
            self._q.put(("error", str(e)))

    _last_prog = 0.0

    def _prog(self, done, total):
        now = time.time()
        if now - self._last_prog > 0.15:
            self._last_prog = now
            self._q.put(("progress", (done, total)))

    def _poll(self):
        try:
            while True:
                msg = self._q.get_nowait()
                if msg[0] == "progress":
                    d, t = msg[1]
                    self._status(f"Comparing... page {d}/{t}")
                elif msg[0] == "error":
                    self._cmp_finished()
                    self._status(f"Compare failed: {msg[1]}")
                elif msg[0] == "done":
                    self._cmp_finished()
                    res, gen = msg[1], msg[2]
                    if gen != self._gen or \
                            (self.va.generation, self.vb2.generation) != gen:
                        self._status("Discarded compare result "
                                     "(a document changed meanwhile); "
                                     "press Compare to retry.")
                        continue
                    if self._cancel.is_set():
                        self._status("Compare cancelled.")
                        continue
                    self.result = res
                    self.va.set_highlights(res.changes)
                    self.vb2.set_highlights(res.changes)
                    dt = time.time() - self._t0
                    nt = sum(c.kind == "text" for c in res.changes)
                    nv = sum(c.kind == "visual" for c in res.changes)
                    np_ = sum(c.kind == "pages" for c in res.changes)
                    self._status(
                        f"{len(res.changes)} changes ({nt} text, {nv} visual, "
                        f"{np_} page) in {dt:.1f}s - open 'Changes list' to jump")
                    if self.changes_win and self.changes_win.winfo_exists():
                        self.changes_win.deiconify()
                        self.changes_win.refresh()
                    if self._open_changes:
                        self._open_changes = False
                        self.show_changes()
        except queue.Empty:
            pass
        self.after(90, self._poll)

    def _cmp_finished(self):
        self.cmp_btn.configure(text="Compare", bg="#d0e8ff",
                               command=self.compare)
        state = "disabled" if (self._worker and self._worker.is_alive()) \
            else "normal"
        self.btn_a.configure(state=state)
        self.btn_b.configure(state=state)

    # -- scroll sync ---------------------------------------------------------
    def _on_vscrollbar(self, *args):
        frac = self.vscroll.get()[0]
        self.va.canvas.yview_moveto(frac)
        self.vb2.canvas.yview_moveto(self.va.view_frac_for(self.vb2, frac))
        self.va.draw()
        self.vb2.draw()

    def _on_hscrollbar(self, *args):
        frac = self.hscroll.get()[0]
        self.va.canvas.xview_moveto(frac)
        self.vb2.canvas.xview_moveto(frac)

    def on_scroll(self, view):
        if self.va.doc is None or self.vb2.doc is None:
            return
        other = self.vb2 if view is self.va else self.va
        first = view.canvas.yview()[0]
        other.canvas.yview_moveto(view.view_frac_for(other, first))
        other.draw()

    def _zoom(self, factor):
        self.zoom = max(ZOOM_MIN, min(ZOOM_MAX, self.zoom * factor))
        self.zoom_lbl.configure(text=f"{int(round(self.zoom * 100))}%")
        self.va.set_zoom(self.zoom)
        self.vb2.set_zoom(self.zoom)

    def _set_mode(self):
        for v in self.panes.panes():
            self.panes.forget(v)
        side = self.mode_var.get()
        if side == "side":
            self.panes.add(self.va, weight=1)
            self.panes.add(self.vb2, weight=1)
        else:
            self.panes.add(self.va if side == "a" else self.vb2, weight=1)
        self.va.draw()
        self.vb2.draw()

    # -- changes / navigation -------------------------------------------
    def show_changes(self):
        if not self.result:
            self._status("Run Compare first.")
            return
        if not (self.changes_win and self.changes_win.winfo_exists()):
            self.changes_win = ChangesWindow(self)
        self.changes_win.deiconify()
        self.changes_win.lift()
        self.changes_win.focus_force()
        self.changes_win.refresh()

    def jump_to(self, c, flash=False):
        a = c.a_page if c.a_page is not None else c.b_page
        b = c.b_page if c.b_page is not None else c.a_page
        self.va.scroll_to(a, c.a_rects[0] if c.a_rects else None)
        self.vb2.scroll_to(b, c.b_rects[0] if c.b_rects else None)
        if flash:
            self.va.flash(a, c.a_rects)
            self.vb2.flash(b, c.b_rects)

    def _nav(self, delta):
        if self.changes_win and self.changes_win.winfo_exists():
            self.changes_win.jump_next(delta)
        elif self.result and self.result.changes:
            ch = self.result.changes
            self._nav_i = max(0, min(len(ch) - 1, self._nav_i + delta))
            self.jump_to(ch[self._nav_i], flash=True)
        elif not self.result:
            self._status("Run Compare first.")

    # -- misc -------------------------------------------------------------
    def _status(self, text):
        self.status.configure(text=text)

    def _on_close(self):
        self._cancel.set()
        self.va.close_doc()
        self.vb2.close_doc()
        self.destroy()


def selftest(paths, want_text=True, want_images=True, out=None):
    if len(paths) < 2:
        raise SystemExit("usage: app.py --selftest A.pdf B.pdf [--text-only] "
                         "[--out FILE]")
    a, b = _fit_path(paths[0]), _fit_path(paths[1])
    res = engine.compare(a, b, want_text, want_images)
    lines = [f"pages A={res.pages_a} B={res.pages_b} changes={len(res.changes)}"]
    for c in res.changes:
        lines.append(f"  #{c.cid} [{c.kind}/{c.op}] {c.location} :: {c.summary[:90]}")
        lines.append(f"      a_rects={c.a_rects} b_rects={c.b_rects}")
    text = "\n".join(lines)
    if out:
        with open(out, "w", encoding="utf-8") as f:
            f.write(text)
    elif sys.stdout:
        print(text)
    return res


def _take(args, name):
    if name in args:
        i = args.index(name)
        if i + 1 >= len(args):
            raise SystemExit(f"error: {name} requires a value")
        v = args[i + 1]
        del args[i:i + 2]
        return v
    return None


def main():
    args = sys.argv[1:]
    if "--selftest" in args:
        args.remove("--selftest")
        wt, wi = True, True
        if "--text-only" in args:
            args.remove("--text-only")
            wi = False
        out = _take(args, "--out")
        files = [a for a in args if not a.startswith("-")]
        selftest(files, wt, wi, out)
        return
    auto = "--auto" in args
    if auto:
        args.remove("--auto")
    changes = "--changes" in args
    if changes:
        args.remove("--changes")
    mode = _take(args, "--mode") or "side"
    if mode not in ("side", "a", "b"):
        raise SystemExit("error: --mode must be side, a or b")
    files = [a for a in args if not a.startswith("-")]
    App(a=files[0] if len(files) > 0 else None,
        b=files[1] if len(files) > 1 else None, auto=auto, mode=mode,
        changes=changes).mainloop()


if __name__ == "__main__":
    main()
