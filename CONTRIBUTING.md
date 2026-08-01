# Contributing

## Development setup

```bash
python -m pip install -e ".[dev]"
```

## Checks

Run these before opening a pull request:

```bash
python scripts/validate_repository.py --build-dir "$BUILD_DIR" --evidence-file "$EVIDENCE_FILE"
```

Set `BUILD_DIR` to a temporary path that does not exist and `EVIDENCE_FILE` to
the file that should receive complete command output. The runner prints `OK` on
success or compact JSON identifying the failed stage and evidence file.

## Pull requests

- Keep changes scoped and explain the user-visible impact.
- Add or update tests when behavior changes.
- Prefer reusable low-level helpers in the package and keep form-specific flows in local runners.
