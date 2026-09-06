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
`pyproject.toml` before promotion. `sdlc/sdlc.yml` owns release preflight,
publication orchestration, artifact identity, and local deployment. Ship runs
preflight before its first remote mutation and rejects an existing PyPI version
or a conflicting remote tag. After merge and synchronization, the release helper's
`--trigger-release` mode pushes the exact synchronized commit to its `v<version>`
tag and waits for `.github/workflows/publish-pypi.yml`. That workflow publishes
with PyPI trusted publishing, then creates the public GitHub Release from the
matching changelog entry using `--github-actions-release`. Local orchestration
verifies both published results before ship runs the local-only `deploy`
operation. The workflow filename remains bound to the existing PyPI publisher.

## Pull requests

- Keep changes scoped and explain the user-visible impact.
- Add or update tests when behavior changes.
- Keep reusable placement and recipe rendering in the package; keep template data and profile bindings in caller-owned declarative configuration.
