# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Print Bridge: a single-file, windowed (no console)
Windows executable.

Build with:
    pyinstaller build.spec --noconfirm
or just run `python build.py`, which generates icon.ico first if missing and
then invokes this spec.
"""
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

PROJECT_ROOT = Path(SPECPATH)  # noqa: F821 - injected into the spec's namespace by PyInstaller

FRONTEND_DIST = PROJECT_ROOT / "frontend" / "dist"
if not FRONTEND_DIST.is_dir():
    raise SystemExit(
        "frontend/dist not found - run `npm install && npm run build` inside "
        "frontend/ before building the .exe (see README 'Building')."
    )

datas = [(str(FRONTEND_DIST), "frontend/dist")]
# python-escpos ships capabilities.json as package data (printer profile
# definitions); python-barcode ships a fallback TTF font for software-
# rendered barcodes. Both are loaded via importlib.resources at runtime and
# need to be bundled explicitly - PyInstaller's static import analysis can't
# see them since they aren't imported, just opened as files.
datas += collect_data_files("escpos")
datas += collect_data_files("barcode")
# PyMuPDF (the `fitz` module) ships a couple of builtin font resources used
# as fallbacks when a PDF doesn't embed its own fonts; collect_data_files
# is a no-op if a given PyMuPDF build doesn't have any, so this is safe
# either way.
datas += collect_data_files("fitz")
datas += collect_data_files("pymupdf")

hiddenimports = [
    "win32timezone",  # commonly required by pywin32/win32com under PyInstaller
    "win32com.client",
    "pythoncom",
    "pywintypes",
    "fitz",  # PyMuPDF legacy import alias
    "pymupdf",  # PyMuPDF current import name (app/pdf_jobs.py: `import pymupdf as fitz`)
]

a = Analysis(
    [str(PROJECT_ROOT / "run.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="PrintBridge",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # windowed - no console popup; errors go to the log file
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(PROJECT_ROOT / "icon.ico"),
)
