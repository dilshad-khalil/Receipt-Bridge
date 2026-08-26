"""System tray icon: status, config UI shortcut, log access, server restart,
and a "Start with Windows" toggle backed by a shortcut in the Startup folder
(see app/startup.py - shared with the Settings page's equivalent toggle via
POST /config/startup).
"""
from __future__ import annotations

import logging
import os

import pystray
import webbrowser
from PIL import Image, ImageDraw

from app import config as config_mod
from app import startup
from app.main import BridgeServer

logger = logging.getLogger("print_bridge")


# ---------------------------------------------------------------------------
# Tray icon image (drawn at runtime - no bundled asset needed)
# ---------------------------------------------------------------------------


def _make_icon_image(size: int = 64) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    body = (35, 95, 160, 255)
    paper = (255, 255, 255, 255)
    slot = (20, 60, 110, 255)
    d.rectangle([size * 0.28, size * 0.05, size * 0.72, size * 0.40], fill=paper)
    d.rounded_rectangle(
        [size * 0.12, size * 0.32, size * 0.88, size * 0.80], radius=size * 0.08, fill=body
    )
    d.rectangle([size * 0.22, size * 0.44, size * 0.78, size * 0.52], fill=slot)
    d.rounded_rectangle(
        [size * 0.22, size * 0.78, size * 0.78, size * 0.92], radius=size * 0.04, fill=body
    )
    return img


# ---------------------------------------------------------------------------
# Tray app
# ---------------------------------------------------------------------------


class TrayApp:
    """Owns the pystray icon. `run()` blocks, so it must be called from the
    main thread while the HTTP server (BridgeServer) runs in its own thread.
    """

    def __init__(self, server: BridgeServer):
        self.server = server
        self._icon = pystray.Icon(
            "print_bridge",
            icon=_make_icon_image(),
            title=self._status_text(),
            menu=self._build_menu(),
        )

    def _status_text(self) -> str:
        if self.server.is_running:
            return f"Print Bridge - running on port {self.server.port}"
        return "Print Bridge - stopped"

    def _status_label(self, item=None) -> str:
        return self._status_text()

    def _open_config_ui(self, icon=None, item=None) -> None:
        port = self.server.port or config_mod.load_config()["port"]
        webbrowser.open(f"http://127.0.0.1:{port}/")

    def _open_logs(self, icon=None, item=None) -> None:
        logs_dir = config_mod.get_logs_dir()
        try:
            os.startfile(logs_dir)  # type: ignore[attr-defined]
        except Exception:
            logger.exception("Could not open logs folder %s", logs_dir)

    def _restart_server(self, icon=None, item=None) -> None:
        try:
            self.server.restart()
        except Exception:
            logger.exception("Failed to restart Print Bridge server from tray")
        self._refresh()

    def _toggle_startup(self, icon=None, item=None) -> None:
        try:
            startup.set_startup_enabled(not startup.is_startup_enabled())
        except Exception:
            logger.exception("Failed to toggle 'Start with Windows'")
        self._refresh()

    def _quit(self, icon=None, item=None) -> None:
        logger.info("Quit requested from tray icon")
        try:
            self.server.stop()
        finally:
            self._icon.stop()

    def _build_menu(self) -> pystray.Menu:
        return pystray.Menu(
            pystray.MenuItem(self._status_label, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Open printer setup", self._open_config_ui),
            pystray.MenuItem("View logs", self._open_logs),
            pystray.MenuItem("Restart server", self._restart_server),
            pystray.MenuItem(
                "Start with Windows",
                self._toggle_startup,
                checked=lambda item: startup.is_startup_enabled(),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._quit),
        )

    def _refresh(self) -> None:
        self._icon.title = self._status_text()
        self._icon.menu = self._build_menu()
        try:
            self._icon.update_menu()
        except Exception:
            pass

    def run(self) -> None:
        """Blocking - runs the tray icon's event loop on the calling thread."""
        self._icon.run()
