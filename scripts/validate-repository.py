#!/usr/bin/env python3
"""Run checks, retain first-failure evidence, and remove it after success."""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import tempfile

CHECK_DEFINITIONS = [{'id': 'pdf-form-tools-validator',
  'command': ['{python}',
              'scripts/validate_repository.py',
              '--build-dir',
              '{temp}/pdf-build',
              '--evidence-file',
              '{temp}/pdf-validation.log'],
  'cwd': '.',
  'exclusive': True,
  'python_packages': []}]
COMMAND_NOT_FOUND_EXIT_CODE = 127


def command(definition: dict[str, object], temporary_root: pathlib.Path) -> list[str]:
    """Resolve portable executable tokens without shell parsing."""

    npm = "npm.cmd" if sys.platform == "win32" else "npm"
    pnpm = "pnpm.cmd" if sys.platform == "win32" else "pnpm"
    pwsh = "pwsh.exe" if sys.platform == "win32" else "pwsh"
    values = {
        "{python}": sys.executable,
        "{npm}": npm,
        "{pnpm}": pnpm,
        "{pwsh}": pwsh,
        "{temp}": str(temporary_root),
    }
    raw_command = definition["command"]
    if not isinstance(raw_command, list):
        raise TypeError("check command must be a list")
    resolved: list[str] = []
    for raw in raw_command:
        value = str(raw)
        for token, replacement in values.items():
            value = value.replace(token, replacement)
        resolved.append(value)
    return resolved


def prepare_temporary_directories(
    argv: list[str], temporary_root: pathlib.Path
) -> None:
    """Create only catalog-declared working directories inside the owned root."""

    resolved_root = temporary_root.resolve()
    for index, value in enumerate(argv[:-1]):
        if value != "--temp-root":
            continue
        path = pathlib.Path(argv[index + 1]).resolve()
        try:
            path.relative_to(resolved_root)
        except ValueError:
            continue
        path.mkdir(parents=True, exist_ok=True)


def cleanup_evidence(
    evidence_file: pathlib.Path,
    *,
    prune_default_parent: bool,
) -> None:
    """Remove stale failure evidence after success and only its owned directory."""

    evidence_file.unlink(missing_ok=True)
    evidence_file.with_name(f".{evidence_file.name}.tmp").unlink(missing_ok=True)
    if prune_default_parent:
        try:
            evidence_file.parent.rmdir()
        except FileNotFoundError:
            pass
        except OSError:
            if not evidence_file.parent.is_dir() or any(
                evidence_file.parent.iterdir()
            ):
                return
            raise


def child_evidence(argv: list[str], temporary_root: pathlib.Path) -> list[str]:
    """Retain declared child-validator evidence before temporary cleanup."""

    paths: list[pathlib.Path] = []
    for index, value in enumerate(argv):
        if value == "--evidence-file" and index + 1 < len(argv):
            paths.append(pathlib.Path(argv[index + 1]))
        elif value.startswith("--evidence-file="):
            paths.append(pathlib.Path(value.partition("=")[2]))
    retained: list[str] = []
    temporary_root = temporary_root.resolve()
    for path in paths:
        resolved = path.resolve()
        try:
            resolved.relative_to(temporary_root)
        except ValueError:
            continue
        if resolved.is_symlink() or not resolved.is_file():
            continue
        retained.extend(
            (
                f"child_evidence: {resolved.relative_to(temporary_root).as_posix()}",
                resolved.read_text(encoding="utf-8", errors="replace"),
            )
        )
    return retained


def main() -> int:
    """Run catalog-selected checks in order and emit one bounded result."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-file", type=pathlib.Path)
    args = parser.parse_args()
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    evidence_file = (
        args.evidence_file.expanduser().resolve()
        if args.evidence_file
        else repo_root / "build" / "deploy-validation" / "repository-validation.log"
    )
    with tempfile.TemporaryDirectory(prefix="repository-validation-") as temporary:
        temporary_root = pathlib.Path(temporary)
        for definition in CHECK_DEFINITIONS:
            argv = command(definition, temporary_root)
            prepare_temporary_directories(argv, temporary_root)
            raw_cwd = definition["cwd"]
            if not isinstance(raw_cwd, str):
                raise TypeError("check cwd must be a string")
            cwd = repo_root.joinpath(*pathlib.PurePosixPath(raw_cwd).parts)
            try:
                result = subprocess.run(
                    argv,
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                )
            except OSError as exc:
                result = subprocess.CompletedProcess(
                    argv,
                    COMMAND_NOT_FOUND_EXIT_CODE,
                    "",
                    f"{type(exc).__name__}: {exc}",
                )
            if result.returncode == 0:
                continue
            evidence_file.parent.mkdir(parents=True, exist_ok=True)
            partial = evidence_file.with_name(f".{evidence_file.name}.tmp")
            retained_child_evidence = child_evidence(argv, temporary_root)
            partial.write_text(
                "\n".join(
                    (
                        f"check: {definition['id']}",
                        f"exit_code: {result.returncode}",
                        f"cwd: {cwd}",
                        "command: " + json.dumps(argv, separators=(",", ":")),
                        "stdout:",
                        result.stdout or "",
                        "stderr:",
                        result.stderr or "",
                        *retained_child_evidence,
                    )
                )
                + "\n",
                encoding="utf-8",
                newline="\n",
            )
            partial.replace(evidence_file)
            print(
                json.dumps(
                    {
                        "check": definition["id"],
                        "exit_code": result.returncode,
                        "evidence_file": str(evidence_file),
                    },
                    separators=(",", ":"),
                )
            )
            return result.returncode if result.returncode > 0 else 1
    try:
        cleanup_evidence(
            evidence_file,
            prune_default_parent=args.evidence_file is None,
        )
    except OSError as exc:
        print(
            json.dumps(
                {
                    "check": "evidence-cleanup",
                    "exit_code": 1,
                    "evidence_file": str(evidence_file),
                    "cleanup_error": f"{type(exc).__name__}: {exc}",
                },
                separators=(",", ":"),
            )
        )
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
