from pathlib import Path

import numpy as np
import pdf_form_tools.pdf_form_overlay as overlay
from PIL import Image, ImageDraw
from pypdf import PdfReader
from reportlab.pdfgen import canvas

from pdf_form_tools import Rect, centered_address_box, detect_id_slots


def test_rect_inset() -> None:
    assert Rect(10, 20, 30, 40).inset(5) == Rect(15, 25, 20, 30)


def test_rect_band() -> None:
    rect = Rect(100, 200, 400, 120)
    assert rect.band(top_pad=10, height=50, left_pad=20) == Rect(120, 210, 360, 50)


def test_rect_above() -> None:
    rect = Rect(100, 200, 400, 20)
    assert rect.above(height=50, gap=10) == Rect(100, 140, 400, 50)


def test_centered_address_box() -> None:
    rect = Rect(100, 200, 400, 120)
    assert centered_address_box(rect, top_pad=10, side_pad=20, height=50) == Rect(120, 210, 360, 50)


def test_load_font_uses_existing_system_font() -> None:
    font = overlay.load_font(18, bold=False)
    assert font is not None
    assert Path(overlay.resolve_font_path()).exists()


def test_contains_hebrew_detects_hebrew_characters() -> None:
    assert overlay.contains_hebrew("\u05d0\u05de\u05d9\u05dc\u05d9")
    assert not overlay.contains_hebrew("Emily")


def test_writable_box_detects_blank_band_inside_field() -> None:
    page_gray = np.full((180, 420), 255, dtype=np.uint8)
    rect = Rect(40, 40, 320, 80)
    inner = rect.inset(8)
    page_gray[inner.y : inner.y + 12, inner.x:inner.x2] = 0
    page_gray[inner.y2 - 12 : inner.y2, inner.x:inner.x2] = 0
    page_gray[inner.y + 20 : inner.y2 - 20, inner.x : inner.x + 50] = 0
    page_gray[inner.y + 20 : inner.y2 - 20, inner.x2 - 35 : inner.x2] = 0

    box = overlay.writable_box(page_gray, rect)

    assert inner.x + 45 <= box.x <= inner.x + 60
    assert inner.x2 - 40 <= box.x2 <= inner.x2 - 20
    assert inner.y + 20 <= box.y <= inner.y + 30
    assert inner.y2 - 20 <= box.y2 <= inner.y2 - 10


def test_draw_check_creates_bold_upward_mark() -> None:
    image = Image.new("RGBA", (120, 120), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    rect = Rect(20, 20, 80, 80)

    overlay.draw_check(draw, rect, raise_px=8)

    alpha = np.array(image.getchannel("A"))
    bbox = image.getchannel("A").getbbox()
    assert bbox is not None
    assert bbox[1] <= rect.y + int(rect.h * 0.3)
    assert np.count_nonzero(alpha) > 900


def test_paste_signature_respects_minimum_a4_width_without_height_limit() -> None:
    page = Image.new("RGBA", (2100, 2970), (0, 0, 0, 0))
    signature = Image.new("RGBA", (20, 20), (0, 0, 0, 255))

    overlay.paste_signature(
        page,
        signature,
        Rect(1000, 500, 300, 10),
        min_cm_width=2.0,
        y_offset=10,
    )

    assert page.getchannel("A").getbbox() == (1050, 310, 1250, 510)


def test_paste_signature_does_not_exceed_narrow_line_width() -> None:
    page = Image.new("RGBA", (2100, 2970), (0, 0, 0, 0))
    signature = Image.new("RGBA", (20, 20), (0, 0, 0, 255))

    overlay.paste_signature(
        page,
        signature,
        Rect(1000, 500, 100, 10),
        min_cm_width=2.0,
        y_offset=10,
    )

    assert page.getchannel("A").getbbox() == (1000, 410, 1100, 510)


def test_paste_signature_preserves_aspect_ratio_when_height_limited() -> None:
    page = Image.new("RGBA", (2100, 2970), (0, 0, 0, 0))
    signature = Image.new("RGBA", (40, 20), (0, 0, 0, 255))

    overlay.paste_signature(
        page,
        signature,
        Rect(1000, 500, 300, 10),
        min_cm_width=2.0,
        target_height=50,
        y_offset=10,
    )

    assert page.getchannel("A").getbbox() == (1100, 460, 1200, 510)


def test_detect_id_slots_follows_printed_guides() -> None:
    page_gray = np.full((120, 920), 255, dtype=np.uint8)
    rect = Rect(10, 10, 900, 90)
    lower_start = rect.y + int(rect.h * 0.7)

    for offset in range(100, 900, 100):
        page_gray[lower_start : rect.y2, rect.x + offset - 1 : rect.x + offset + 2] = 0

    slots = detect_id_slots(page_gray, rect)

    assert len(slots) == 9
    assert slots[0] == Rect(13, 42, 94, 43)
    assert slots[-1] == Rect(813, 42, 93, 43)


def test_detect_square_boxes_finds_sorted_boxes() -> None:
    page_gray = np.full((220, 520), 255, dtype=np.uint8)
    region = Rect(20, 20, 420, 160)
    for offset in (40, 140, 240):
        page_gray[70:115, 20 + offset : 20 + offset + 45] = 0

    boxes = overlay.detect_square_boxes(page_gray, region)

    assert boxes == [
        Rect(60, 70, 45, 45),
        Rect(160, 70, 45, 45),
        Rect(260, 70, 45, 45),
    ]


def test_detect_lines_finds_sorted_lines() -> None:
    page_gray = np.full((240, 1100), 255, dtype=np.uint8)
    region = Rect(50, 50, 950, 120)
    lines = [
        Rect(90, 70, 600, 6),
        Rect(320, 95, 620, 6),
        Rect(430, 120, 520, 6),
    ]
    for line in lines:
        page_gray[line.y : line.y2, line.x : line.x2] = 0

    detected = overlay.detect_lines(page_gray, region)

    assert detected == lines


def test_draw_id_number_places_digits_in_detected_slots() -> None:
    page_gray = np.full((120, 920), 255, dtype=np.uint8)
    rect = Rect(10, 10, 900, 90)
    lower_start = rect.y + int(rect.h * 0.7)
    for offset in range(100, 900, 100):
        page_gray[lower_start : rect.y2, rect.x + offset - 1 : rect.x + offset + 2] = 0
    image = Image.new("RGBA", (920, 120), (0, 0, 0, 0))

    overlay.draw_id_number(ImageDraw.Draw(image), page_gray, rect, "123456789")

    assert image.getchannel("A").getbbox() is not None


def test_merge_overlay_pdf_preserves_pages_and_renders(tmp_path: Path) -> None:
    source_pdf = tmp_path / "source.pdf"
    overlay_png = tmp_path / "overlay.png"
    output_pdf = tmp_path / "output.pdf"
    rendered_png = tmp_path / "rendered.png"

    pdf = canvas.Canvas(str(source_pdf), pagesize=(200, 200))
    pdf.drawString(20, 100, "page one")
    pdf.showPage()
    pdf.drawString(20, 100, "page two")
    pdf.save()

    image = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    ImageDraw.Draw(image).rectangle((40, 40, 120, 120), fill=(255, 0, 0, 255))
    image.save(overlay_png)

    overlay.merge_overlay_pdf(source_pdf, overlay_png, output_pdf)
    rendered = overlay.render_pdf_page(output_pdf, 0, 1, rendered_png)

    assert len(PdfReader(str(output_pdf)).pages) == 2
    assert rendered.size == (200, 200)
    assert rendered_png.exists()
