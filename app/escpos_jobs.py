"""Builds and sends ESC/POS print jobs through python-escpos, retrying
transient failures and failing over across a mapping's backup targets.

Every job function here takes a resolved printer *mapping* (the dict from
app.printers.resolve_printer_settings - `{"targets": [{"type":
"windows"|"network", ...}, ...], "dpi": ..., "width_px": ...}`) rather than
a single printer target, and goes through app.printers.open_connector()/
close_connector() to get a live connector - `Win32Raw` or
`escpos.printer.Network` - once per target it tries, so the same job code
works identically against an installed Windows printer or a bare TCP
ESC/POS printer with no Windows driver at all.

Two layers of resilience, applied in order for every job:
  1. `_run_job_with_retry` - retry-with-backoff *within* one target (a
     momentary spooler hiccup, a printer mid-reboot).
  2. `_run_job_with_failover` - once a target's own retries are fully
     exhausted, fall through to the mapping's next configured target (a
     powered-off primary, a backup taking over) - see config.json's
     `targets` list (app/config.py's `_normalize_mapping`).

`run_text_job`'s per-line handling also checks each `/print/text` line for
Arabic content (see app/text_render.py) and routes just that line through
the same shape+bidi-reorder+bitmap pipeline `test_print.py` uses for the
dedicated test print, instead of native ESC/POS text mode - so a receipt
mixing English and Arabic lines renders both correctly, not just the
lines that happen to be pure Latin.
"""
from __future__ import annotations

import base64
import logging
import time
from typing import Any, Callable

import pywintypes
from escpos.exceptions import Error as EscposError
from PIL import Image

from app import printers, text_render

logger = logging.getLogger("print_bridge")

# Backoff between successive attempts of a failed job, in seconds - spans a
# quick spooler hiccup (0.5s) up through a printer that just power-cycled
# and is still bringing its network stack back up (4s). `len(RETRY_DELAYS_S)`
# retries are attempted on top of the first try, so a job is attempted up to
# `len(RETRY_DELAYS_S) + 1` times in total before giving up.
RETRY_DELAYS_S = (0.5, 1.5, 4.0)


class PrintJobError(Exception):
    """Raised whenever a job could not be delivered to the printer, after
    all applicable retries were exhausted (or immediately, for a failure
    that isn't worth retrying at all - see `_run_job_with_retry`).

    Callers (main.py) turn this into an HTTP 502 with the message as
    detail, and record `attempts` in the job log (app/job_log.py) so a
    flaky-but-eventually-failing printer leaves a visible trace instead of
    silently retrying with nothing to show for it.
    """

    def __init__(self, message: str, attempts: int = 1) -> None:
        super().__init__(message)
        self.attempts = attempts


# Exceptions worth retrying: transport/spooler-level failures that
# plausibly clear up on their own (spooler momentarily busy, a network
# printer refusing/timing out a connection while mid-reboot, a Windows
# printer that's temporarily offline, ...). `pywintypes.error` covers
# Win32Raw failures raised mid-job (after a successful open) - e.g. the
# spooler connection dropping partway through - not just at connect time.
# PrinterConfigError (unknown printer type, malformed mapping) is
# deliberately NOT in this tuple - that's a configuration problem that
# won't fix itself, so it fails on the very first try.
_TRANSIENT_EXCEPTIONS = (EscposError, pywintypes.error, OSError, printers.PrinterConnectionError)


def _run_job_with_retry(job_fn: Callable[[], None], description: str) -> int:
    """Run one full print-job attempt (open connector, send everything,
    close) with retry-with-backoff on transient failures.

    Retries the *entire* job rather than resuming mid-way through, because
    ESC/POS has no concept of resuming a partially-sent job: if attempt 2 of
    a 5-page PDF fails after 3 pages already went through, the printer has
    no way to "continue from page 4", so starting over on a fresh connector
    is the only correct way to retry.

    :returns: how many attempts it took to succeed (1 = worked first try).
    :raises PrintJobError: if every attempt fails (`.attempts` is the total
        attempts made), or immediately if the failure isn't one of
        `_TRANSIENT_EXCEPTIONS` (`.attempts` is 1 - never retried).
    """
    max_attempts = len(RETRY_DELAYS_S) + 1
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            job_fn()
            return attempt
        except PrintJobError:
            # Already a clean, terminal error raised by the job body itself
            # (e.g. invalid barcode data) - not a transport failure, so
            # retrying it would just fail the same way three more times.
            raise
        except _TRANSIENT_EXCEPTIONS as exc:
            last_exc = exc
            if attempt == max_attempts:
                break
            delay = RETRY_DELAYS_S[attempt - 1]
            logger.warning(
                "%s: attempt %d/%d failed (%s) - retrying in %.1fs",
                description, attempt, max_attempts, exc, delay,
            )
            time.sleep(delay)
        except Exception as exc:
            # Anything else (e.g. PrinterConfigError) is a configuration
            # problem, not a transient one - fail immediately, no retry.
            raise PrintJobError(str(exc), attempts=attempt) from exc
    raise PrintJobError(
        f"{description} failed after {max_attempts} attempt(s): {last_exc}",
        attempts=max_attempts,
    ) from last_exc


