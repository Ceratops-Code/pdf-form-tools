"""Detect form geometry and render generic content onto PDF overlays."""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from functools import lru_cache
from io import BytesIO
from itertools import pairwise
from pathlib import Path
from typing import Any, Literal

import cv2
import numpy as np
import pymupdf as fitz
from bidi.algorithm import get_display
from jsonschema import Draft202012Validator
from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfReader, PdfWriter
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

TEXT_COLOR = (20, 20, 20, 255)
FORM_RECIPE_SCHEMA = "pdf-form-tools.form-recipe.v2"
WINDOWS_FONT_DIR = Path(os.environ["WINDIR"]) / "Fonts" if "WINDIR" in os.environ else None

_RECT_SCHEMA = {
    "type": "array",
    "prefixItems": [
        {"type": "integer", "minimum": 0},
        {"type": "integer", "minimum": 0},
        {"type": "integer", "minimum": 1},
        {"type": "integer", "minimum": 1},
    ],
    "items": False,
    "minItems": 4,
    "maxItems": 4,
}
FORM_RECIPE_JSON_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["schema", "template", "render", "fields"],
    "properties": {
        "schema": {"const": FORM_RECIPE_SCHEMA},
        "template": {
            "type": "object",
            "additionalProperties": False,
            "required": ["id", "version"],
            "properties": {
                "id": {
                    "type": "string",
                    "pattern": "^[a-z0-9][a-z0-9_-]*$",
                },
                "version": {"type": "integer", "minimum": 1},
            },
        },
        "render": {
            "type": "object",
            "additionalProperties": False,
            "required": ["page_index", "scale", "expected_size"],
            "properties": {
                "page_index": {"type": "integer", "minimum": 0},
                "scale": {"type": "integer", "minimum": 1},
                "expected_size": {
                    "type": "array",
                    "prefixItems": [
                        {"type": "integer", "minimum": 1},
                        {"type": "integer", "minimum": 1},
                    ],
                    "items": False,
                    "minItems": 2,
                    "maxItems": 2,
                },
            },
        },
        "fields": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "additionalProperties": False,
                "required": ["value", "rect", "align", "max_size", "min_size"],
                "properties": {
                    "value": {"type": "string"},
                    "rect": _RECT_SCHEMA,
                    "align": {"enum": ["left", "center", "right"]},
                    "max_size": {"type": "integer", "minimum": 1},
                    "min_size": {"type": "integer", "minimum": 1},
                    "bold": {"type": "boolean"},
                },
            },
        },
        "circles": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "additionalProperties": False,
                "required": ["rect"],
                "properties": {
                    "rect": _RECT_SCHEMA,
                    "stroke_width": {"type": "integer", "minimum": 1},
                },
            },
        },
    },
}
_FORM_RECIPE_VALIDATOR = Draft202012Validator(FORM_RECIPE_JSON_SCHEMA)


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

    def inset(self, dx: int, dy: int | None = None) -> Rect:
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
    ) -> Rect:
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
    ) -> Rect:
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


def detect_guided_slots(
    page_gray: np.ndarray,
    region: Rect,
    *,
    expected_count: int,
) -> list[Rect]:
    """Detect text slots divided by vertical guides in a region's lower band."""

    if expected_count < 1:
        raise ValueError("Expected slot count must be positive.")
    crop = page_gray[region.y:region.y2, region.x:region.x2]
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
        center = round((left + right) / 2)
        if 4 < center < crop.shape[1] - 5:
            boundaries.append(center)
    boundaries.append(crop.shape[1] - 1)
    boundaries = sorted(set(boundaries))

    expected_boundaries = expected_count + 1
    if len(boundaries) != expected_boundaries:
        raise RuntimeError(
            f"Expected {expected_boundaries} slot boundaries, "
            f"found {len(boundaries)} for {region}."
        )

    slot_top = region.y + first_guide_row - int(region.h * 0.34)
    slot_height = int(region.h * 0.48)
    slots: list[Rect] = []
    for left, right in pairwise(boundaries):
        slots.append(
            Rect(
                region.x + left + 3,
                slot_top,
                right - left - 6,
                slot_height,
            )
        )
    return slots


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


