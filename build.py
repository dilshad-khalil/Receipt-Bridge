"""Convenience build script: generates icon.ico if it doesn't exist yet, then
runs PyInstaller against build.spec to produce dist/PrintBridge.exe.

Usage (from the project's venv):
    python build.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
ICON_PATH = PROJECT_ROOT / "icon.ico"
FAVICON_PNG = PROJECT_ROOT / "frontend" / "public" / "favicon.png"


def ensure_icon() -> None:
    """(Re)generate icon.ico from frontend/public/favicon.png - the same
    artwork used as the web favicon (see frontend/index.html), so the .exe
    file icon and the browser-tab icon match. Always regenerated (not just
    when icon.ico is missing) so editing favicon.png and rebuilding is
    enough to update the .exe icon too.
    """
    from PIL import Image

    print(f"Generating {ICON_PATH.name} from {FAVICON_PNG.relative_to(PROJECT_ROOT)} ...")
    sizes = [16, 32, 48, 64, 128, 256]

    if FAVICON_PNG.exists():
        base = Image.open(FAVICON_PNG).convert("RGBA")
    else:
        # Fallback: draw the same simple printer glyph used for the running
        # tray icon, in case favicon.png is ever missing from the checkout.
        print(f"  ({FAVICON_PNG.name} not found - falling back to the drawn tray icon)")
        sys.path.insert(0, str(PROJECT_ROOT))
        from app.tray import _make_icon_image

        base = _make_icon_image(256)

    base.save(ICON_PATH, format="ICO", sizes=[(s, s) for s in sizes])
    print(f"Wrote {ICON_PATH}")


def run_pyinstaller() -> None:
    """Invoke PyInstaller against build.spec to produce dist/PrintBridge.exe."""
    cmd = [sys.executable, "-m", "PyInstaller", "build.spec", "--noconfirm"]
    print("Running:", " ".join(cmd))
    subprocess.run(cmd, cwd=PROJECT_ROOT, check=True)


def main() -> None:
    """Entry point: generate the icon, then build the .exe."""
    ensure_icon()
    run_pyinstaller()
    exe_path = PROJECT_ROOT / "dist" / "PrintBridge.exe"
    if exe_path.exists():
        print(f"\nBuilt: {exe_path}")
    else:
        print("\nBuild finished but dist/PrintBridge.exe was not found - check the PyInstaller output above.")


if __name__ == "__main__":
    main()