def _run_job_with_failover(
    targets: list[dict[str, Any]],
    make_job_fn: Callable[[dict[str, Any]], Callable[[], None]],
    description: str,
) -> tuple[int, str]:
    """Attempt `targets` in order (index 0 = primary, the rest are ordered
    backups), retrying each one on its own transient failures first (see
    `_run_job_with_retry`) before falling through to the next target - only
    once a target's own retries are fully exhausted does this move on, so
    a backup is never preferred over a primary that's merely having a
    momentary hiccup.

    `make_job_fn(target)` builds the actual "send everything to this one
    target" closure for a single attempt - each job function below
    (`run_text_job`, etc.) supplies its own, since what "everything" means
    (lines+barcode+qr+cut, vs. a whole rasterized PDF, vs. raw bytes)
    differs per endpoint.

    :returns: `(total_attempts, served_by)` - `total_attempts` sums
        attempts spent across every target tried, including any that
        ultimately failed over; `served_by` is `printers.describe_target()`
        of whichever target actually completed the job. Both are what
        app/main.py logs and records in the job history, so "which printer
        actually printed this" is never a mystery when a primary is down.
    :raises PrintJobError: every target failed - `.attempts` is the sum
        across all of them, and the message names every target tried.
    """
    total_attempts = 0
    last_exc: Exception | None = None
    for index, target in enumerate(targets):
        target_desc = printers.describe_target(target)
        is_last_target = index == len(targets) - 1
        try:
            attempts = _run_job_with_retry(
                make_job_fn(target), description=f"{description} -> {target_desc}"
            )
            return total_attempts + attempts, target_desc
        except PrintJobError as exc:
            total_attempts += exc.attempts
            last_exc = exc
            if not is_last_target:
                logger.warning(
                    "%s: target '%s' failed after %d attempt(s) (%s) - failing over to the next target",
                    description, target_desc, exc.attempts, exc,
                )
    raise PrintJobError(
        f"{description}: all {len(targets)} target(s) failed (last error: {last_exc})",
        attempts=total_attempts,
    ) from last_exc


def _clamp_scale(value: int | None) -> int:
    if not value:
        return 1
    return max(1, min(8, int(value)))


def _apply_line(printer: Any, line: Any, width_px: int) -> None:
    """Print one `/print/text` line - either as a native ESC/POS text
    command, or, if it contains Arabic, as a bitmap.

    ESC/POS text mode can't reliably shape/reorder Arabic (see
    app/text_render.py's module docstring), so a line is only ever routed
    to the bitmap path if it actually needs it - a receipt mixing English
    and Arabic lines keeps the fast native path for the English ones and
    only pays for image rendering on the lines that require it.
    """
    align = (getattr(line, "align", None) or "left").lower()
    if align not in ("left", "center", "right"):
        raise PrintJobError(f"Invalid align '{align}', must be left/center/right")
    text = line.text or ""
    bold = bool(getattr(line, "bold", False))
    width = _clamp_scale(getattr(line, "width", 1))
    height = _clamp_scale(getattr(line, "height", 1))

    if text_render.contains_arabic(text):
        image = text_render.render_line_image(
            text, width_px, align=align, bold=bold, width_scale=width, height_scale=height
        )
        _send_image(printer, image, cut=False)
        return

    # custom_size is always passed explicitly (even for width=height=1) so
    # that every line resets text scale rather than inheriting the previous
    # line's size - GS ! is a persistent printer mode, not per-line.
    printer.set(align=align, bold=bold, custom_size=True, width=width, height=height)
    printer.text(text + "\n")


