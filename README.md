# pdf-form-tools

`pdf-form-tools` is an import-only Python package for filling layout-sensitive scanned PDF forms with deterministic placement helpers, a validated JSON recipe renderer, and visual verification primitives.

It is intentionally small:

- render PDF pages to raster images
- detect writable regions, checkbox boxes, signature lines, and ID slots
- draw text, checks, and signatures onto an overlay
- validate and render self-contained JSON form recipes
- merge the overlay back into the original PDF

## Install

```bash
python -m pip install pdf-form-tools
```

Supported Python versions: 3.11 through 3.14.

## Example

```python
from pathlib import Path

from pdf_form_tools import merge_overlay_pdf, render_form_recipe

recipe = {
    "schema": "pdf-form-tools.form-recipe.v1",
    "template": {"id": "example", "version": 1},
    "render": {
        "page_index": 0,
        "scale": 2,
        "expected_size": [1191, 1684],
        "page_size_cm": [21.0, 29.7],
    },
    "fields": {
        "name": {
            "value": "Emily",
            "rect": [300, 500, 500, 60],
            "align": "center",
            "max_size": 44,
            "min_size": 28,
        }
    },
    "signatures": {},
}

source_pdf = Path("form.pdf")
overlay_png = Path("overlay-page1.png")
render_form_recipe(
    source_pdf,
    recipe,
    Path("signatures"),
    source_render_path=Path("preview-page1.png"),
    overlay_path=overlay_png,
)

merge_overlay_pdf(source_pdf, overlay_png, Path("form-filled.pdf"))
```

## Development

```bash
python -m pip install -e ".[dev]"
python scripts/validate_repository.py --build-dir "$BUILD_DIR" --evidence-file "$EVIDENCE_FILE"
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for validation path requirements and
the release flow.

## Scope

This package owns reusable placement and generic recipe rendering. Template geometry, resolved values, profile bindings, and workflow policy remain declarative data or project-local runner concerns.
