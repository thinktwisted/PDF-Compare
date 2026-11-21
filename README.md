# PDF Compare

A small, low-memory Windows tool for comparing two PDF files — text **and**
images — side by side with the key changes highlighted, plus a pop-out
**Changes list** you can click to jump straight to each change.

![side-by-side with highlights](docs/screenshot.png)

## Download

Grab the latest single-file build from the
[releases page](https://github.com/thinktwisted/PDF-Compare/releases/latest):
`PDFCompare.exe` (Windows x64, no installation needed).

## Features

- **Side-by-side view** of both PDFs with synchronized scrolling
  (page-aligned, so documents with added/removed pages stay together).
- **Text comparison** — changed lines are grouped and highlighted
  yellow (replaced), green (added), red (removed), with old/new text
  snippets in the change list.
- **Image / layout comparison** — pages are rasterized and diffed in
  blocks; moved or edited images and graphics get blue highlights.
  Pixels inside text line boxes are excluded so shifted text never
  double-reports.
- **Page changes** — whole pages inserted (purple) or removed (red).
- **Changes list pop-out** (Window > Changes list, or the toolbar button):
  one row per change with type, location and summary. Click a row to jump
  there in both panes; double-click or press Enter to also flash a red
  focus box. The toolbar **<** and **>** buttons (or Ctrl+PgUp / Ctrl+PgDn)
  jump to the previous / next change, with or without the list open.
- **Single-PDF mode** — View menu: show only PDF A or only PDF B alongside
  the changes list, click-to-jump as before.
- **Zoom** 40–300 %, Text/Images toggles, horizontal + vertical scroll.

## Low RAM by design

- Pages are rasterized **on demand**; only pages near the viewport exist
  on screen, and rendered bitmaps are true-LRU with a per-pane pixel
  budget (20 M raster px), so high zoom on long documents can't balloon.
- The diff works page-pair by page-pair: only per-page text and
  coordinates are held, never whole-document images.
- Comparison runs in a worker thread with its own file handles; the UI
  stays responsive and shows page progress. Measured peak working set on
  the bundled samples (interpreter, MuPDF and Tk included): **76 MB**.

## Use

1. Launch `PDFCompare.exe` (or `python app.py`).
2. *Open A…* = original, *Open B…* = revised document.
3. Press **Compare** (Ctrl+G). Highlights appear on the pages.
4. Open **Changes list** and click entries, or use **<** / **>** to step.

Command line: `PDFCompare.exe before.pdf after.pdf --auto --changes`
opens, compares and pops the list immediately.
`--mode b` starts in single-PDF view of B.

## Build the executable

Requires Python 3.10+ and PyMuPDF + PyInstaller on Windows
(versions pinned in `requirements.txt`):

```bat
pip install -r requirements.txt pyinstaller
build_exe.bat        REM or: pyinstaller --noconfirm PDFCompare.spec
```

Output: `dist\PDFCompare.exe` (single file, no install needed).

## Development

```
app.py          Tkinter GUI (side-by-side canvases, changes pop-out)
engine.py       diff engine: page alignment, text diff, block pixel diff
make_samples.py generates samples/old.pdf + samples/new.pdf for testing
PDFCompare.spec PyInstaller one-file build recipe
build_exe.bat   build helper -> dist/PDFCompare.exe
```

Headless engine check:

```
python app.py --selftest samples/old.pdf samples/new.pdf [--text-only]
```

## Known limitations

- Line-level text diff (not word-level), matching the "key changes" goal.
- A visual change confined entirely inside a text line box is reported
  only as text when the text itself changed.
- Scanned (image-only) PDFs compare via the pixel diff; there is no OCR.
- Password-protected PDFs prompt for the password; only PDF-format files
  are accepted (XPS/EPUB/etc. are rejected even if named `.pdf`).

## License

GPL-3.0 — see [LICENSE](LICENSE).
