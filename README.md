# pdf-form-tools

`pdf-form-tools` is an import-only Python package for filling layout-sensitive scanned PDF forms with deterministic placement helpers and visual verification primitives.

It is intentionally small:

- render PDF pages to raster images
- detect writable regions, checkbox boxes, signature lines, and ID slots
- draw text, checks, and signatures onto an overlay
- merge the overlay back into the original PDF

## Install

```bash
python -m pip install pdf-form-tools
```

Supported Python versions: 3.11 through 3.14.

## Example

```python
from pathlib import Path

from pdf_form_tools import Rect, merge_overlay_pdf, render_pdf_page

source_pdf = Path("form.pdf")
preview_png = Path("preview-page1.png")
render_pdf_page(source_pdf, 0, 2, preview_png)

# draw your overlay separately, then merge it back
merge_overlay_pdf(source_pdf, Path("overlay-page1.png"), Path("form-filled.pdf"))
```

## Development

```bash
python -m pip install -e ".[dev]"
python scripts/validate_repository.py --build-dir "$BUILD_DIR" --evidence-file "$EVIDENCE_FILE"
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for validation path requirements and
the release flow.

## Scope

This package contains reusable low-level helpers only. Form-specific filling flows belong in project-local scripts or thin runners, not in the shared library.