def draw_circle(
    draw: ImageDraw.ImageDraw,
    rect: Rect,
    *,
    stroke_width: int = 4,
    fill: tuple[int, int, int, int] = TEXT_COLOR,
) -> None:
    """Draw an outline ellipse inside one recipe rectangle."""

    if stroke_width < 1 or stroke_width * 2 >= min(rect.w, rect.h):
        raise ValueError("Circle stroke must leave a visible interior.")
    inset = stroke_width // 2
    draw.ellipse(
        (
            rect.x + inset,
            rect.y + inset,
            rect.x2 - inset - 1,
            rect.y2 - inset - 1,
        ),
        outline=fill,
        width=stroke_width,
    )


def _occupancy_mask(image: Image.Image) -> np.ndarray:
    """Return visible non-white pixels for deterministic placement scoring."""

    rgba = np.asarray(image.convert("RGBA"), dtype=np.uint8)
    visible = rgba[:, :, 3] > 8
    non_white = np.min(rgba[:, :, :3], axis=2) < 245
    return np.asarray(visible & non_white, dtype=np.uint8)


def _nearby_axis_candidates(
    preferred: int,
    extent: int,
    page_extent: int,
    radius: int,
    step: int,
) -> list[int]:
    maximum = page_extent - extent
    if maximum < 0:
        return []
    lower = max(0, preferred - radius)
    upper = min(maximum, preferred + radius)
    if lower > upper:
        return []
    values = set(range(lower, upper + 1, step))
    values.update({lower, upper, min(max(preferred, lower), upper)})
    return sorted(values)


