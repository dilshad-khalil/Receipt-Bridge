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
    "rate_limit_per_minute": 60,
}

# Default cap for /print/* requests per minute (see app/rate_limit.py and
# app/main.py's enforce_rate_limit) - generous for normal receipt-printing
# traffic (a busy till sending a few jobs a minute) while still stopping a
# runaway integration from flooding the spooler/printer.
DEFAULT_RATE_LIMIT_PER_MINUTE = 60

# Defaults for a printer's PDF-rasterization settings (see app/pdf_jobs.py).
# 203 DPI is the common thermal-printer resolution (some are 180 or 300, so
# it's overridable per printer); 384px is a safe default dot width for a
# narrow 58mm printhead (576 is typical for 80mm).
DEFAULT_PRINTER_DPI = 203
DEFAULT_PRINTER_WIDTH_PX = 384

# 9100 ("JetDirect"/raw ESC/POS-over-TCP) is the near-universal default port
# for receipt printers that listen on the network directly - used whenever a
# network mapping is saved/migrated without an explicit port.
DEFAULT_NETWORK_PORT = 9100

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


def _normalize_target(value: Any) -> Optional[dict[str, Any]]:
    """Validate/clean one entry of a mapping's `targets` list -
    `{"type": "windows", "name": ...}` or `{"type": "network", "host": ...,
    "port": ...}` - filling in the default port for a network target
    missing one. Returns `None` if `value` isn't a usable target at all
    (caller drops it), the same "don't crash on a malformed hand-edited
    config.json" policy used throughout this module.
    """
    if not isinstance(value, dict):
        return None
    target_type = value.get("type")
    if target_type == "windows":
        name = value.get("name")
        if not name:
            return None
        return {"type": "windows", "name": name}
    if target_type == "network":
        host = value.get("host")
        if not host:
            return None
        return {"type": "network", "host": host, "port": value.get("port", DEFAULT_NETWORK_PORT)}
    return None


def _normalize_mapping(value: Any) -> tuple[Optional[dict[str, Any]], bool]:
    """Upgrade one `cfg["printers"][logical]` entry to the current shape:
    `{"targets": [{"type": ..., ...}, ...], "dpi": ..., "width_px": ...}` -
    an *ordered* list of targets (index 0 is the primary, tried first;
    later ones are backups tried only once the ones before them have
    exhausted their own retries - see app/escpos_jobs.py's failover logic),
    plus the dpi/width_px used for PDF rasterization. Those two are
    mapping-level, not per-target: every target for one logical printer is
    assumed to feed the same physical receipt roll/width.

    Three prior shapes are recognized and migrated into a one-item
    `targets` list, same backward-compatibility policy as every previous
    config version bump:
      1. A plain string - just the Windows printer name
         (`{"receipt_1": "EPSON TM-T88"}`).
      2. A dict with no `type` key - the pre-v4a shape, Windows-only
         (`{"windows_printer_name": ..., "dpi": ..., "width_px": ...}`).
      3. A dict with a `type` key but no `targets` key - the v4a
         single-target shape (`{"type": ..., "name"|"host"/"port": ...,
         "dpi": ..., "width_px": ...}`).

    :returns: `(normalized_entry_or_None, changed)` - `None` means the
        entry was unsalvageable (e.g. hand-edited into nonsense, or every
        target in its list was invalid) and should be dropped; `changed`
        tells the caller whether anything needed rewriting, so
        `load_config()` knows whether to persist the upgrade.
    """
    if isinstance(value, str):
        return (
            {
                "targets": [{"type": "windows", "name": value}],
                "dpi": DEFAULT_PRINTER_DPI,
                "width_px": DEFAULT_PRINTER_WIDTH_PX,
            },
            True,
        )

    if not isinstance(value, dict):
        return None, True

    entry = dict(value)
    changed = False

    if "targets" not in entry:
        # Pre-v4c: either the v4a single-typed-target shape, or the
        # original pre-v4a Windows-only shape - fold both into a
        # single-item target list using the same field-renaming v4a
        # already did for the pre-v4a shape.
        if "type" not in entry:
            if "windows_printer_name" in entry:
                entry["type"] = "windows"
                entry["name"] = entry.pop("windows_printer_name")
            elif "host" in entry:
                entry["type"] = "network"
            else:
                return None, True
        single_target = _normalize_target(entry)
        if single_target is None:
            return None, True
        entry = {
            "targets": [single_target],
            "dpi": entry.get("dpi", DEFAULT_PRINTER_DPI),
            "width_px": entry.get("width_px", DEFAULT_PRINTER_WIDTH_PX),
        }
        changed = True

    targets_in = entry.get("targets")
    if not isinstance(targets_in, list):
        return None, True
    targets_out: list[dict[str, Any]] = []
    for raw_target in targets_in:
        normalized_target = _normalize_target(raw_target)
        if normalized_target is None:
            changed = True
            continue
        if normalized_target != raw_target:
            changed = True
        targets_out.append(normalized_target)
    if not targets_out:
        # No valid target left at all - nothing this mapping could ever
        # print to, so it's dropped the same way a malformed single-target
        # mapping always has been.
        return None, True

    if "dpi" not in entry:
        entry["dpi"] = DEFAULT_PRINTER_DPI
        changed = True
    if "width_px" not in entry:
        entry["width_px"] = DEFAULT_PRINTER_WIDTH_PX
        changed = True

    return {"targets": targets_out, "dpi": entry["dpi"], "width_px": entry["width_px"]}, changed


