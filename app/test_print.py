"""The single "Test print" action: a connectivity check and the bilingual
(English + Arabic) test quotes, printed as one combined job. Used by the
config UI's "Test print" button (see POST /print/test in app/main.py).

Everything text-shaped (the header, the connectivity line, both quotes) is
rendered together into one PIL image rather than mixed with native ESC/POS
text commands, for one reason: ESC/POS printers don't reliably do Arabic
contextual letter-shaping or right-to-left reordering in text mode - even
printers whose codepage table includes Arabic glyphs typically just print
isolated letterforms in physical left-to-right order, which reads as broken
Arabic. The reliable, printer-model-agnostic fix (what most real-world
Arabic-market POS systems actually do) is to render the whole thing as a
bitmap: shape + bidi-reorder the Arabic text with arabic_reshaper/python-bidi,
draw both languages with a Unicode TrueType font, and send the result as one
ESC/POS raster image - that bypasses printer codepage/font support entirely,
since it's just dots. Since the connectivity header sits directly above the
quotes with a plain horizontal rule between them, it goes into the same
image rather than being printed separately in ESC/POS text mode first (see
app/escpos_jobs.py's run_test_print_job for how a QR code - genuinely
native ESC/POS - still gets appended after this image in the same job).
"""
from __future__ import annotations

from pathlib import Path

import arabic_reshaper
from bidi.algorithm import get_display
from PIL import Image, ImageDraw, ImageFont

QUOTE_EN = "The best way to predict the future is to create it."
ATTRIBUTION_EN = "Peter Drucker"

QUOTE_AR = "أفضل وسيلة للتنبؤ بالمستقبل هي أن تصنعه بنفسك."
ATTRIBUTION_AR = "بيتر دركر"

# Windows ships all three; Tahoma has historically had the most complete and
# legible Arabic glyph set, so it's tried first.
_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\tahoma.ttf",
    r"C:\Windows\Fonts\segoeui.ttf",
    r"C:\Windows\Fonts\arial.ttf",
]


class FontNotFoundError(Exception):
    """Raised when none of the candidate Windows fonts could be found."""


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    for path in _FONT_CANDIDATES:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    raise FontNotFoundError(
        "No Unicode TrueType font found (looked for Tahoma / Segoe UI / "
        r"Arial under C:\Windows\Fonts) - needed to render the test print "
        "as a bitmap."
    )


def _shape_arabic(text: str) -> str:
    """Reshape Arabic codepoints into contextual (joined) letterforms, then
    reorder into left-to-right *visual* order - PIL draws strings character
    by character in the order given and doesn't run the bidi algorithm
    itself, so this has to happen before drawing."""
    return get_display(arabic_reshaper.reshape(text))


def _wrap_to_width(
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


def build_test_print_image(width_px: int = 384, printer_label: str = "") -> Image.Image:
    """Render the combined test print - a connectivity header, a horizontal
    rule, then the bilingual EN/AR test quotes - as one PIL image sized for
    a thermal printhead `width_px` dots wide (384 = a safe default for
    narrow 58mm printers; use 576 for a typical 80mm printer). Ready to hand
    straight to python-escpos's `.image()` (see escpos_jobs.run_test_print_job).

    :param printer_label: the logical printer name, shown in the
        connectivity line ("printing from <printer_label> works") so the
        printout confirms which mapping produced it.
    """
    margin = max(10, int(width_px * 0.07))
    content_width = width_px - 2 * margin

    title_font = _load_font(int(width_px * 0.088))
    subtitle_font = _load_font(int(width_px * 0.040))
    quote_font = _load_font(int(width_px * 0.058))
    attribution_font = _load_font(int(width_px * 0.046))
    footer_font = _load_font(int(width_px * 0.034))

    # Render on a generously tall scratch canvas, then crop to actual
    # content height - simpler than pre-computing exact heights up front.
    scratch_height = width_px * 7
    img = Image.new("L", (width_px, scratch_height), 255)
    draw = ImageDraw.Draw(img)

    y = int(width_px * 0.09)

    def line_gap(font: ImageFont.FreeTypeFont, ratio: float) -> int:
        bbox = font.getbbox("Ag\u062c")  # includes an Arabic letter w/ descender-ish shape
        return int((bbox[3] - bbox[1]) * (1 + ratio))

    def draw_centered(text: str, font: ImageFont.FreeTypeFont, gap_ratio: float = 0.45) -> None:
        nonlocal y
        w = draw.textlength(text, font=font)
        draw.text(((width_px - w) / 2, y), text, font=font, fill=0)
        y += line_gap(font, gap_ratio)

    def draw_divider(gap_ratio: float = 1.1) -> None:
        nonlocal y
        draw.line([(margin, y), (width_px - margin, y)], fill=0, width=2)
        y += int(width_px * 0.05)

    # --- Connectivity header ---------------------------------------------
    draw_centered("PRINT BRIDGE TEST", title_font, gap_ratio=0.15)
    connectivity_line = (
        f'If you can read this, printing from "{printer_label}" works.'
        if printer_label
        else "If you can read this, printing works."
    )
    for line in _wrap_to_width(draw, connectivity_line, subtitle_font, content_width):
        draw_centered(line, subtitle_font, gap_ratio=0.5)
    y += int(width_px * 0.02)
    draw_divider()

    # --- Bilingual quotes ---------------------------------------------
    en_text = f"\u201c{QUOTE_EN}\u201d"
    for line in _wrap_to_width(draw, en_text, quote_font, content_width):
        draw_centered(line, quote_font)
    draw_centered(f"\u2014 {ATTRIBUTION_EN}", attribution_font, gap_ratio=0.9)
    draw_divider()

    ar_text = f"\u00AB{QUOTE_AR}\u00BB"
    for line in _wrap_to_width(draw, ar_text, quote_font, content_width):
        draw_centered(_shape_arabic(line), quote_font)
    draw_centered(_shape_arabic(f"\u2014 {ATTRIBUTION_AR}"), attribution_font, gap_ratio=0.9)
    draw_divider()

    draw_centered("Print Bridge - ESC/POS bitmap test", footer_font, gap_ratio=0.2)

    return img.crop((0, 0, width_px, min(y + margin, scratch_height)))
