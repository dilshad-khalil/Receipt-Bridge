"""Load/save config.json for Print Bridge.

Config always lives in %APPDATA%\\PrintBridge\\config.json. This is used both
when running from source and when running as a packaged .exe, so the file
survives updates/reinstalls and never requires write access to the install
directory (which may be read-only, e.g. under Program Files).
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Optional

APP_NAME = "PrintBridge"

DEFAULT_CONFIG: dict[str, Any] = {
    "port": 9187,
    "allowed_origins": [],
    "auth_token": None,
    "printers": {},
}

# Defaults for a printer's PDF-rasterization settings (see app/pdf_jobs.py).
# 203 DPI is the common thermal-printer resolution (some are 180 or 300, so
# it's overridable per printer); 384px is a safe default dot width for a
# narrow 58mm printhead (576 is typical for 80mm).
DEFAULT_PRINTER_DPI = 203
DEFAULT_PRINTER_WIDTH_PX = 384

# Config access is a critical section shared by the HTTP handlers (which may
# run print jobs and read/write config from different request threads) and
# the tray icon. threading.RLock allows load_config() to call save_config()
# (e.g. to persist first-run defaults) without deadlocking itself.
_lock = threading.RLock()

# Sentinel to distinguish "argument not passed" from "explicitly set to None".
# Public (no leading underscore) since callers (e.g. app.main's settings
# endpoint) need to pass it explicitly to mean "leave auth_token unchanged".
UNSET = object()


def get_app_dir() -> Path:
    """Return %APPDATA%\\PrintBridge, creating it if necessary."""
    appdata = os.environ.get("APPDATA")
    if not appdata:
        # Extremely unlikely on Windows, but keep this from ever crashing.
        appdata = str(Path.home() / "AppData" / "Roaming")
    d = Path(appdata) / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_config_path() -> Path:
    """Return the full path to config.json (inside get_app_dir())."""
    return get_app_dir() / "config.json"


def get_logs_dir() -> Path:
    """Return %APPDATA%\\PrintBridge\\logs, creating it if necessary."""
    d = get_app_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_config() -> dict[str, Any]:
    """Load config.json, creating it with defaults on first run.

    Unknown/missing keys are backfilled from DEFAULT_CONFIG so older config
    files keep working after an update adds new settings.
    """
    with _lock:
        path = get_config_path()
        if not path.exists():
            cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
            save_config(cfg)
            return cfg
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("config.json does not contain a JSON object")
        except (json.JSONDecodeError, OSError, ValueError):
            # Corrupt config: fall back to defaults rather than crashing the
            # whole app, but don't clobber the bad file - it may be useful
            # for debugging.
            data = {}
        merged = json.loads(json.dumps(DEFAULT_CONFIG))
        merged.update(data)
        if _normalize_printers(merged):
            save_config(merged)
        return merged


def _normalize_printers(cfg: dict[str, Any]) -> bool:
    """Upgrade `cfg["printers"]` in place to the current shape and report
    whether anything changed (so the caller knows to persist it).

    Before this version, a printer entry was just the Windows printer name
    as a plain string (`{"receipt_1": "EPSON TM-T88"}`). PDF printing needs
    a per-printer DPI and dot width (see app/pdf_jobs.py), so entries are
    now dicts: `{"receipt_1": {"windows_printer_name": ..., "dpi": ...,
    "width_px": ...}}`. Old string-valued entries are upgraded on load with
    the DPI/width defaults, so existing config.json files from before this
    change keep working without the user having to redo their mappings.
    """
    printers = cfg.get("printers", {})
    if not isinstance(printers, dict):
        cfg["printers"] = {}
        return True
    changed = False
    normalized: dict[str, Any] = {}
    for logical, value in printers.items():
        if isinstance(value, str):
            normalized[logical] = {
                "windows_printer_name": value,
                "dpi": DEFAULT_PRINTER_DPI,
                "width_px": DEFAULT_PRINTER_WIDTH_PX,
            }
            changed = True
        elif isinstance(value, dict) and "windows_printer_name" in value:
            entry = dict(value)
            if "dpi" not in entry:
                entry["dpi"] = DEFAULT_PRINTER_DPI
                changed = True
            if "width_px" not in entry:
                entry["width_px"] = DEFAULT_PRINTER_WIDTH_PX
                changed = True
            normalized[logical] = entry
        else:
            # Malformed entry (e.g. hand-edited config.json) - drop it
            # rather than crash the whole app on startup.
            changed = True
    if changed or normalized.keys() != printers.keys():
        cfg["printers"] = normalized
        return True
    cfg["printers"] = normalized
    return changed


def save_config(cfg: dict[str, Any]) -> None:
    """Write config atomically (write to temp file, then rename)."""
    with _lock:
        path = get_config_path()
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, sort_keys=True)
        os.replace(tmp, path)


def upsert_printer(
    logical_name: str,
    windows_printer_name: str,
    dpi: Optional[int] = None,
    width_px: Optional[int] = None,
) -> dict[str, Any]:
    """Add or update a logical->Windows printer mapping and persist it.

    `dpi`/`width_px` (used by PDF rasterization, see app/pdf_jobs.py) fall
    back to the mapping's existing values when updating, or the module
    defaults when creating a new mapping - so re-saving a mapping from the
    UI without touching those fields doesn't reset them.
    """
    with _lock:
        cfg = load_config()
        printers = cfg.setdefault("printers", {})
        existing = printers.get(logical_name, {})
        existing_dpi = existing.get("dpi", DEFAULT_PRINTER_DPI) if isinstance(existing, dict) else DEFAULT_PRINTER_DPI
        existing_width = existing.get("width_px", DEFAULT_PRINTER_WIDTH_PX) if isinstance(existing, dict) else DEFAULT_PRINTER_WIDTH_PX
        printers[logical_name] = {
            "windows_printer_name": windows_printer_name,
            "dpi": dpi if dpi is not None else existing_dpi,
            "width_px": width_px if width_px is not None else existing_width,
        }
        save_config(cfg)
        return cfg


def remove_printer(logical_name: str) -> dict[str, Any]:
    """Delete a logical->Windows printer mapping, if it exists, and persist."""
    with _lock:
        cfg = load_config()
        cfg.get("printers", {}).pop(logical_name, None)
        save_config(cfg)
        return cfg


def update_settings(
    port: Optional[int] = None,
    allowed_origins: Optional[list[str]] = None,
    auth_token: Any = UNSET,
) -> dict[str, Any]:
    """Update top-level settings and persist them.

    A restart is required for `port` and `allowed_origins` changes to take
    effect, since the HTTP server and CORS middleware are configured once at
    startup - the config UI makes this clear to the user.
    """
    with _lock:
        cfg = load_config()
        if port is not None:
            cfg["port"] = port
        if allowed_origins is not None:
            cfg["allowed_origins"] = allowed_origins
        if auth_token is not UNSET:
            cfg["auth_token"] = auth_token
        save_config(cfg)
        return cfg