def place_image_near_rect(
    image: Image.Image,
    anchor: Rect,
    page: Image.Image,
    protected_regions: Iterable[Rect] = (),
    *,
    occupancy_image: Image.Image | None = None,
    horizontal_align: Literal["left", "center", "right"] = "center",
) -> Rect:
    """Place an already-sized image in nearby low-occupancy page space.

    The image is never cropped or resized. The search is bounded to one maximum
    image-or-anchor extent around the preferred position immediately above the
    anchor. Candidates are ranked by occupied pixels and then distance, with
    stable coordinate tie-breaks. Protected rectangles are hard exclusions;
    ordinary page content is only a score penalty and may be overlapped.
    ``occupancy_image`` can supply immutable background content while ``page``
    remains the mutable RGBA target.
    """

    if page.mode != "RGBA":
        raise ValueError("Placement page must use RGBA mode.")
    if image.width < 1 or image.height < 1:
        raise ValueError("Placement image must have positive dimensions.")
    if image.convert("RGBA").getchannel("A").getbbox() is None:
        raise ValueError("Placement image has no visible pixels.")
    if anchor.w < 1 or anchor.h < 1:
        raise ValueError("Placement anchor must have positive dimensions.")
    if (
        anchor.x < 0
        or anchor.y < 0
        or anchor.x2 > page.width
        or anchor.y2 > page.height
    ):
        raise ValueError(f"Placement anchor extends outside the page: {anchor}.")
    if horizontal_align not in {"left", "center", "right"}:
        raise ValueError(f"Unsupported horizontal alignment: {horizontal_align}")
    if occupancy_image is not None and occupancy_image.size != page.size:
        raise ValueError("Occupancy image and placement page must have the same size.")

    if horizontal_align == "left":
        preferred_x = anchor.x
    elif horizontal_align == "right":
        preferred_x = anchor.x2 - image.width
    else:
        preferred_x = anchor.x + (anchor.w - image.width) // 2
    preferred_y = anchor.y - image.height

    radius = max(image.width, image.height, anchor.w, anchor.h)
    step = max(1, min(image.width, image.height) // 20)
    x_candidates = _nearby_axis_candidates(
        preferred_x,
        image.width,
        page.width,
        radius,
        step,
    )
    y_candidates = _nearby_axis_candidates(
        preferred_y,
        image.height,
        page.height,
        radius,
        step,
    )
    if not x_candidates or not y_candidates:
        raise RuntimeError("The supplied image cannot fit on the placement page.")

    occupancy = _occupancy_mask(page)
    if occupancy_image is not None:
        occupancy |= _occupancy_mask(occupancy_image)
    integral = cv2.integral(occupancy)
    protected = tuple(protected_regions)
    best: tuple[tuple[int, int, int, int, int, int], Rect] | None = None
    for y in y_candidates:
        for x in x_candidates:
            candidate = Rect(x, y, image.width, image.height)
            if any(_rectangles_overlap(candidate, region) for region in protected):
                continue
            occupied = int(
                integral[candidate.y2, candidate.x2]
                - integral[candidate.y, candidate.x2]
                - integral[candidate.y2, candidate.x]
                + integral[candidate.y, candidate.x]
            )
            dx = abs(candidate.x - preferred_x)
            dy = abs(candidate.y - preferred_y)
            score = (occupied, dx + dy, dy, dx, candidate.y, candidate.x)
            if best is None or score < best[0]:
                best = (score, candidate)

    if best is None:
        raise RuntimeError("No nearby placement avoids every protected region.")
    bounds = best[1]
    page.alpha_composite(image.convert("RGBA"), (bounds.x, bounds.y))
    return bounds


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


def _recipe_rect(value: list[int]) -> Rect:
    return Rect(value[0], value[1], value[2], value[3])


def _require_rect_on_page(rect: Rect, page_size: tuple[int, int], label: str) -> None:
    if rect.x2 > page_size[0] or rect.y2 > page_size[1]:
        raise ValueError(f"{label} extends outside expected page size {page_size}: {rect}")


def _rectangles_overlap(first: Rect, second: Rect) -> bool:
    return not (
        first.x2 <= second.x
        or second.x2 <= first.x
        or first.y2 <= second.y
        or second.y2 <= first.y
    )


def validate_form_recipe(recipe: Mapping[str, Any]) -> dict[str, Any]:
    """Return an isolated, validated generic form recipe.

    JSON Schema owns the closed structural contract. This function adds the
    geometry and cross-field invariants that are awkward or misleading to
    encode declaratively. Callers may safely serialize or edit the returned
    value without mutating their input mapping.
    """

    normalized = deepcopy(dict(recipe))
    errors = sorted(
        _FORM_RECIPE_VALIDATOR.iter_errors(normalized),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if errors:
        error = errors[0]
        location = ".".join(str(part) for part in error.absolute_path) or "recipe"
        raise ValueError(f"Invalid form recipe at {location}: {error.message}")

    expected_size = tuple(normalized["render"]["expected_size"])
    for name, field in normalized["fields"].items():
        if field["max_size"] < field["min_size"]:
            raise ValueError(f"Field {name} max_size must be at least min_size.")
        _require_rect_on_page(
            _recipe_rect(field["rect"]),
            expected_size,
            f"Field {name} rect",
        )

    for name, circle in normalized.get("circles", {}).items():
        rect = _recipe_rect(circle["rect"])
        stroke_width = circle.get("stroke_width", 4)
        if stroke_width * 2 >= min(rect.w, rect.h):
            raise ValueError(f"Circle {name} stroke must leave a visible interior.")
        _require_rect_on_page(rect, expected_size, f"Circle {name} rect")

    return normalized


def _draw_validated_form_recipe(
    source_image: Image.Image,
    recipe: Mapping[str, Any],
) -> Image.Image:
    expected_size = tuple(recipe["render"]["expected_size"])
    if source_image.size != expected_size:
        raise RuntimeError(
            f"Unexpected template render size {source_image.size}; expected {expected_size}."
        )

    form_overlay = Image.new("RGBA", source_image.size, (255, 255, 255, 0))
    draw = ImageDraw.Draw(form_overlay)
    for field in recipe["fields"].values():
        draw_text(
            draw,
            field["value"],
            _recipe_rect(field["rect"]),
            align=field["align"],
            max_size=field["max_size"],
            min_size=field["min_size"],
            bold=field.get("bold", False),
        )

    for circle in recipe.get("circles", {}).values():
        draw_circle(
            draw,
            _recipe_rect(circle["rect"]),
            stroke_width=circle.get("stroke_width", 4),
        )

    return form_overlay


def draw_form_recipe(
    source_image: Image.Image,
    recipe: Mapping[str, Any],
) -> Image.Image:
    """Draw one self-contained form recipe onto a transparent overlay."""

    normalized = validate_form_recipe(recipe)
    return _draw_validated_form_recipe(source_image, normalized)


def render_form_recipe(
    source_pdf: Path,
    recipe: Mapping[str, Any],
    *,
    source_render_path: Path,
    overlay_path: Path,
) -> dict[str, Any]:
    """Validate a recipe and write its source render and transparent overlay.

    The caller owns both output paths and their cleanup. This helper never
    modifies the source PDF and does not merge or deliver a final document;
    callers can retry their own output fallback around ``merge_overlay_pdf``.
    """

    normalized = validate_form_recipe(recipe)
    render = normalized["render"]
    source_image = render_pdf_page(
        source_pdf,
        render["page_index"],
        render["scale"],
        source_render_path,
    )
    form_overlay = _draw_validated_form_recipe(
        source_image,
        normalized,
    )
    form_overlay.save(overlay_path)
    return normalized
