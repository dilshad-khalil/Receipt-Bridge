"""Builds and sends ESC/POS print jobs via python-escpos's Win32Raw connector.

python-escpos's Win32Raw does not open the Windows print spooler job in its
constructor - `.open()` must be called explicitly (it starts the spooler doc,
`.close()` ends/closes it). We do that ourselves here so job lifetime is
explicit and every job gets its own spooler document.
"""
from __future__ import annotations

import base64
import logging
from typing import Any

import pywintypes
from escpos.exceptions import Error as EscposError
from escpos.printer import Win32Raw
from PIL import Image

logger = logging.getLogger("print_bridge")

JOB_NAME_PREFIX = "PrintBridge"


class PrintJobError(Exception):
    """Raised whenever a job could not be delivered to the Windows printer.

    Callers (main.py) turn this into an HTTP 502 with the message as detail.
    """


def _open(windows_printer_name: str, job_name: str) -> Win32Raw:
    printer = Win32Raw(windows_printer_name)
    try:
        printer.open(job_name=job_name)
    except (EscposError, pywintypes.error) as exc:
        raise PrintJobError(
            f"Windows printer '{windows_printer_name}' is not available: {exc}"
        ) from exc
    return printer


def _close(printer: Win32Raw) -> None:
    try:
        printer.close()
    except Exception:  # best-effort cleanup, never mask the real error
        logger.exception("Error closing printer connection")


def _clamp_scale(value: int | None) -> int:
    if not value:
        return 1
    return max(1, min(8, int(value)))


def _apply_line(printer: Win32Raw, line: Any) -> None:
    width = _clamp_scale(getattr(line, "width", 1))
    height = _clamp_scale(getattr(line, "height", 1))
    align = (getattr(line, "align", None) or "left").lower()
    if align not in ("left", "center", "right"):
        raise PrintJobError(f"Invalid align '{align}', must be left/center/right")
    # custom_size is always passed explicitly (even for width=height=1) so
    # that every line resets text scale rather than inheriting the previous
    # line's size - GS ! is a persistent printer mode, not per-line.
    printer.set(
        align=align,
        bold=bool(getattr(line, "bold", False)),
        custom_size=True,
        width=width,
        height=height,
    )
    printer.text((line.text or "") + "\n")


def _apply_barcode(printer: Win32Raw, barcode: Any) -> None:
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


def _apply_qr(printer: Win32Raw, qr: Any) -> None:
    try:
        printer.qr(qr.data, size=6, center=True)
    except EscposError as exc:
        raise PrintJobError(f"Could not render QR code: {exc}") from exc


def run_text_job(windows_printer_name: str, payload: Any, job_name: str) -> None:
    """Render `payload` (a PrintTextRequest) and send it to the printer.

    `copies` sends the full sequence (text/barcode/qr/cut/drawer) that many
    times as separate spooler documents, so a jam/paper-out on one copy
    doesn't silently skip the rest.
    """
    copies = max(1, payload.copies or 1)
    for copy_num in range(1, copies + 1):
        name = job_name if copies == 1 else f"{job_name} ({copy_num}/{copies})"
        printer = _open(windows_printer_name, name)
        try:
            for line in payload.lines or []:
                _apply_line(printer, line)
            if payload.barcode:
                _apply_barcode(printer, payload.barcode)
            if payload.qr:
                _apply_qr(printer, payload.qr)
            if payload.open_drawer:
                printer.cashdraw(2)
            if payload.cut:
                printer.cut()
        except (EscposError, pywintypes.error) as exc:
            raise PrintJobError(str(exc)) from exc
        finally:
            _close(printer)


def _send_image(printer: Win32Raw, image: Image.Image, cut: bool) -> None:
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
    windows_printer_name: str,
    image: Image.Image,
    job_name: str,
    qr_data: str | None = None,
    cut: bool = True,
) -> None:
    """Print the combined "Test print" job: the pre-rendered connectivity
    header + EN/AR test quotes image (see app/test_print.py), optionally
    followed by a native ESC/POS QR code, then a cut.

    Mixes raster (`_send_image`) and native ESC/POS commands (`printer.qr`,
    the cut) within a single open/close spooler document - printers process
    whatever commands they're sent in order regardless of kind, so a bitmap
    followed by native QR/cut commands is no different from any other
    sequence of ESC/POS commands in one job.
    """
    printer = _open(windows_printer_name, job_name)
    try:
        _send_image(printer, image, cut=False)
        if qr_data:
            printer.qr(qr_data, size=6, center=True)
        if cut:
            printer.cut()
    except (EscposError, pywintypes.error) as exc:
        raise PrintJobError(f"Could not print test page: {exc}") from exc
    finally:
        _close(printer)


def run_pdf_job(
    windows_printer_name: str,
    images: list[Image.Image],
    job_name: str,
    cut_between_pages: bool = True,
) -> None:
    """Print a rasterized multi-page PDF (see app/pdf_jobs.py) as one
    continuous ESC/POS spooler job - one page per rendered image, in order.

    Opens the printer connection once for the whole document (rather than
    once per page) so it behaves like a single physical print job with
    optional partial cuts between pages, the same way a receipt printer
    handles a multi-section receipt. The last page is always cut regardless
    of `cut_between_pages`, so the paper is left ready to tear off.

    Reuses `_send_image` - the same raster-image-plus-cut primitive used by
    `run_test_print_job` for the EN/AR test print - so PDF printing and the
    Arabic/RTL bitmap path share one ESC/POS raster implementation.
    """
    if not images:
        raise PrintJobError("PDF has no pages to print")
    printer = _open(windows_printer_name, job_name)
    last_index = len(images) - 1
    try:
        for i, image in enumerate(images):
            do_cut = True if i == last_index else cut_between_pages
            _send_image(printer, image, do_cut)
    except (EscposError, pywintypes.error) as exc:
        raise PrintJobError(f"Could not print PDF page {i + 1}: {exc}") from exc
    finally:
        _close(printer)


def run_raw_job(windows_printer_name: str, data_base64: str, job_name: str) -> None:
    """Decode base64 and write the bytes straight to the printer."""
    try:
        raw = base64.b64decode(data_base64, validate=True)
    except Exception as exc:
        raise PrintJobError(f"Invalid base64 data: {exc}") from exc

    printer = _open(windows_printer_name, job_name)
    try:
        printer._raw(raw)
    except (EscposError, pywintypes.error) as exc:
        raise PrintJobError(str(exc)) from exc
    finally:
        _close(printer)
