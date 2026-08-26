""""Start with Windows" shortcut management.

Creates/removes a `.lnk` in the current user's Startup folder
(`shell:startup`) so Print Bridge launches automatically at login. Kept in
its own module (rather than inside app/tray.py, where this used to live) so
both the tray menu item and the HTTP `/config/startup` endpoint (used by the
Settings page's toggle) can use it without app/main.py and app/tray.py
importing each other - app.tray already imports app.main (for BridgeServer),
so app.main importing back from app.tray would be a circular import.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger("print_bridge")

STARTUP_SHORTCUT_NAME = "PrintBridge.lnk"


def _startup_folder() -> Path:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        appdata = str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def _startup_shortcut_path() -> Path:
    return _startup_folder() / STARTUP_SHORTCUT_NAME


def is_startup_enabled() -> bool:
    """True if the Startup-folder shortcut currently exists."""
    return _startup_shortcut_path().exists()


def enable_startup() -> None:
    """Create a .lnk in the current user's Startup folder pointing at the
    running executable (frozen build) or `pythonw run.py` (dev checkout)."""
    import win32com.client  # only needed here; pulled in lazily

    project_root = Path(__file__).resolve().parent.parent
    shortcut_path = _startup_shortcut_path()
    shortcut_path.parent.mkdir(parents=True, exist_ok=True)

    shell = win32com.client.Dispatch("WScript.Shell")
    shortcut = shell.CreateShortcut(str(shortcut_path))

    if getattr(sys, "frozen", False):
        exe = sys.executable
        shortcut.TargetPath = exe
        shortcut.Arguments = ""
        shortcut.WorkingDirectory = str(Path(exe).parent)
    else:
        python_dir = Path(sys.executable).parent
        pythonw = python_dir / "pythonw.exe"
        interpreter = pythonw if pythonw.exists() else Path(sys.executable)
        run_script = project_root / "run.py"
        shortcut.TargetPath = str(interpreter)
        shortcut.Arguments = f'"{run_script}"'
        shortcut.WorkingDirectory = str(project_root)

    shortcut.Description = "Print Bridge - local ESC/POS print server"
    shortcut.Save()
    logger.info("Startup shortcut created at %s", shortcut_path)


def disable_startup() -> None:
    """Remove the Startup-folder shortcut, if present."""
    shortcut_path = _startup_shortcut_path()
    if shortcut_path.exists():
        shortcut_path.unlink()
        logger.info("Startup shortcut removed")


def set_startup_enabled(enabled: bool) -> bool:
    """Enable/disable in one call (used by POST /config/startup). Returns
    the resulting state."""
    if enabled:
        enable_startup()
    else:
        disable_startup()
    return is_startup_enabled()
