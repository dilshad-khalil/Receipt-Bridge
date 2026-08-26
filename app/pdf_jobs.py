"""PDF rasterization for `POST /print/pdf`.

**Why rasterize instead of trying to interpret the PDF's content stream
directly?** A PDF's own layout engine has already solved text shaping,
right-to-left reordering, font embedding/substitution, tables, and mixed
scripts correctly at PDF-authoring time - reimplementing any of that
server-side (to instead emit ESC/POS text commands) would mean re-solving
problems PDF already solved, and would still fail for anything ESC/POS text
mode can't represent (logos, complex layout). Treating each page as a fixed
image sidesteps all of that: whatever the PDF looks like on screen is
what gets printed, in one code path that already exists for the bilingual
EN/AR test print (see app/test_print.py and app/escpos_jobs.run_pdf_job).
The trade-off is the same one that path already accepts: a bitmap can't be
edited/reflowed by the printer, but a receipt/label doesn't need to be.

Uses PyMuPDF (the `fitz` module) rather than `pdf2image`, because PyMuPDF is
a self-contained wheel with its own PDF renderer - no external Poppler
binary to install/bundle - which matters for a single-file PyInstaller
distributable.
"""
from __future__ import annotations

import pymupdf as fitz  # PyMuPDF - `pymupdf` is the current import name, `fitz` the historical alias
from PIL import Image


class PdfRenderError(Exception):
    """Raised when the uploaded bytes can't be parsed/rendered as a PDF."""


def rasterize_pdf(pdf_bytes: bytes, dpi: int, width_px: int) -> list[Image.Image]:
    """Render every page of a PDF to a 1-bit PIL image `width_px` dots wide.

    :param pdf_bytes: the raw uploaded PDF file content.
    :param dpi: render resolution - should match the printer's native DPI
        (see the per-printer `dpi` config field) so text/line weight comes
        out at a sensible physical size rather than needing rescaling.
    :param width_px: target width in printer dots (384 for a 58mm printer,
        576 for 80mm, etc.) - the render is uniformly scaled to this width
        after rasterizing, preserving aspect ratio.
    :returns: one 1-bit PIL image per page, in page order.
    :raises PdfRenderError: if the bytes aren't a readable PDF, or it has no
        pages.
    """
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:
        raise PdfRenderError(f"Could not read PDF: {exc}") from exc

    try:
        if doc.page_count == 0:
            raise PdfRenderError("PDF has no pages")

        # PDF's native unit is 1/72 inch, and fitz's page-to-pixmap matrix
        # is expressed relative to that 72dpi base - so the zoom factor
        # needed to render at a target DPI is simply dpi/72.
        zoom = dpi / 72.0
        matrix = fitz.Matrix(zoom, zoom)

        images: list[Image.Image] = []
        for page_index in range(doc.page_count):
            page = doc.load_page(page_index)
            pix = page.get_pixmap(matrix=matrix, colorspace=fitz.csRGB, alpha=False)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)

            if img.width != width_px and img.width > 0:
                new_height = max(1, round(img.height * (width_px / img.width)))
                img = img.resize((width_px, new_height), Image.LANCZOS)

            # Floyd-Steinberg error-diffusion dithering (rather than a hard
            # black/white threshold) so photos/logos/gradients in the PDF
            # come out as a reasonable halftone pattern instead of clipping
            # to solid black/white blobs. escpos_jobs._send_image() ->
            # python-escpos's printer.image() will re-derive its own 1-bit
            # image from whatever mode we hand it, but that re-derivation is
            # a no-op on pixels that are already pure black/white, so
            # dithering here is what actually determines the output.
            img = img.convert("L").convert("1", dither=Image.FLOYDSTEINBERG)
            images.append(img)
        return images
    finally:
        doc.close()
