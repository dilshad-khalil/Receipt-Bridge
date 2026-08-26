"""Shared text-to-bitmap rendering primitives: Unicode font loading, Arabic
detection/shaping, word-wrapping, and single-line rasterization.

Used by:
- `app/test_print.py` - the combined connectivity + EN/AR test print image.
- `app/escpos_jobs.py` - per-line Arabic detection/rendering within
  `/print/text`, so a receipt mixing English and Arabic lines renders both
  correctly instead of only the dedicated test print getting this right.
- `app/main.py` - the `/print/text` dry-run preview image (`dry_run: true`),
  which lays out every line (Arabic or not) the same way a real job would.

Kept as one module rather than duplicated per-caller so there's exactly one
implementation of "detect/shape Arabic and rasterize a line of text" to get
right - the same reasoning already applied to `escpos_jobs._send_image()`
for the ESC/POS raster-send side of this pipeline.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import arabic_reshaper
from bidi.algorithm import get_display
from PIL import Image, ImageDraw, ImageFont

# Windows ships all three; Tahoma has historically had the most complete and
# legible Arabic glyph set, so it's tried first.
_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\tahoma.ttf",
    r"C:\Windows\Fonts\segoeui.ttf",
    r"C:\Windows\Fonts\arial.ttf",
]

# Arabic script codepoint blocks (Unicode 15) - covers standard Arabic,
# Arabic Supplement, Arabic Extended-A, and the two Arabic Presentation
# Forms blocks (compatibility ligatures some input methods/fonts produce).
# Deliberately Arabic-specific, not a general RTL-script detector: Hebrew
# and other RTL scripts don't need arabic_reshaper's letter-joining, so
# lumping them in here would be the wrong fix for a script this pipeline
# hasn't been built (or tested on real hardware) for.
_ARABIC_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")


class FontNotFoundError(Exception):
    """Raised when none of the candidate Windows fonts could be found."""


def contains_arabic(text: str) -> bool:
    """True if `text` contains any Arabic-script codepoint.

    This is the signal used to decide whether a line needs bitmap rendering
    (shaped + bidi-reordered, see `shape_arabic`) instead of native ESC/POS
    text mode: ESC/POS printers don't reliably shape Arabic (joining
    letters into their contextual initial/medial/final forms) or reorder it
    right-to-left in text mode, even when their codepage table includes the
    glyphs - they just print isolated letterforms in physical left-to-right
    order, which reads as broken Arabic.
    """
    return bool(_ARABIC_RE.search(text))


def shape_arabic(text: str) -> str:
    """Reshape Arabic codepoints into contextual (joined) letterforms, then
    reorder into left-to-right *visual* order - PIL draws strings character
    by character in the order given and doesn't run the bidi algorithm
    itself, so this has to happen before drawing.

    Safe to call on mixed Arabic/Latin text: `arabic_reshaper` only touches
    Arabic runs, and `get_display` reorders the whole string with the
    standard bidi algorithm, which keeps an embedded Latin run (e.g. a
    product code within an Arabic sentence) in left-to-right order within
    the overall right-to-left line.
    """
    return get_display(arabic_reshaper.reshape(text))


def load_font(size: int) -> ImageFont.FreeTypeFont:
    """Load the first available candidate Unicode TrueType font at `size`.

    Explicitly requests Pillow's `BASIC` layout engine rather than leaving
    it as the default. Left unset, `ImageFont.truetype()` silently uses
    Raqm (HarfBuzz-based complex text shaping) instead whenever the
    current Pillow build has it available - and that availability is not
    consistent: observed `False` when this exact Pillow/Python is run from
    a plain venv, but `True` for the identical package inside a
    PyInstaller-frozen build (whether Raqm's DLL gets bundled/discovered
    depends on the build environment, not anything this app controls).

    That matters because this whole pipeline already does its own Arabic
    shaping and bidi-reordering by hand before any text reaches a font -
    see `shape_arabic()` - and hands PIL text that's already been reshaped
    into presentation-form glyphs in final visual order. Raqm, given that
    already-shaped text, tries to shape and reorder it *again* (it has no
    way to know the input isn't plain logical-order text), which corrupts
    it into disconnected/garbled letterforms - the double-shaping only
    shows up wherever Raqm happens to be available, which is exactly why
    this could render correctly on one machine/build and break on another
    with no code change. `BASIC` layout draws exactly the glyphs for the
    codepoints it's given, once, which is what pre-shaped text needs.
    """
    for path in _FONT_CANDIDATES:
        if Path(path).exists():
            return ImageFont.truetype(path, size, layout_engine=ImageFont.Layout.BASIC)
    raise FontNotFoundError(
        "No Unicode TrueType font found (looked for Tahoma / Segoe UI / "
        r"Arial under C:\Windows\Fonts) - needed to render text as a bitmap."
    )


def wrap_to_width(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int
) -> list[str]:
    """Greedy word-wrap by measured pixel width. Wrapping happens on
    logical-order text (splitting on spaces is direction-agnostic); each
    wrapped line is shaped/reordered independently by the caller before
    drawing."""
    words = text.split(" ")
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if not current or draw.textlength(candidate, font=font) <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def render_line_image(
    text: str,
    width_px: int,
    *,
    align: str = "left",
    bold: bool = False,
    width_scale: int = 1,
    height_scale: int = 1,
) -> Image.Image:
    """Render one line of text as a single-row PIL image `width_px` dots
    wide, shaping + bidi-reordering it first if it contains Arabic.

    Two callers, two purposes:
    - `escpos_jobs._apply_line` sends this as an actual ESC/POS raster
      image in place of a native text command, for any line containing
      Arabic (see `contains_arabic`) - the real, printed output.
    - `render_lines_preview` (below) uses it to lay out every line - Arabic
      or not - for the `/print/text` dry-run preview image, so the preview
      matches what would actually print.

    This approximates ESC/POS's own text formatting (bold as a heavier
    stroke via PIL's `stroke_width`, width/height as independent horizontal/
    vertical scale) rather than pixel-matching the printer's built-in font -
    "good enough visual feedback", not a full ESC/POS emulator.
    """
    display_text = shape_arabic(text) if contains_arabic(text) else text
    height_scale = max(1, min(8, int(height_scale or 1)))
    width_scale = max(1, min(8, int(width_scale or 1)))
    stroke = 1 if bold else 0

    font_size = max(10, int(width_px * 0.052)) * height_scale
    font = load_font(font_size)

    probe = ImageDraw.Draw(Image.new("L", (1, 1)))
    # A fixed reference string (Latin ascender/descender + an Arabic letter)
    # so every line - even a blank one - gets the same row height, the same
    # way a printer's fixed line pitch doesn't shrink for a blank line.
    ref_bbox = font.getbbox("Ag\u062c|")
    line_height = (ref_bbox[3] - ref_bbox[1]) + stroke * 2 + 4
    text_width = int(probe.textlength(display_text, font=font)) + stroke * 2 if display_text else 0

    unscaled = Image.new("L", (max(1, text_width + 4), line_height), 255)
    if display_text:
        ImageDraw.Draw(unscaled).text(
            (2, 2 - ref_bbox[1]), display_text, font=font, fill=0, stroke_width=stroke, stroke_fill=0
        )
    if width_scale != 1:
        unscaled = unscaled.resize((max(1, unscaled.width * width_scale), unscaled.height))

    if unscaled.width > width_px:
        # A real printer would just run off the edge (or wrap, depending on
        # firmware) - but for a *preview*, silently cropping part of the
        # text off-canvas would misrepresent the content rather than just
        # its size. Shrink the whole line to fit instead, so long and/or
        # heavily-scaled text is still fully visible, just smaller than
        # requested.
        scale = width_px / unscaled.width
        unscaled = unscaled.resize((width_px, max(1, int(unscaled.height * scale))))

    canvas = Image.new("L", (width_px, unscaled.height), 255)
    if align == "center":
        x = (width_px - unscaled.width) // 2
    elif align == "right":
        x = width_px - unscaled.width
    else:
        x = 0
    canvas.paste(unscaled, (max(0, x), 0))
    return canvas


def render_lines_preview(
    lines: list[Any],
    width_px: int,
    barcode: Any | None = None,
    qr: Any | None = None,
) -> Image.Image:
    """Lay out a `/print/text` job's lines (plus a placeholder note for any
    barcode/QR) as one composite bitmap - the dry-run preview image
    returned by `POST /print/text` when `dry_run` is true.

    `lines`/`barcode`/`qr` are duck-typed the same way `escpos_jobs.py`'s
    `_apply_line`/`_apply_barcode`/`_apply_qr` already read them (`.text`/
    `.align`/`.bold`/`.width`/`.height`, `.type`/`.data`), so the actual
    Pydantic request models from `app/main.py` can be passed straight
    through with no adapting.

    A barcode/QR isn't rendered as a real barcode/QR image here (that's
    printer-native ESC/POS, not something rasterized client-side) - a text
    placeholder is enough to confirm one is present and see its data, which
    matches the brief: approximate the job, don't build a full emulator.
    """
    strips: list[Image.Image] = []
    for line in lines:
        text = getattr(line, "text", "") or ""
        align = (getattr(line, "align", None) or "left").lower()
        bold = bool(getattr(line, "bold", False))
        width_scale = getattr(line, "width", 1)
        height_scale = getattr(line, "height", 1)
        strips.append(
            render_line_image(
                text, width_px, align=align, bold=bold, width_scale=width_scale, height_scale=height_scale
            )
        )

    if barcode is not None:
        note = f"[barcode {getattr(barcode, 'type', '?')}: {getattr(barcode, 'data', '')}]"
        strips.append(render_line_image(note, width_px, align="center"))
    if qr is not None:
        note = f"[QR: {getattr(qr, 'data', '')}]"
        strips.append(render_line_image(note, width_px, align="center"))

    if not strips:
        strips.append(render_line_image("(empty job - no lines/barcode/QR)", width_px, align="center"))

    gap = 6
    total_height = sum(s.height for s in strips) + gap * (len(strips) + 1)
    canvas = Image.new("L", (width_px, total_height), 255)
    y = gap
    for strip in strips:
        canvas.paste(strip, (0, y))
        y += strip.height + gap
    return canvas
