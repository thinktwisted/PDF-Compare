# -*- mode: python ; coding: utf-8 -*-
# Build:  pyinstaller PDFCompare.spec
a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=["pymupdf"],
    hookspath=[],
    excludes=[
        # keep the bundle lean: these arrive with pymupdf's pip deps but
        # the app never imports them
        "onnxruntime", "pymupdf_layout", "pymupdf.layout", "pymupdf4llm",
        "networkx", "numba", "llvmlite", "matplotlib", "scipy", "PIL",
        "numpy", "pytest", "setuptools", "pip",
    ],
    runtime_hooks=[],
    noarchive=False,
)
# MuPDF's hook collects ML layout models + dev static libs we never load
a.datas = [d for d in a.datas
           if "layout" not in d[0].lower() and "mupdf-devel" not in d[0].lower()
           and not d[0].lower().endswith(".lib")]
a.binaries = [b for b in a.binaries
              if "mupdf-devel" not in b[0].lower()]
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="PDFCompare",
    debug=False,
    strip=False,
    upx=False,
    console=False,          # windowed app
    icon=None,
)
