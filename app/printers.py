"""Windows printer enumeration and logical-name resolution.

Uses win32print.EnumPrinters at Level 4, which returns a lightweight dict
per printer (name/server/attributes only, no per-printer OpenPrinter calls),
matching what python-escpos's Win32Raw.open() itself checks against.
"""
from __future__ import annotations

from typing import Any

import win32print

# Local printers plus printer connections the user has made to other
# machines' shared printers - not network-discovered printers in general,
# and no USB/serial/HID enumeration (out of scope).
_ENUM_FLAGS = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS


def list_printers() -> list[dict[str, Any]]:
    """Return all Windows printers visible to this user session."""
    try:
        default_name = win32print.GetDefaultPrinter()
    except Exception:
        default_name = None

    raw = win32print.EnumPrinters(_ENUM_FLAGS, None, 4)
    printers = []
    for entry in raw:
        name = entry["pPrinterName"]
        printers.append({"name": name, "is_default": name == default_name})
    printers.sort(key=lambda p: p["name"].lower())
    return printers


def printer_exists(windows_printer_name: str) -> bool:
    """True if `windows_printer_name` is currently installed/visible."""
    return any(p["name"] == windows_printer_name for p in list_printers())


class UnknownLogicalPrinterError(KeyError):
    """Raised when a logical printer name has no mapping in config.json."""


def resolve_logical_printer(logical_name: str, config: dict[str, Any]) -> str:
    """Resolve a logical printer name (from config) to a real Windows printer
    name. Raises UnknownLogicalPrinterError if not configured."""
    entry = config.get("printers", {}).get(logical_name)
    if entry is None:
        raise UnknownLogicalPrinterError(logical_name)
    # Entries are normalized to dicts by config.load_config(), but accept a
    # bare string too so this stays correct if ever called with a raw/
    # un-normalized dict in a test or future migration.
    if isinstance(entry, str):
        return entry
    return entry["windows_printer_name"]


def resolve_printer_settings(logical_name: str, config: dict[str, Any]) -> dict[str, Any]:
    """Return the full mapping entry (windows_printer_name/dpi/width_px) for
    a logical printer - used by /print/pdf to fall back to the printer's
    configured DPI/dot-width when the request doesn't override them.
    Raises UnknownLogicalPrinterError if not configured."""
    entry = config.get("printers", {}).get(logical_name)
    if entry is None:
        raise UnknownLogicalPrinterError(logical_name)
    if isinstance(entry, str):
        return {"windows_printer_name": entry, "dpi": 203, "width_px": 384}
    return entry
