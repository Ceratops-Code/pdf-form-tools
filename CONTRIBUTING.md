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

## Releases

Document the release in `CHANGELOG.md` and set the next semantic version in
`pyproject.toml` before promotion. Ship runs the release preflight before its
first remote mutation and rejects an existing PyPI version or a conflicting
remote tag. After merge and synchronization, ship pushes the exact synchronized
commit to its `v<version>` tag, waits for the trusted-publishing workflow,
verifies PyPI, then creates and verifies the public GitHub release from the
matching changelog entry. Only after release publication succeeds does ship run
the local-only `deploy` operation.

## Pull requests

- Keep changes scoped and explain the user-visible impact.
- Add or update tests when behavior changes.
- Keep reusable placement and recipe rendering in the package; keep template data and profile bindings in caller-owned declarative configuration.