def _apply_barcode(printer: Any, barcode: Any) -> None:
    bc_type = barcode.type.strip()
    data = barcode.data
    bc_alnum = "".join(ch for ch in bc_type.upper() if ch.isalnum())
    if bc_alnum in ("CODE128", "GS1128") and not (
        len(data) >= 2 and data[0] == "{" and data[1] in "ABC"
    ):
        # CODE128/GS1-128 payloads must start with an explicit code-set
        # selector ({A, {B, or {C - see escpos.constants.BARCODE_FORMATS).
        # Code Set B covers the full printable ASCII range, so it's a safe
        # default for callers who just want "print this as a barcode"
        # without learning ESC/POS's code-set encoding rules.
        data = "{B" + data
    try:
        printer.barcode(data, bc_type, height=64, width=2, pos="BELOW", align_ct=True)
    except EscposError as exc:
        raise PrintJobError(f"Could not render barcode: {exc}") from exc


def _apply_qr(printer: Any, qr: Any) -> None:
    try:
        printer.qr(qr.data, size=6, center=True)
    except EscposError as exc:
        raise PrintJobError(f"Could not render QR code: {exc}") from exc


def _apply_cut(printer: Any, cut_mode: str) -> None:
    """Cut the paper per `cut_mode` - `"none"` (no-op), `"partial"`, or
    `"full"` (see `PrintTextRequest.cut` in app/main.py, which also accepts
    the original boolean for backward compatibility and normalizes it
    before this ever runs). Maps directly to python-escpos's `Escpos.cut()`
    modes (`"PART"`/`"FULL"`) - it already falls back to whichever of the
    two a given printer profile actually supports."""
    if cut_mode == "none":
        return
    if cut_mode == "partial":
        printer.cut(mode="PART")
    elif cut_mode == "full":
        printer.cut(mode="FULL")
    else:
        raise PrintJobError(f"Invalid cut mode '{cut_mode}'")


def run_text_job(mapping: dict[str, Any], payload: Any, job_name: str) -> tuple[int, str]:
    """Render `payload` (a PrintTextRequest) and send it to the printer,
    failing over across `mapping["targets"]` if needed (see
    `_run_job_with_failover`).

    `copies` sends the full sequence (text/barcode/qr/cut/drawer) that many
    times as separate spooler documents, so a jam/paper-out on one copy
    doesn't silently skip the rest. Each copy attempts the targets, and
    each target's retries, independently - so a primary that recovers
    between copies is still preferred for the next one, rather than the
    whole job "sticking" to whichever target served the first copy.

    :returns: `(total_attempts, served_by)` - `total_attempts` sums
        attempts across all copies and any failed-over targets;
        `served_by` names whichever target served the *last* copy (an
        earlier copy failing over is still visible in the warning-level
        logs from `_run_job_with_failover`, even if a later copy lands
        back on the primary).
    """
    targets = mapping.get("targets") or []
    if not targets:
        raise PrintJobError("No printer targets configured for this mapping", attempts=0)

    copies = max(1, payload.copies or 1)
    width_px = mapping.get("width_px", 384)
    total_attempts = 0
    served_by = ""
    for copy_num in range(1, copies + 1):
        name = job_name if copies == 1 else f"{job_name} ({copy_num}/{copies})"

        def make_job_fn(target: dict[str, Any]) -> Callable[[], None]:
            def send_one_copy() -> None:
                printer = printers.open_connector(target, name)
                try:
                    for line in payload.lines or []:
                        _apply_line(printer, line, width_px)
                    if payload.barcode:
                        _apply_barcode(printer, payload.barcode)
                    if payload.qr:
                        _apply_qr(printer, payload.qr)
                    if payload.open_drawer:
                        printer.cashdraw(2)
                    _apply_cut(printer, payload.cut)
                finally:
                    printers.close_connector(printer)

            return send_one_copy

        attempts, served_by = _run_job_with_failover(targets, make_job_fn, description=name)
        total_attempts += attempts
    return total_attempts, served_by


def _send_image(printer: Any, image: Image.Image, cut: bool) -> None:
    """Write one bitmap to the printer as an ESC/POS raster image, optionally
    followed by a paper cut.

    `printer.image()` (python-escpos) does the actual encoding: it converts
    the PIL image to 1-bit (white background inverted so "ink" pixels become
    set bits), splits it into raster strips if needed, and emits them as
    `GS v 0` raster-bitmap commands - the ESC/POS command essentially every
    thermal/receipt printer supports regardless of its firmware's codepage
    or language support, which is exactly why both the bilingual EN/AR test
    quote and PDF printing render to a bitmap and go through here rather
    than using ESC/POS text mode.

    This one function is the single place that talks to python-escpos's
    raster/cut API, shared by `run_test_print_job` (one image, the
    connectivity header + EN/AR quotes) and `run_pdf_job` (one image per PDF
    page) so there's exactly one implementation to get right and test.
    """
    printer.image(image, center=True)
    if cut:
        printer.cut()


