"""PDF comparison engine: text + pixel diffing with coordinates.

Single dependency: PyMuPDF. Works lazily page-by-page to keep memory
low: only page fingerprints are held globally; line data and pixmaps
are released per page pair.

Rectangles are (x0, y0, x1, y1) in PDF points, origin top-left, in each
document's own page coordinates (MuPDF normalizes page origins to 0).
pixel_diff_rects returns (a_rects, b_rects) lists; a side may be empty
when the change lives only on the other page (e.g. larger page margins).
"""
from __future__ import annotations

import difflib
import os
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional

import pymupdf

INSERT, DELETE, REPLACE = "insert", "delete", "replace"
TEXT, VISUAL, PAGES = "text", "visual", "pages"

# pixel-diff tuning
PX_DPI = 48          # rasterization dpi for comparison (cheap)
PX_BLOCK = 24        # block size in px (~0.5in) for change grouping
PX_STRIDE = 3        # sample every 3rd pixel
PX_CELL_LIMIT = 1400 # accumulated |diff| per cell before it counts as dirty
MIN_RECT_PT2 = 12.0  # ignore sub-noise rectangles (points^2)
FP_CHUNK = 16        # pages between cancellation checks


class NeedsPassword(Exception):
    pass


class WrongPassword(Exception):
    pass


class NotPDF(Exception):
    pass


@dataclass
class Change:
    cid: int
    kind: str                  # 'text' | 'visual' | 'pages'
    op: str                    # 'insert' | 'delete' | 'replace'
    a_page: Optional[int]      # 0-based page in PDF A, None if absent there
    b_page: Optional[int]
    a_rects: list = field(default_factory=list)
    b_rects: list = field(default_factory=list)
    old: str = ""
    new: str = ""

    @property
    def kind_label(self):
        if self.kind == TEXT:
            return {"replace": "text changed", "insert": "text added",
                    "delete": "text removed"}[self.op]
        if self.kind == VISUAL:
            return "image/layout change"
        return {"insert": "page added", "delete": "page removed",
                "replace": "page replaced"}[self.op]

    @property
    def location(self):
        parts = []
        if self.a_page is not None:
            parts.append(f"A p.{self.a_page + 1}")
        if self.b_page is not None:
            parts.append(f"B p.{self.b_page + 1}")
        return ", ".join(parts)

    @property
    def summary(self):
        if self.kind == PAGES:
            return self.new or self.old
        if self.kind == VISUAL:
            return self.new or "(visual difference)"
        s = self.old or self.new
        if self.old and self.new:
            s = f"{self.old}  ->  {self.new}"
        return s


@dataclass
class Result:
    changes: list
    pages_a: int
    pages_b: int
    aligned_pairs: int = 0


def norm(s: str) -> str:
    return " ".join(s.split()).lower()


def page_lines(page) -> list:
    """[(stripped text, bbox)] sorted roughly top-to-bottom."""
    out = []
    d = page.get_text("dict")
    for blk in d.get("blocks", ()):
        if blk.get("type") != 0:
            continue
        for ln in blk.get("lines", ()):
            txt = "".join(sp.get("text", "") for sp in ln.get("spans", ()))
            t = txt.strip()
            if t:
                out.append((t, tuple(ln["bbox"])))
    out.sort(key=lambda r: (round(r[1][1] / 2.0), r[1][0]))
    return out


# ---------------------------------------------------------------- page align

def page_opcodes(h1: list, h2: list):
    sm = difflib.SequenceMatcher(None, h1, h2, autojunk=False)
    return sm.get_opcodes()


# ---------------------------------------------------------------- text diff

def pair_text_changes(pa, pb) -> list:
    """[(op, rects_a, rects_b, old_str, new_str, ratio)] per page pair."""
    la = page_lines(pa)
    lb = page_lines(pb)
    ta = [norm(t) for t, _ in la]
    tb = [norm(t) for t, _ in lb]
    sm = difflib.SequenceMatcher(None, ta, tb, autojunk=False)
    out = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        ra = [la[k][1] for k in range(i1, i2)]
        rb = [lb[k][1] for k in range(j1, j2)]
        old = " | ".join(la[k][0] for k in range(i1, i2))
        new = " | ".join(lb[k][0] for k in range(j1, j2))
        if len(ra) == 1:
            old = la[i1][0]
        if len(rb) == 1:
            new = lb[j1][0]
        out.append((tag, ra, rb, old[:200], new[:200], sm.ratio()))
    return out


