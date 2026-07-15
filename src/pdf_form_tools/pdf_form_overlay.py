"""Detect form geometry and render text, checks, signatures, and PDF overlays."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from io import BytesIO
from pathlib import Path

import cv2
import fitz
import numpy as np
from bidi.algorithm import get_display
from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfReader, PdfWriter
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas


TEXT_COLOR = (20, 20, 20, 255)
WINDOWS_FONT_DIR = Path(os.environ["WINDIR"]) / "Fonts" if "WINDIR" in os.environ else None


def windows_font(name: str) -> list[Path]:
    return [WINDOWS_FONT_DIR / name] if WINDOWS_FONT_DIR is not None else []


FONT_CANDIDATES = {
    False: [
        *windows_font("arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
        Path("/Library/Fonts/Arial.ttf"),
        Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
    ],
    True: [
        *windows_font("arialbd.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"),
        Path("/Library/Fonts/Arial Bold.ttf"),
        Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
    ],
}


@dataclass(frozen=True)
class Rect:
    """Integer pixel rectangle used by detection and drawing helpers."""

    x: int
    y: int
    w: int
    h: int

    @property
    def x2(self) -> int:
        return self.x + self.w

    @property
    def y2(self) -> int:
        return self.y + self.h

    def inset(self, dx: int, dy: int | None = None) -> "Rect":
        if dy is None:
            dy = dx
        return Rect(self.x + dx, self.y + dy, self.w - dx * 2, self.h - dy * 2)

    def band(
        self,
        *,
        top_pad: int,
        height: int,
        left_pad: int = 0,
        right_pad: int | None = None,
    ) -> "Rect":
        if right_pad is None:
            right_pad = left_pad
        return Rect(self.x + left_pad, self.y + top_pad, self.w - left_pad - right_pad, height)

    def above(
        self,
        *,
        height: int,
        gap: int = 0,
        left_pad: int = 0,
        right_pad: int | None = None,
    ) -> "Rect":
        if right_pad is None:
            right_pad = left_pad
        return Rect(self.x + left_pad, self.y - gap - height, self.w - left_pad - right_pad, height)


def contains_hebrew(text: str) -> bool:
    return any("\u0590" <= ch <= "\u05FF" for ch in text)


def visual_text(text: str) -> str:
    return get_display(text) if contains_hebrew(text) else text


@lru_cache(maxsize=2)
def resolve_font_path(bold: bool = False) -> Path:
    for candidate in FONT_CANDIDATES[bold]:
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"Could not find a usable {'bold' if bold else 'regular'} TrueType font.")


def load_font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    font_path = resolve_font_path(bold=bold)
    return ImageFont.truetype(str(font_path), size)


def close_small_gaps(mask: np.ndarray, max_gap: int = 4) -> np.ndarray:
    result = mask.copy()
    start = None
    for idx, value in enumerate(mask):
        if not value and start is None:
            start = idx
        elif value and start is not None:
            if idx - start <= max_gap:
                result[start:idx] = True
            start = None
    if start is not None and len(mask) - start <= max_gap:
        result[start:] = True
    return result


def longest_true_segment(mask: np.ndarray, min_len: int) -> tuple[int, int] | None:
    best = None
    start = None
    for idx, value in enumerate(mask):
        if value and start is None:
            start = idx
        elif not value and start is not None:
            if idx - start >= min_len and (best is None or idx - start > best[1] - best[0]):
                best = (start, idx)
            start = None
    if start is not None and len(mask) - start >= min_len:
        candidate = (start, len(mask))
        if best is None or candidate[1] - candidate[0] > best[1] - best[0]:
            best = candidate
    return best


def writable_box(page_gray: np.ndarray, rect: Rect, row_threshold: float = 0.015, col_threshold: float = 0.03) -> Rect:
    """Locate a low-ink writable area within a grayscale form region."""

    inner = rect.inset(8)
    crop = page_gray[inner.y:inner.y2, inner.x:inner.x2]
    ink = crop < 185

    row_density = ink.mean(axis=1)
    row_mask = row_density < row_threshold
    row_mask[:4] = False
    row_mask[-4:] = False
    row_mask = close_small_gaps(row_mask, max_gap=5)
    row_segment = longest_true_segment(row_mask, min_len=max(18, crop.shape[0] // 6))
    if row_segment is None:
        row_segment = (crop.shape[0] // 3, crop.shape[0] - 12)

    band = crop[row_segment[0]:row_segment[1], :]
    band_ink = band < 185
    col_density = band_ink.mean(axis=0)
    col_mask = col_density < col_threshold
    col_mask[:6] = False
    col_mask[-6:] = False
    col_mask = close_small_gaps(col_mask, max_gap=8)
    col_segment = longest_true_segment(col_mask, min_len=max(40, crop.shape[1] // 6))
    if col_segment is None:
        col_segment = (10, crop.shape[1] - 10)

    box = Rect(
        inner.x + col_segment[0],
        inner.y + row_segment[0],
        col_segment[1] - col_segment[0],
        row_segment[1] - row_segment[0],
    )
    return box.inset(4)


def fit_font(
    draw: ImageDraw.ImageDraw,
    text: str,
    rect: Rect,
    max_size: int,
    min_size: int,
    bold: bool,
) -> tuple[ImageFont.FreeTypeFont, tuple[int, int, int, int]]:
    prepared = visual_text(text)
    for size in range(max_size, min_size - 1, -2):
        font = load_font(size, bold=bold)
        bbox = draw.textbbox((0, 0), prepared, font=font)
        width = bbox[2] - bbox[0]
        height = bbox[3] - bbox[1]
        if width <= rect.w and height <= rect.h:
            return font, bbox
    font = load_font(min_size, bold=bold)
    bbox = draw.textbbox((0, 0), prepared, font=font)
    return font, bbox


def draw_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    rect: Rect,
    *,
    align: str,
    max_size: int,
    min_size: int,
    bold: bool = False,
    fill: tuple[int, int, int, int] = TEXT_COLOR,
) -> None:
    """Draw fitted, direction-aware text inside a rectangle."""

    prepared = visual_text(text)
    font, bbox = fit_font(draw, text, rect, max_size=max_size, min_size=min_size, bold=bold)
    width = bbox[2] - bbox[0]
    height = bbox[3] - bbox[1]

    if align == "right":
        x = rect.x2 - width - bbox[0]
    elif align == "left":
        x = rect.x - bbox[0]
    else:
        x = rect.x + (rect.w - width) / 2 - bbox[0]

    y = rect.y + (rect.h - height) / 2 - bbox[1]
    draw.text((x, y), prepared, font=font, fill=fill)


def centered_address_box(rect: Rect, *, top_pad: int, side_pad: int, height: int, right_pad: int | None = None) -> Rect:
    """Return a padded horizontal band for centered address text."""

    return rect.band(top_pad=top_pad, height=height, left_pad=side_pad, right_pad=right_pad)


def detect_square_boxes(page_gray: np.ndarray, region: Rect) -> list[Rect]:
    """Detect checkbox-sized square contours in a grayscale page region."""

    crop = page_gray[region.y:region.y2, region.x:region.x2]
    _, thresh = cv2.threshold(crop, 210, 255, cv2.THRESH_BINARY_INV)
    contours, _ = cv2.findContours(thresh, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    boxes: list[Rect] = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if 40 <= w <= 60 and 40 <= h <= 60 and 0.8 <= (w / h) <= 1.25:
            candidate = Rect(region.x + x, region.y + y, w, h)
            if any(abs(candidate.x - existing.x) < 5 and abs(candidate.y - existing.y) < 5 for existing in boxes):
                continue
            boxes.append(candidate)
    return sorted(boxes, key=lambda item: (item.y, item.x))


def detect_lines(page_gray: np.ndarray, region: Rect) -> list[Rect]:
    """Detect long horizontal form lines in a grayscale page region."""

    crop = page_gray[region.y:region.y2, region.x:region.x2]
    _, thresh = cv2.threshold(crop, 200, 255, cv2.THRESH_BINARY_INV)
    contours, _ = cv2.findContours(thresh, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    lines: list[Rect] = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if 500 <= w <= 900 and h <= 12:
            candidate = Rect(region.x + x, region.y + y, w, h)
            if any(abs(candidate.x - existing.x) < 10 and abs(candidate.y - existing.y) < 10 for existing in lines):
                continue
            lines.append(candidate)
    return sorted(lines, key=lambda item: item.x)


def detect_id_slots(page_gray: np.ndarray, rect: Rect) -> list[Rect]:
    """Detect the nine digit slots within an Israeli ID-number field."""

    crop = page_gray[rect.y:rect.y2, rect.x:rect.x2]
    guide_start = int(crop.shape[0] * 0.7)
    lower = crop[guide_start:, :]
    ink = lower < 180

    row_sum = ink.sum(axis=1)
    first_guide_row = next((idx + guide_start for idx, value in enumerate(row_sum) if value >= 6), int(crop.shape[0] * 0.82))

    col_sum = ink.sum(axis=0)
    peak_columns = [idx for idx, value in enumerate(col_sum) if value >= 8]
    ranges: list[tuple[int, int]] = []
    start = None
    prev = None
    for idx in peak_columns:
        if start is None:
            start = idx
            prev = idx
            continue
        if idx == prev + 1:
            prev = idx
            continue
        ranges.append((start, prev))
        start = idx
        prev = idx
    if start is not None and prev is not None:
        ranges.append((start, prev))

    boundaries = [0]
    for left, right in ranges:
        center = int(round((left + right) / 2))
        if 4 < center < crop.shape[1] - 5:
            boundaries.append(center)
    boundaries.append(crop.shape[1] - 1)
    boundaries = sorted(set(boundaries))

    if len(boundaries) != 10:
        raise RuntimeError(f"Expected 10 ID slot boundaries, found {len(boundaries)} for {rect}.")

    digit_top = rect.y + first_guide_row - int(rect.h * 0.34)
    digit_height = int(rect.h * 0.48)
    slots: list[Rect] = []
    for left, right in zip(boundaries, boundaries[1:]):
        slots.append(Rect(rect.x + left + 3, digit_top, right - left - 6, digit_height))
    return slots


def draw_id_number(draw: ImageDraw.ImageDraw, page_gray: np.ndarray, rect: Rect, number: str) -> None:
    """Draw one ID-number digit in each detected slot."""

    slots = detect_id_slots(page_gray, rect)
    if len(number) != len(slots):
        raise RuntimeError(f"ID length {len(number)} does not match detected slot count {len(slots)}.")
    for digit, slot in zip(number, slots):
        draw_text(draw, digit, slot, align="center", max_size=74, min_size=54)


def draw_check(
    draw: ImageDraw.ImageDraw,
    rect: Rect,
    *,
    raise_px: int = 10,
    fill: tuple[int, int, int, int] = TEXT_COLOR,
) -> None:
    """Draw a check mark inside a rectangle."""

    x0, y0 = rect.x, rect.y
    width = max(10, rect.w // 4)
    p1 = (x0 + rect.w * 0.18, y0 + rect.h * 0.54 - raise_px)
    p2 = (x0 + rect.w * 0.43, y0 + rect.h * 0.80 - raise_px)
    p3 = (x0 + rect.w * 0.83, y0 + rect.h * 0.20 - raise_px)
    draw.line([p1, p2], fill=fill, width=width)
    draw.line([p2, p3], fill=fill, width=width)


def paste_signature(
    overlay: Image.Image,
    signature: Image.Image,
    line_rect: Rect,
    *,
    min_cm_width: float = 2.0,
    target_height: int | None = None,
    y_offset: int = 45,
) -> None:
    """Scale and alpha-composite a signature above a form line."""

    alpha_bbox = signature.getchannel("A").getbbox()
    if alpha_bbox:
        signature = signature.crop(alpha_bbox)

    min_signature_width = int(round((overlay.width / 21.0) * min_cm_width))
    target_width = min(line_rect.w, max(min_signature_width, int(line_rect.w * 0.55)))
    width_scale = target_width / signature.width
    if target_height is None:
        scale = width_scale
    else:
        scale = min(width_scale, target_height / signature.height)

    resized_width = max(1, int(round(signature.width * scale)))
    resized_height = max(1, int(round(signature.height * scale)))
    resized = signature.resize((resized_width, resized_height), Image.Resampling.LANCZOS)
    x = int(line_rect.x + (line_rect.w - resized_width) / 2)
    y = int(line_rect.y - resized_height + y_offset)
    overlay.alpha_composite(resized, (x, y))


def render_pdf_page(pdf_path: Path, page_index: int, scale: int, out_path: Path) -> Image.Image:
    """Render one PDF page to an image, save it, and return the image."""

    document = fitz.open(pdf_path)
    try:
        page = document[page_index]
        pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
        image.save(out_path)
        return image
    finally:
        document.close()


def merge_overlay_pdf(src_pdf: Path, overlay_png: Path, out_pdf: Path) -> None:
    """Write a PDF with a PNG overlay merged onto the first source page."""

    reader = PdfReader(str(src_pdf))
    writer = PdfWriter()

    page = reader.pages[0]
    width = float(page.mediabox.width)
    height = float(page.mediabox.height)

    overlay_buffer = BytesIO()
    c = canvas.Canvas(overlay_buffer, pagesize=(width, height))
    c.drawImage(ImageReader(str(overlay_png)), 0, 0, width=width, height=height, mask="auto")
    c.save()
    overlay_buffer.seek(0)
    overlay_reader = PdfReader(overlay_buffer)

    writer.add_page(page)
    writer.pages[0].merge_page(overlay_reader.pages[0])
    for extra_page in reader.pages[1:]:
        writer.add_page(extra_page)

    with out_pdf.open("wb") as handle:
        writer.write(handle)