def run_test_print_job(
    mapping: dict[str, Any],
    image: Image.Image,
    job_name: str,
    qr_data: str | None = None,
    cut: bool = True,
) -> tuple[int, str]:
    """Print the combined "Test print" job: the pre-rendered connectivity
    header + EN/AR test quotes image (see app/test_print.py), optionally
    followed by a native ESC/POS QR code, then a cut. Fails over across
    `mapping["targets"]` if needed (see `_run_job_with_failover`).

    Mixes raster (`_send_image`) and native ESC/POS commands (`printer.qr`,
    the cut) within a single open/close spooler document - printers process
    whatever commands they're sent in order regardless of kind, so a bitmap
    followed by native QR/cut commands is no different from any other
    sequence of ESC/POS commands in one job.

    :returns: `(attempts, served_by)` - see `_run_job_with_failover`.
    """
    targets = mapping.get("targets") or []
    if not targets:
        raise PrintJobError("No printer targets configured for this mapping", attempts=0)

    def make_job_fn(target: dict[str, Any]) -> Callable[[], None]:
        def send() -> None:
            printer = printers.open_connector(target, job_name)
            try:
                _send_image(printer, image, cut=False)
                if qr_data:
                    printer.qr(qr_data, size=6, center=True)
                if cut:
                    printer.cut()
            finally:
                printers.close_connector(printer)

        return send

    return _run_job_with_failover(targets, make_job_fn, description=job_name)


def run_pdf_job(
    mapping: dict[str, Any],
    images: list[Image.Image],
    job_name: str,
    cut_between_pages: bool = True,
) -> tuple[int, str]:
    """Print a rasterized multi-page PDF (see app/pdf_jobs.py) as one
    continuous ESC/POS spooler job - one page per rendered image, in order.
    Fails over across `mapping["targets"]` if needed (see
    `_run_job_with_failover`).

    Opens the printer connection once for the whole document (rather than
    once per page) so it behaves like a single physical print job with
    optional partial cuts between pages, the same way a receipt printer
    handles a multi-section receipt. The last page is always cut regardless
    of `cut_between_pages`, so the paper is left ready to tear off. If a
    retry/failover is needed partway through the pages, the whole document
    (all pages) is resent on a fresh connector, to whichever target is
    being tried - see `_run_job_with_retry`.

    Reuses `_send_image` - the same raster-image-plus-cut primitive used by
    `run_test_print_job` for the EN/AR test print - so PDF printing and the
    Arabic/RTL bitmap path share one ESC/POS raster implementation.

    :returns: `(attempts, served_by)` - see `_run_job_with_failover`.
    """
    if not images:
        raise PrintJobError("PDF has no pages to print")
    targets = mapping.get("targets") or []
    if not targets:
        raise PrintJobError("No printer targets configured for this mapping", attempts=0)
    last_index = len(images) - 1

    def make_job_fn(target: dict[str, Any]) -> Callable[[], None]:
        def send_all_pages() -> None:
            printer = printers.open_connector(target, job_name)
            try:
                for i, image in enumerate(images):
                    do_cut = True if i == last_index else cut_between_pages
                    _send_image(printer, image, do_cut)
            finally:
                printers.close_connector(printer)

        return send_all_pages

    return _run_job_with_failover(targets, make_job_fn, description=job_name)


def run_raw_job(mapping: dict[str, Any], data_base64: str, job_name: str) -> tuple[int, str]:
    """Decode base64 and write the bytes straight to the printer, failing
    over across `mapping["targets"]` if needed (see
    `_run_job_with_failover`).

    :returns: `(attempts, served_by)` - see `_run_job_with_failover`.
    """
    try:
        raw = base64.b64decode(data_base64, validate=True)
    except Exception as exc:
        raise PrintJobError(f"Invalid base64 data: {exc}") from exc

    targets = mapping.get("targets") or []
    if not targets:
        raise PrintJobError("No printer targets configured for this mapping", attempts=0)

    def make_job_fn(target: dict[str, Any]) -> Callable[[], None]:
        def send() -> None:
            printer = printers.open_connector(target, job_name)
            try:
                printer._raw(raw)
            finally:
                printers.close_connector(printer)

        return send

    return _run_job_with_failover(targets, make_job_fn, description=job_name)
