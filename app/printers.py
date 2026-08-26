"""Windows printer enumeration, logical-name resolution, connector
construction, and printer status.

Uses win32print.EnumPrinters at Level 4, which returns a lightweight dict
per printer (name/server/attributes only, no per-printer OpenPrinter calls),
matching what python-escpos's Win32Raw.open() itself checks against.

A logical printer mapping (config.json's `printers` dict, see
config._normalize_mapping) is `{"targets": [...], "dpi": ..., "width_px":
...}` - an *ordered* list of targets (index 0 is the primary, the rest are
failover backups - see app/escpos_jobs.py's failover logic), where each
target is one of two shapes:
  - `{"type": "windows", "name": ...}` - an installed Windows printer,
    reached via python-escpos's Win32Raw connector.
  - `{"type": "network", "host": ..., "port": ...}` - a raw ESC/POS printer
    listening on a TCP port directly (typically 9100), needing no Windows
    driver at all, reached via python-escpos's Network connector.
`open_connector()` below is the one place that turns a single target into a
live connector; every job (text/raw/test-print/PDF, see app/escpos_jobs.py)
goes through it, once per target it tries, so the rest of the code writes
to "a printer" without caring which transport backs it - both connector
classes share the same write API (`.text()`, `.image()`, `.cut()`, ...)
since both subclass escpos.printer.Escpos.
"""
from __future__ import annotations

import logging
import socket
from typing import Any

import pywintypes
import win32print
from escpos.exceptions import Error as EscposError
from escpos.printer import Network, Win32Raw

from app import config as config_mod

logger = logging.getLogger("print_bridge")

# Local printers plus printer connections the user has made to other
# machines' shared printers - not network-discovered printers in general,
# and no USB/serial/HID enumeration (out of scope).
_ENUM_FLAGS = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS

# Used when actually opening a connector to send a real job - a bit more
# patient than the status probe below, since a slow/booting printer is
# still worth waiting a few seconds for rather than failing immediately.
CONNECT_TIMEOUT_S = 5.0
# Used only for the periodic reachability probe (get_printer_status) - kept
# short since the Printers page polls this regularly and shouldn't feel
# sluggish just because one mapped printer happens to be switched off.
STATUS_PROBE_TIMEOUT_S = 1.5


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


class PrinterConfigError(Exception):
    """Raised for a malformed/unsupported printer mapping (e.g. an unknown
    `type`, or a network mapping missing `host`) - a configuration problem
    that retrying will never fix, so escpos_jobs.py's retry loop must NOT
    retry on this."""


class PrinterConnectionError(Exception):
    """Raised when a connector for an otherwise well-formed mapping could
    not be opened/reached - e.g. the Windows printer is temporarily
    offline, or the network printer's host:port refused/timed out the
    connection. Unlike PrinterConfigError, this kind of failure can
    plausibly clear up on its own, so escpos_jobs.py's retry loop treats it
    as transient."""


def resolve_printer_settings(logical_name: str, config: dict[str, Any]) -> dict[str, Any]:
    """Return the full mapping entry (`targets`/dpi/width_px) for a logical
    printer - used by every /print/* endpoint, and by /print/pdf
    specifically to fall back to the printer's configured DPI/dot-width
    when the request doesn't override them. Raises
    UnknownLogicalPrinterError if not configured."""
    entry = config.get("printers", {}).get(logical_name)
    if entry is None:
        raise UnknownLogicalPrinterError(logical_name)
    if isinstance(entry, str):
        # Defensive fallback only - config.load_config() always normalizes
        # entries to the typed dict shape (with a `targets` list) before a
        # request ever reaches here, but this keeps the function correct
        # if ever called with a raw/un-normalized value in a test or
        # future migration.
        return {
            "targets": [{"type": "windows", "name": entry}],
            "dpi": config_mod.DEFAULT_PRINTER_DPI,
            "width_px": config_mod.DEFAULT_PRINTER_WIDTH_PX,
        }
    return entry


def describe_target(target: dict[str, Any]) -> str:
    """Short human-readable description of one target - a Windows printer
    name, or a network printer's host:port."""
    if target.get("type") == "network":
        return f'{target.get("host")}:{target.get("port", config_mod.DEFAULT_NETWORK_PORT)}'
    return str(target.get("name", "?"))


def describe_mapping(mapping: dict[str, Any]) -> str:
    """Short human-readable description of a mapping's full failover chain,
    for logs and UI messages - each target's `describe_target()` joined in
    try-order, e.g. `"EPSON TM-T20III Receipt -> 192.168.1.51:9100"` for a
    primary Windows printer with one network backup. A single-target
    mapping (the common case) just prints as that one target, unchanged
    from before failover existed."""
    targets = mapping.get("targets") or []
    if not targets:
        return "(no targets configured)"
    return " -> ".join(describe_target(t) for t in targets)


