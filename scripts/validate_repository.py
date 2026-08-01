"""Run the repository's local and CI validation pipeline.

The caller installs dependencies and supplies an evidence file plus a build
directory that does not yet exist. The runner creates that directory so every
artifact passed to Twine belongs to this invocation. Complete combined output
from each subprocess is written to the evidence file; stdout is reserved for
``OK`` or a compact failure object.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
INTERNAL_ERROR = 2


class ArgumentError(Exception):
    """Raised when command-line arguments do not satisfy the runner contract."""


class CompactArgumentParser(argparse.ArgumentParser):
    """Raise parse errors so the entrypoint can preserve its JSON output contract."""

    def error(self, message: str) -> None:
        raise ArgumentError(message)


def _emit_failure(stage: str, exit_code: int, evidence_path: Path | str) -> None:
    print(
        json.dumps(
            {
                "stage": stage,
                "exit_code": exit_code,
                "evidence_path": str(evidence_path),
            },
            separators=(",", ":"),
        )
    )


def _run_stage(stage: str, command: list[str], evidence: TextIO) -> int:
    evidence.write(f"== {stage} ==\n")
    evidence.flush()
    try:
        result = subprocess.run(
            command,
            cwd=REPOSITORY_ROOT,
            stdout=evidence,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
    except OSError as error:
        evidence.write(f"{type(error).__name__}: {error}\n")
        evidence.flush()
        return INTERNAL_ERROR
    evidence.flush()
    return result.returncode


def run_validation(build_dir: Path, evidence_path: Path) -> int:
    """Run every validation stage and return its process exit code."""

    build_dir = build_dir.expanduser().resolve()
    evidence_path = evidence_path.expanduser().resolve()

    try:
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence = evidence_path.open("w", encoding="utf-8", newline="\n")
    except OSError:
        _emit_failure("evidence", INTERNAL_ERROR, evidence_path)
        return INTERNAL_ERROR

    with evidence:
        try:
            build_dir.mkdir(parents=True, exist_ok=False)
        except OSError as error:
            evidence.write(f"== prepare ==\n{type(error).__name__}: {error}\n")
            _emit_failure("prepare", INTERNAL_ERROR, evidence_path)
            return INTERNAL_ERROR

        commands = (
            ("ruff", [sys.executable, "-m", "ruff", "check", "."]),
            (
                "pytest",
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-W",
                    "error::DeprecationWarning",
                ],
            ),
            (
                "build",
                [
                    sys.executable,
                    "-m",
                    "build",
                    "--outdir",
                    str(build_dir),
                ],
            ),
        )

        for stage, command in commands:
            exit_code = _run_stage(stage, command, evidence)
            if exit_code:
                _emit_failure(stage, exit_code, evidence_path)
                return exit_code

        artifacts = sorted(path for path in build_dir.iterdir() if path.is_file())
        if not artifacts:
            evidence.write("No package artifacts were created.\n")
            _emit_failure("build", INTERNAL_ERROR, evidence_path)
            return INTERNAL_ERROR

        twine_command = [
            sys.executable,
            "-m",
            "twine",
            "check",
            *(str(path) for path in artifacts),
        ]
        exit_code = _run_stage("twine", twine_command, evidence)
        if exit_code:
            _emit_failure("twine", exit_code, evidence_path)
            return exit_code

    print("OK")
    return 0


def _argument_value(arguments: Sequence[str], option: str) -> str:
    try:
        return arguments[arguments.index(option) + 1]
    except (ValueError, IndexError):
        return ""


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = CompactArgumentParser(add_help=False)
    parser.add_argument("--build-dir", required=True, type=Path)
    parser.add_argument("--evidence-file", required=True, type=Path)
    try:
        options = parser.parse_args(arguments)
    except ArgumentError:
        _emit_failure(
            "arguments",
            INTERNAL_ERROR,
            _argument_value(arguments, "--evidence-file"),
        )
        return INTERNAL_ERROR
    return run_validation(options.build_dir, options.evidence_file)


if __name__ == "__main__":
    raise SystemExit(main())
