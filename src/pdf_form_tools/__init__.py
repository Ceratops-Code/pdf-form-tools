"""Public geometry, detection, drawing, and PDF-overlay helpers."""

from .pdf_form_overlay import (
    FORM_RECIPE_JSON_SCHEMA,
    FORM_RECIPE_SCHEMA,
    Rect,
    detect_guided_slots,
    detect_lines,
    detect_square_boxes,
    draw_check,
    draw_circle,
    draw_form_recipe,
    draw_text,
    merge_overlay_pdf,
    place_image_near_rect,
    render_form_recipe,
    render_pdf_page,
    validate_form_recipe,
    writable_box,
)

__all__ = [
    "FORM_RECIPE_JSON_SCHEMA",
    "FORM_RECIPE_SCHEMA",
    "Rect",
    "detect_guided_slots",
    "detect_lines",
    "detect_square_boxes",
    "draw_check",
    "draw_circle",
    "draw_form_recipe",
    "draw_text",
    "merge_overlay_pdf",
    "place_image_near_rect",
    "render_form_recipe",
    "render_pdf_page",
    "validate_form_recipe",
    "writable_box",
]