def _normalize_printers(cfg: dict[str, Any]) -> bool:
    """Upgrade `cfg["printers"]` in place to the current shape (see
    `_normalize_mapping`) and report whether anything changed, so the
    caller knows whether to persist it."""
    printers = cfg.get("printers", {})
    if not isinstance(printers, dict):
        cfg["printers"] = {}
        return True
    changed = False
    normalized: dict[str, Any] = {}
    for logical, value in printers.items():
        entry, entry_changed = _normalize_mapping(value)
        changed = changed or entry_changed
        if entry is not None:
            normalized[logical] = entry

    if normalized.keys() != printers.keys():
        changed = True
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
    *,
    printer_type: str,
    name: Optional[str] = None,
    host: Optional[str] = None,
    port: Optional[int] = None,
    dpi: Optional[int] = None,
    width_px: Optional[int] = None,
) -> dict[str, Any]:
    """Add a new logical printer mapping, or replace an existing one's
    *entire* `targets` list with just this one target - the "Add mapping"
    form's simple path (one logical name, one target) and persist it.

    To add/remove/reorder additional *backup* targets on an existing
    mapping without replacing its primary, use `set_printer_targets`
    instead (POST /config/printers/{name}/targets) - that's what the
    Printers page's target-editor dialog calls.

    `printer_type` is `"windows"` (target given by `name`, a Windows printer
    name) or `"network"` (target given by `host`/`port`, a raw TCP ESC/POS
    printer needing no Windows driver at all - see app/printers.py's
    `open_connector`).

    `dpi`/`width_px` (used by PDF rasterization, see app/pdf_jobs.py; these
    are mapping-level, not per-target) fall back to the mapping's existing
    values when updating, or the module defaults when creating a new
    mapping - so re-saving a mapping from the UI without touching those
    fields doesn't reset them. Likewise, a network mapping's `port` falls
    back to its existing primary target's value (or the module default) if
    not given, so repointing just the DPI/width doesn't require resending
    the port.
    """
    with _lock:
        cfg = load_config()
        printers = cfg.setdefault("printers", {})
        existing = printers.get(logical_name, {})
        existing = existing if isinstance(existing, dict) else {}
        existing_dpi = existing.get("dpi", DEFAULT_PRINTER_DPI)
        existing_width = existing.get("width_px", DEFAULT_PRINTER_WIDTH_PX)
        existing_targets = existing.get("targets") or []

        if printer_type == "windows":
            target: dict[str, Any] = {"type": "windows", "name": name}
        elif printer_type == "network":
            existing_port = existing_targets[0].get("port") if existing_targets else None
            target = {
                "type": "network",
                "host": host,
                "port": port if port is not None else (existing_port or DEFAULT_NETWORK_PORT),
            }
        else:
            raise ValueError(f"Unknown printer type '{printer_type}'")

        printers[logical_name] = {
            "targets": [target],
            "dpi": dpi if dpi is not None else existing_dpi,
            "width_px": width_px if width_px is not None else existing_width,
        }
        save_config(cfg)
        return cfg


def set_printer_targets(logical_name: str, targets: list[dict[str, Any]]) -> dict[str, Any]:
    """Replace an existing mapping's ordered `targets` list wholesale - the
    operation behind the Printers page's target-editor dialog: adding a
    backup target, removing one, or reordering them. Index 0 is always the
    primary, tried first; the rest are backups, tried in order only once
    the ones before them have exhausted their own retries (see
    app/escpos_jobs.py's failover logic).

    Each of `targets` must already be in the typed per-target shape
    (`{"type": "windows", "name": ...}` or `{"type": "network", "host":
    ..., "port": ...}`) - app/main.py's endpoint validates/builds these
    from the request body before calling this. dpi/width_px (mapping-level,
    not per-target) are left untouched.

    :raises KeyError: `logical_name` isn't an existing mapping - unlike
        `upsert_printer`, this only edits an existing mapping's targets, it
        never creates a new logical printer.
    :raises ValueError: `targets` is empty - a mapping must always have at
        least one target to ever be usable.
    """
    if not targets:
        raise ValueError("A printer mapping needs at least one target")
    with _lock:
        cfg = load_config()
        printers = cfg.setdefault("printers", {})
        if logical_name not in printers:
            raise KeyError(logical_name)
        printers[logical_name]["targets"] = targets
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
    rate_limit_per_minute: Optional[int] = None,
) -> dict[str, Any]:
    """Update top-level settings and persist them.

    A restart is required for `port` and `allowed_origins` changes to take
    effect, since the HTTP server and CORS middleware are configured once at
    startup - the config UI makes this clear to the user. `auth_token` and
    `rate_limit_per_minute` both take effect immediately instead, since
    app/main.py's `require_token`/`enforce_rate_limit` dependencies re-read
    config on every request.
    """
    with _lock:
        cfg = load_config()
        if port is not None:
            cfg["port"] = port
        if allowed_origins is not None:
            cfg["allowed_origins"] = allowed_origins
        if auth_token is not UNSET:
            cfg["auth_token"] = auth_token
        if rate_limit_per_minute is not None:
            cfg["rate_limit_per_minute"] = rate_limit_per_minute
        save_config(cfg)
        return cfg
