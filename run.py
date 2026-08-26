"""Print Bridge entry point.

Starts the FastAPI/uvicorn HTTP server in a background thread, opens the
config UI in the default browser, then runs the system tray icon on the main
thread. Quitting the tray icon cleanly stops the server (see
app.tray.TrayApp._quit); closing this process any other way also stops it
since the server thread is a daemon thread.

If Print Bridge is already running (e.g. the user double-clicked the .exe a
second time), this just opens the browser to the existing instance instead
of failing with a "port already in use" error.

This is the script PyInstaller builds (see build.spec) and the one the
"Start with Windows" shortcut launches in dev mode (see app/tray.py).
"""
from __future__ import annotations

import json
import logging
import sys
import traceback
import urllib.request
import webbrowser

# Ensure `app` is importable both when run as `python run.py` from the
# project root and when frozen by PyInstaller.
sys.path.insert(0, __file__.rsplit("\\", 1)[0] if "\\" in __file__ else ".")

from app import config as config_mod  # noqa: E402
from app.main import BridgeServer, setup_logging  # noqa: E402
from app.tray import TrayApp  # noqa: E402

logger = logging.getLogger("print_bridge")


def _show_fatal_error(message: str) -> None:
    """Best-effort visible error for a windowed (no console) build."""
    logger.error(message)
    try:
        import ctypes

        MB_ICONERROR = 0x10
        ctypes.windll.user32.MessageBoxW(0, message, "Print Bridge - error", MB_ICONERROR)
    except Exception:
        pass


def _is_print_bridge_already_running(port: int) -> bool:
    """True if something answering like Print Bridge is already listening on
    `port` - used so a second launch just opens the browser instead of
    failing with a "port already in use" error."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1.5) as resp:
            data = json.loads(resp.read())
            return data.get("status") == "ok"
    except Exception:
        return False


def main() -> None:
    """Start the HTTP server, open the config UI, then run the tray icon
    (blocks until "Quit"). See the module docstring for the full sequence."""
    setup_logging()
    logger.info("Print Bridge starting (version %s)", __import__("app").__version__)

    cfg = config_mod.load_config()
    port = cfg["port"]

    if _is_print_bridge_already_running(port):
        logger.info("Print Bridge is already running on port %d - opening browser only", port)
        webbrowser.open(f"http://127.0.0.1:{port}/")
        return

    server = BridgeServer()
    try:
        server.start()
    except Exception as exc:
        _show_fatal_error(
            "Print Bridge could not start its HTTP server:\n\n"
            f"{exc}\n\nSee the log file for details."
        )
        sys.exit(1)

    webbrowser.open(f"http://127.0.0.1:{server.port}/")

    tray = TrayApp(server)
    try:
        tray.run()  # blocks until "Quit" is chosen
    except Exception:
        logger.error("Tray icon crashed:\n%s", traceback.format_exc())
        server.stop()
        raise

    logger.info("Print Bridge exiting")


if __name__ == "__main__":
    main()