# -------------------------------------------------------------- pixel diff

def _pixmap(page):
    z = PX_DPI / 72.0
    return page.get_pixmap(matrix=pymupdf.Matrix(z, z),
                           colorspace=pymupdf.csRGB, alpha=False)


def _text_cell_mask(pages, ncol, nrow):
    """Boolean grid: cells whose center lies inside a text line bbox."""
    mask = [False] * (ncol * nrow)
    s = PX_DPI / 72.0
    for pg in pages:
        for _, (x0, y0, x1, y1) in page_lines(pg):
            gx0 = max(0, int(x0 * s) // PX_BLOCK)
            gx1 = max(gx0, int(x1 * s) // PX_BLOCK)
            gy0 = max(0, int(y0 * s) // PX_BLOCK)
            gy1 = max(gy0, int(y1 * s) // PX_BLOCK)
            for gy in range(gy0, min(gy1 + 1, nrow)):
                base = gy * ncol
                for gx in range(gx0, min(gx1 + 1, ncol)):
                    mask[base + gx] = True
    return mask


def pixel_diff_rects(pa, pb, mask_lines: bool = False) -> list:
    """[(a_rects, b_rects)] of visually changed regions, PDF points.

    mask_lines: skip pixels inside text line boxes (those regions are the
    text diff's responsibility; avoids duplicate flags for shifted text).
    """
    ma, mb = _pixmap(pa), _pixmap(pb)
    wa, ha, wb, hb = ma.width, ma.height, mb.width, mb.height
    w, h = min(wa, wb), min(ha, hb)
    rects = []
    scale = 72.0 / PX_DPI

    # Page-size mismatch: strips beyond the overlap exist on one side only.
    if wb != wa or hb != ha:
        a_strip, b_strip = [], []
        if wb > wa:
            b_strip.append((wa * scale, 0, wb * scale, hb * scale))
        if wa > wb:
            a_strip.append((wb * scale, 0, wa * scale, ha * scale))
        if hb > ha:
            b_strip.append((0, ha * scale, wb * scale, hb * scale))
        if ha > hb:
            a_strip.append((0, hb * scale, wa * scale, ha * scale))
        if a_strip:
            rects.append((a_strip, []))
        if b_strip:
            rects.append(([], b_strip))

    if w > 0 and h > 0:
        a, b = ma.samples, mb.samples
        st_a, st_b = ma.stride, mb.stride
        row_bytes = w * 3
        ncol = w // PX_BLOCK + 1
        nrow = h // PX_BLOCK + 1
        cells = [0] * (ncol * nrow)
        y = 0
        while y < h:
            oa, ob = y * st_a, y * st_b
            if a[oa:oa + row_bytes] != b[ob:ob + row_bytes]:
                gy = y // PX_BLOCK
                x = 0
                while x < w:
                    ia, ib = oa + x * 3, ob + x * 3
                    d = (abs(a[ia] - b[ib]) + abs(a[ia + 1] - b[ib + 1])
                         + abs(a[ia + 2] - b[ib + 2]))
                    if d:
                        cells[gy * ncol + x // PX_BLOCK] += d
                    x += PX_STRIDE
            y += PX_STRIDE
        del a, b
        mask = (_text_cell_mask((pa, pb), ncol, nrow)
                if mask_lines else None)
        dirty = set()
        for idx, v in enumerate(cells):
            if v > PX_CELL_LIMIT and not (mask and mask[idx]):
                dirty.add(idx)
        del cells
        # merge adjacent dirty cells (4-neighbour BFS)
        seen = set()
        for start in sorted(dirty):
            if start in seen:
                continue
            seen.add(start)
            q = deque([start])
            gx0 = gx1 = start % ncol
            gy0 = gy1 = start // ncol
            while q:
                c = q.popleft()
                cx, cy = c % ncol, c // ncol
                gx0, gx1 = min(gx0, cx), max(gx1, cx)
                gy0, gy1 = min(gy0, cy), max(gy1, cy)
                for n in (c - 1, c + 1, c - ncol, c + ncol):
                    if n in dirty and n not in seen:
                        # keep components from wrapping rows
                        if abs(n - c) == 1 and n // ncol != cy:
                            continue
                        seen.add(n)
                        q.append(n)
            r = (gx0 * PX_BLOCK * scale, gy0 * PX_BLOCK * scale,
                 min(gx1 + 1, ncol) * PX_BLOCK * scale,
                 min(gy1 + 1, nrow) * PX_BLOCK * scale)
            if (r[2] - r[0]) * (r[3] - r[1]) >= MIN_RECT_PT2 * 4:
                rects.append(([r], [r]))
    del ma, mb
    return rects


# ------------------------------------------------------------------ compare

def _open(path: str, password: Optional[str] = None):
    """Open a PDF with validation: PDF format only, password handled."""
    try:
        doc = pymupdf.open(path)
    except Exception as e:
        raise type(e)(f"{os.path.basename(path)}: {e}") from e
    if not doc.is_pdf:
        doc.close()
        raise NotPDF(f"{os.path.basename(path)} is not a PDF file")
    if doc.needs_pass:
        if password:
            if not doc.authenticate(password):
                doc.close()
                raise WrongPassword(
                    f"wrong password for {os.path.basename(path)}")
        else:
            doc.close()
            raise NeedsPassword(os.path.basename(path))
    return doc


def _fingerprints(doc, cancel, progress, done_ref, total):
    """Page hashes; cancellable and progress-reporting on long books."""
    hs = []
    n = len(doc)
    for i in range(n):
        if cancel and i % FP_CHUNK == 0:
            if cancel():
                return None
            if progress:
                done_ref[0] = i // 2
                progress(done_ref[0], total)
        hs.append(hash(norm(doc[i].get_text())))
    return hs


def compare(path_a: str, path_b: str,
            want_text: bool = True, want_images: bool = True,
            progress: Optional[Callable[[int, int], None]] = None,
            cancel: Optional[Callable[[], bool]] = None,
            pwd_a: Optional[str] = None,
            pwd_b: Optional[str] = None) -> Result:
    da = _open(path_a, pwd_a)
    try:
        db = _open(path_b, pwd_b)
    except Exception:
        da.close()
        raise
    try:
        return _compare_docs(da, db, want_text, want_images, progress, cancel)
    finally:
        db.close()
        da.close()


def _align_pair_block(da, db, h1, h2, i1, i2, j1, j2):
    """Pair pages inside a mismatched 'replace' block.

    Uses DP over text similarity for small blocks so an inserted junk page
    does not shift the real counterpart; falls back to zip for big ones.
    Returns [(ai, bj) | (ai, None) | (None, bj)].
    """
    A = list(range(i1, i2))
    B = list(range(j1, j2))
    if not A or not B:
        return [(a, None) for a in A] + [(None, b) for b in B]
    if len(A) * len(B) > 225:
        k = min(len(A), len(B))
        return (list(zip(A[:k], B[:k]))
                + [(a, None) for a in A[k:]] + [(None, b) for b in B[k:]])
    sa = {i: norm(da[i].get_text())[:800] for i in A}
    sb = {j: norm(db[j].get_text())[:800] for j in B}

    def sim(i, j):
        if h1[i] == h2[j]:
            return 1.0
        x, y = sa[i], sb[j]
        if x == y:
            return 1.0
        return difflib.SequenceMatcher(None, x, y).ratio()

    la, lb = len(A), len(B)
    dp = [[0.0] * (lb + 1) for _ in range(la + 1)]
    pair = [[None] * (lb + 1) for _ in range(la + 1)]
    for i in range(la):
        for j in range(lb):
            best = dp[i][j + 1]
            src = "up"
            if dp[i + 1][j] > best:
                best, src = dp[i + 1][j], "left"
            s = sim(A[i], B[j])
            if s >= 0.45 and dp[i][j] + s > best:
                best, src = dp[i][j] + s, "diag"
            dp[i + 1][j + 1] = best
            pair[i + 1][j + 1] = src
    res = []
    i, j = la, lb
    while i > 0 and j > 0:
        src = pair[i][j]
        if src == "diag":
            res.append((A[i - 1], B[j - 1]))
            i, j = i - 1, j - 1
        elif src == "up":
            res.append((A[i - 1], None))
            i -= 1
        elif src == "left":
            res.append((None, B[j - 1]))
            j -= 1
        else:            # empty block or ties: consume deterministically
            if dp[i][j + 1] == dp[i - 1][j]:
                res.append((A[i - 1], None))
                i -= 1
            else:
                res.append((None, B[j - 1]))
                j -= 1
    while i > 0:
        res.append((A[i - 1], None))
        i -= 1
    while j > 0:
        res.append((None, B[j - 1]))
        j -= 1
    return res[::-1]


def _pair_pages(da, db, h1, h2, ap, bp, want_text, want_images, add):
    if h1[ap] == h2[bp]:
        # text layer identical: only the pixel pass can find changes
        if want_images:
            for ar, br in pixel_diff_rects(da[ap], db[bp]):
                add(VISUAL, REPLACE, ap, bp, ar, br,
                    new="(visual difference)")
    else:
        _diff_pair(da[ap], db[bp], ap, bp, want_text, want_images, add)


def _compare_docs(da, db, want_text, want_images, progress, cancel) -> Result:
    n, m = len(da), len(db)
    total = max(n, m, 1)
    done_ref = [0]
    h1 = _fingerprints(da, cancel, progress, done_ref, total)
    if h1 is None:
        return Result([], n, m)
    h2 = _fingerprints(db, cancel, progress, done_ref, total)
    if h2 is None:
        return Result([], n, m)

    changes = []
    cid = 0
    done = n // 2
    pairs = 0

    def tick():
        nonlocal done
        done += 1
        if progress:
            progress(done, total + 1)

    def add(kind, op, ap, bp, ar, br, old="", new=""):
        nonlocal cid
        changes.append(Change(cid, kind, op, ap, bp, ar, br, old, new))
        cid += 1

    for tag, i1, i2, j1, j2 in page_opcodes(h1, h2):
        if tag == "insert":
            for p in range(j1, j2):
                if cancel and cancel():
                    break
                tick()
                add(PAGES, INSERT, None, p, [], [tuple(db[p].rect)],
                    new=f"page {p + 1}")
        elif tag == "delete":
            for p in range(i1, i2):
                if cancel and cancel():
                    break
                tick()
                add(PAGES, DELETE, p, None, [tuple(da[p].rect)], [],
                    new=f"page {p + 1}")
        elif tag == "replace":
            for ap, bp in _align_pair_block(da, db, h1, h2, i1, i2, j1, j2):
                if cancel and cancel():
                    break
                tick()
                if ap is not None and bp is not None:
                    pairs += 1
                    _pair_pages(da, db, h1, h2, ap, bp,
                                want_text, want_images, add)
                elif ap is not None:
                    add(PAGES, DELETE, ap, None, [tuple(da[ap].rect)], [],
                        new=f"page {ap + 1}")
                else:
                    add(PAGES, INSERT, None, bp, [], [tuple(db[bp].rect)],
                        new=f"page {bp + 1}")
        else:  # equal text layer: pixel pass only (catches moved/edited images)
            for q in range(i2 - i1):
                if cancel and q % 4 == 0 and cancel():
                    break
                ap, bp = i1 + q, j1 + q
                tick()
                pairs += 1
                if want_images:
                    for ar, br in pixel_diff_rects(da[ap], db[bp]):
                        add(VISUAL, REPLACE, ap, bp, ar, br,
                            new="(visual difference)")

    changes.sort(key=lambda c: (c.b_page if c.b_page is not None
                                else c.a_page, c.cid))
    for i, c in enumerate(changes):
        c.cid = i
    return Result(changes, n, m, pairs)


def _diff_pair(pa, pb, pi, pj, want_text, want_images, add):
    if want_text:
        for op, ra, rb, old, new, ratio in pair_text_changes(pa, pb):
            if ratio < 0.25 and (ra or rb):
                # page rewritten wholesale: one coarse change
                add(TEXT, REPLACE, pi, pj,
                    [tuple(pa.rect)] if (ra or not rb) else [],
                    [tuple(pb.rect)],
                    old="(entire page different)",
                    new="(entire page different)")
                return
            add(TEXT, op, pi, pj, ra, rb, old, new)
    if want_images:
        for ar, br in pixel_diff_rects(pa, pb, mask_lines=want_text):
            add(VISUAL, REPLACE, pi, pj, ar, br, new="(visual difference)")
