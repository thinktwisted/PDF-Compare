"""Generate samples/old.pdf and samples/new.pdf for smoke-testing."""
import os
import pymupdf

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "samples")
os.makedirs(OUT, exist_ok=True)

W, H = 612, 792


def page_text(page, items):
    for text, x, y, size in items:
        page.insert_text((x, y), text, fontsize=size, fontname="helv")


def old():
    d = pymupdf.open()
    p = d.new_page(width=W, height=H)
    page_text(p, [
        ("Quarterly Report - Q3", 72, 90, 20),
        ("Revenue grew 12% versus last year.", 72, 140, 12),
        ("Operating costs remained flat.", 72, 160, 12),
        ("Headcount stayed at 148 employees.", 72, 180, 12),
        ("The Denver office closed in August.", 72, 200, 12),
        ("Cash reserves are healthy at 9 months of runway.", 72, 220, 12),
    ])
    p = d.new_page(width=W, height=H)
    page_text(p, [("Figure 1 - Regional distribution", 72, 90, 14)])
    p.draw_rect(pymupdf.Rect(80, 300, 320, 480), color=(0, 0, 0),
                fill=(0.85, 0.15, 0.15), width=1)          # red block
    p.draw_circle(pymupdf.Point(450, 390), 60, color=(0, 0, 0),
                  fill=(0.15, 0.15, 0.85), width=1)        # blue circle
    page_text(p, [("West", 180, 400, 16)])
    p = d.new_page(width=W, height=H)
    page_text(p, [
        ("Appendix A - Detailed tables", 72, 90, 16),
        ("Region  North   South   East   West", 72, 130, 12),
        ("Q1      2.1     1.8     2.5    3.0", 72, 150, 12),
        ("Q2      2.3     1.9     2.4    3.3", 72, 170, 12),
        ("Q3      2.6     2.0     2.7    3.9", 72, 190, 12),
    ])
    d.save(os.path.join(OUT, "old.pdf"))
    d.close()


def new():
    d = pymupdf.open()
    p = d.new_page(width=W, height=H)
    page_text(p, [
        ("Quarterly Report - Q3", 72, 90, 20),
        ("Revenue grew 18% versus last year.", 72, 140, 12),      # changed
        ("Operating costs increased 3% in Q3.", 72, 160, 12),     # changed
        ("Headcount stayed at 148 employees.", 72, 180, 12),
        # Denver line removed
        ("Cash reserves are healthy at 9 months of runway.", 72, 200, 12),
        ("A new Austin office opened in September.", 72, 220, 12),  # added
    ])
    p = d.new_page(width=W, height=H)
    page_text(p, [("Figure 1 - Regional distribution", 72, 90, 14)])
    p.draw_rect(pymupdf.Rect(140, 340, 420, 520), color=(0, 0, 0),
                fill=(0.15, 0.6, 0.15), width=1)              # moved + green
    p.draw_circle(pymupdf.Point(450, 390), 60, color=(0, 0, 0),
                  fill=(0.15, 0.15, 0.85), width=1)           # unchanged
    page_text(p, [("West", 180, 400, 16)])
    p = d.new_page(width=W, height=H)                        # inserted page
    page_text(p, [
        ("Interim Note", 72, 90, 16),
        ("This page was added between revisions.", 72, 130, 12),
    ])
    p = d.new_page(width=W, height=H)
    page_text(p, [
        ("Appendix A - Detailed tables", 72, 90, 16),
        ("Region  North   South   East   West", 72, 130, 12),
        ("Q1      2.1     1.8     2.5    3.0", 72, 150, 12),
        ("Q2      2.3     1.9     2.4    3.3", 72, 170, 12),
        ("Q3      2.6     2.0     2.7    4.2", 72, 190, 12),   # cell changed
    ])
    d.save(os.path.join(OUT, "new.pdf"))
    d.close()


if __name__ == "__main__":
    old()
    new()
    print("wrote", OUT)