def open_connector(target: dict[str, Any], job_name: str) -> Any:
    """Open a live python-escpos connector for a single resolved target -
    `Win32Raw` for a `"windows"` target, `escpos.printer.Network` for a
    `"network"` one. Takes one target dict (one element of a mapping's
    `targets` list, see resolve_printer_settings), not the whole mapping -
    app/escpos_jobs.py's failover logic calls this once per target it
    tries.

    python-escpos's Win32Raw does not open the Windows print spooler job in
    its constructor - `.open()` must be called explicitly (it starts the
    spooler doc; the caller is responsible for calling `.close()` when
    done, same as before). `Network` connects in its constructor instead,
    so there's nothing further to "open" for that branch.

    :raises PrinterConfigError: the target itself is malformed (unknown
        `type`, or missing a required field) - not retryable.
    :raises PrinterConnectionError: the target looks fine but the printer
        couldn't be reached right now - plausibly transient, so
        escpos_jobs.py's retry loop retries these before failing over to
        the mapping's next target, if any.
    """
    printer_type = target.get("type")
    if printer_type == "windows":
        name = target.get("name")
        if not name:
            raise PrinterConfigError("Windows printer target is missing 'name'")
        printer = Win32Raw(name)
        try:
            printer.open(job_name=job_name)
        except (EscposError, pywintypes.error) as exc:
            raise PrinterConnectionError(
                f"Windows printer '{name}' is not available: {exc}"
            ) from exc
        return printer

    if printer_type == "network":
        host = target.get("host")
        if not host:
            raise PrinterConfigError("Network printer target is missing 'host'")
        port = target.get("port", config_mod.DEFAULT_NETWORK_PORT)
        try:
            return Network(host, port=port, timeout=CONNECT_TIMEOUT_S)
        except (EscposError, OSError) as exc:
            raise PrinterConnectionError(
                f"Network printer {host}:{port} is not reachable: {exc}"
            ) from exc

    raise PrinterConfigError(f"Unknown printer type '{printer_type}'")


def close_connector(connector: Any) -> None:
    """Best-effort close - never raise from here, so a close failure can't
    mask (or get confused with) the real error from the job itself."""
    try:
        connector.close()
    except Exception:
        logger.exception("Error closing printer connection")


# --- Status ------------------------------------------------------------

# win32print status bit flags -> a short status string. Multiple flags can
# be set simultaneously (e.g. offline AND paper out); checked in roughly
# worst-first order so the single string returned is the most actionable
# one to show on the Printers page.
_STATUS_FLAG_ORDER = [
    (win32print.PRINTER_STATUS_OFFLINE, "offline"),
    (win32print.PRINTER_STATUS_NOT_AVAILABLE, "offline"),
    (win32print.PRINTER_STATUS_ERROR, "error"),
    (win32print.PRINTER_STATUS_PAPER_JAM, "paper_jam"),
    (win32print.PRINTER_STATUS_PAPER_OUT, "paper_out"),
    (win32print.PRINTER_STATUS_DOOR_OPEN, "door_open"),
    (win32print.PRINTER_STATUS_PAUSED, "paused"),
    (win32print.PRINTER_STATUS_BUSY, "busy"),
    (win32print.PRINTER_STATUS_PRINTING, "busy"),
]


def _decode_windows_status(status_flags: int) -> str:
    for flag, label in _STATUS_FLAG_ORDER:
        if status_flags & flag:
            return label
    return "ready"


def get_printer_status(mapping: dict[str, Any]) -> str:
    """Best-effort status for one mapping, for the Printers page's status
    indicator - reports the status of the mapping's *primary* target only
    (`targets[0]`), not its backups. A backup target being down doesn't by
    itself make the mapping unable to print (that's the whole point of
    failover - see app/escpos_jobs.py), so surfacing only the primary here
    keeps the single status dot meaning what it's always meant: "is the
    printer this is normally going to reach OK right now".

    Never raises - a mapping with no targets, or an unreadable/removed
    printer, becomes "offline" (or "unreachable"), not a 500, since this
    backs a page that polls periodically and must degrade gracefully.
    """
    targets = mapping.get("targets") or []
    if not targets:
        return "offline"
    return _get_target_status(targets[0])


def _get_target_status(target: dict[str, Any]) -> str:
    """Best-effort status for a single target. What the string means
    differs by backing type, since there's no universal cross-brand
    ESC/POS status query over a raw socket:

    - `"windows"`: the real spooler-reported printer status, decoded from
      win32print's status flags - one of "ready", "offline", "paper_out",
      "paper_jam", "door_open", "paused", "busy", "error".
    - `"network"`: no protocol-level status is available, so this is only
      a reachability probe - a short-timeout TCP connect - returning
      "reachable" or "unreachable", not a real device status.
    """
    if target.get("type") == "network":
        host = target.get("host")
        port = target.get("port", config_mod.DEFAULT_NETWORK_PORT)
        if not host:
            return "unreachable"
        try:
            with socket.create_connection((host, port), timeout=STATUS_PROBE_TIMEOUT_S):
                return "reachable"
        except OSError:
            return "unreachable"

    name = target.get("name")
    if not name:
        return "offline"
    try:
        handle = win32print.OpenPrinter(name)
        try:
            info = win32print.GetPrinter(handle, 2)
        finally:
            win32print.ClosePrinter(handle)
        return _decode_windows_status(info.get("Status", 0))
    except Exception:
        return "offline"
