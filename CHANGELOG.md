# Changelog

## Unreleased

- Size signatures by physical dimensions, apply an optional physical downward offset, align them horizontally, and return their placed bounds for collision checks.

## 2.2.0

- Export `detect_id_slots()` from the package's public API and document the overlay helpers.
- Resolve Windows fonts through `WINDIR` instead of a hard-coded system path.
- Use one isolated repository-validation command locally and across the CI matrix.
- Add retry-safe release preflight and tag publication for the lifecycle ship flow.

## 2.1.0

- Add reusable `Rect.band()` and `Rect.above()` helpers for shared form geometry.
- Keep `centered_address_box()` as a compatibility wrapper over the shared band helper.
- Add regression tests for writable-band detection and line and checkbox-region detection.

## 2.0.2

- Preserve signature image aspect ratios when scaling to form signature lines.

## 2.0.1

- Add Python 3.14 support metadata and CI coverage.
- Add regression tests for PDF merge/render, signature sizing, checkbox drawing, ID slot detection, and RTL text handling.
- Avoid a pypdf deprecation warning when merging overlays.
- Document GitHub private vulnerability reporting for sensitive security reports.

## 2.0.0

- Publish the import-only `pdf-form-tools` package to PyPI.
- Keep form-specific filling flows out of the reusable package.
