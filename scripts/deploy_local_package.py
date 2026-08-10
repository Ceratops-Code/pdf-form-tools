"""Build and install this package into the running Python interpreter.

The helper snapshots current tracked files into temporary storage, builds one
wheel there, force-installs that exact wheel with ``sys.executable``, verifies
the installed version, and removes all temporary paths on success or failure.
"""

from __future__ import annotations

import importlib.metadata
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[1]
INTERNAL_ERROR = 2


@dataclass(frozen=True)
class DeployFailure(Exception):
    """Carry one compact deployment-stage failure to the CLI boundary."""

    stage: str
    exit_code: int
    detail: str


def _failure_tail(result: subprocess.CompletedProcess[str]) -> str:
    """Return bounded subprocess context suitable for the operation runner."""

    lines = (result.stderr or result.stdout or "").splitlines()
    return "\n".join(lines[-8:]) or "command failed without output"


def _run_stage(
    stage: str,
    command: list[str],
    *,
    cwd: pathlib.Path,
) -> subprocess.CompletedProcess[str]:
    """Run one argv-only deployment stage."""

    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as error:
        raise DeployFailure(
            stage,
            INTERNAL_ERROR,
            f"{type(error).__name__}: {error}",
        ) from error
    if result.returncode:
        raise DeployFailure(stage, result.returncode, _failure_tail(result))
    return result


def _resolved_repository() -> pathlib.Path:
    """Resolve the configured repository root or fail compactly."""

    try:
        resolved = REPOSITORY_ROOT.expanduser().resolve(strict=True)
    except OSError as error:
        raise DeployFailure(
            "source", INTERNAL_ERROR, f"repository is unavailable: {error}"
        ) from error
    if not resolved.is_dir():
        raise DeployFailure(
            "source", INTERNAL_ERROR, "repository root is not a directory"
        )
    return resolved


def _copy_tracked_source(
    repository_root: pathlib.Path,
    destination: pathlib.Path,
) -> None:
    """Copy current tracked file contents into an isolated build tree."""

    listing = _run_stage(
        "source",
        ["git", "ls-files", "-z"],
        cwd=repository_root,
    )
    try:
        destination.mkdir(parents=True, exist_ok=False)
    except OSError as error:
        raise DeployFailure(
            "source", INTERNAL_ERROR, f"could not create source snapshot: {error}"
        ) from error
    for value in listing.stdout.split("\0"):
        if not value:
            continue
        relative = pathlib.PurePosixPath(value)
        if relative.is_absolute() or ".." in relative.parts:
            raise DeployFailure("source", INTERNAL_ERROR, "unsafe tracked path")
        source = repository_root.joinpath(*relative.parts)
        try:
            resolved_source = source.resolve(strict=True)
            resolved_source.relative_to(repository_root)
        except (OSError, RuntimeError, ValueError) as error:
            raise DeployFailure(
                "source",
                INTERNAL_ERROR,
                f"unreadable tracked path: {relative.as_posix()}",
            ) from error
        if not resolved_source.is_file():
            raise DeployFailure(
                "source",
                INTERNAL_ERROR,
                f"tracked path is not a file: {relative.as_posix()}",
            )
        target = destination.joinpath(*relative.parts)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(resolved_source, target)
        except OSError as error:
            raise DeployFailure(
                "source",
                INTERNAL_ERROR,
                f"could not copy tracked path: {relative.as_posix()}",
            ) from error


def _project_identity(repository_root: pathlib.Path) -> tuple[str, str]:
    """Read the static distribution name and version from ``pyproject.toml``."""

    try:
        with (repository_root / "pyproject.toml").open("rb") as stream:
            value = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise DeployFailure(
            "metadata", INTERNAL_ERROR, f"could not read pyproject.toml: {error}"
        ) from error
    project = value.get("project")
    if not isinstance(project, Mapping):
        raise DeployFailure(
            "metadata", INTERNAL_ERROR, "pyproject.toml has no project table"
        )
    name = project.get("name")
    version = project.get("version")
    if not isinstance(name, str) or not name or not isinstance(version, str) or not version:
        raise DeployFailure(
            "metadata", INTERNAL_ERROR, "project name and version must be static text"
        )
    return name, version


def deploy_local_package() -> None:
    """Build, install, and verify one wheel in the running Python interpreter."""

    repository_root = _resolved_repository()
    name, expected_version = _project_identity(repository_root)
    try:
        with tempfile.TemporaryDirectory(prefix="pdf-form-tools-deploy-") as temporary:
            temporary_root = pathlib.Path(temporary)
            source_dir = temporary_root / "source"
            output_dir = temporary_root / "wheel"
            _copy_tracked_source(repository_root, source_dir)
            output_dir.mkdir()
            _run_stage(
                "build",
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "--disable-pip-version-check",
                    "wheel",
                    "--no-deps",
                    "--wheel-dir",
                    str(output_dir),
                    ".",
                ],
                cwd=source_dir,
            )
            wheels = sorted(output_dir.glob("*.whl"))
            if len(wheels) != 1:
                raise DeployFailure(
                    "artifact",
                    INTERNAL_ERROR,
                    f"expected one wheel, found {len(wheels)}",
                )
            _run_stage(
                "install",
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "--disable-pip-version-check",
                    "install",
                    "--force-reinstall",
                    "--no-deps",
                    str(wheels[0]),
                ],
                cwd=repository_root,
            )
            try:
                installed_version = importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError as error:
                raise DeployFailure(
                    "verify", INTERNAL_ERROR, f"installed distribution not found: {name}"
                ) from error
            if installed_version != expected_version:
                raise DeployFailure(
                    "verify",
                    INTERNAL_ERROR,
                    f"expected {expected_version}, found {installed_version}",
                )
    except DeployFailure:
        raise
    except OSError as error:
        raise DeployFailure(
            "temporary", INTERNAL_ERROR, f"temporary cleanup failed: {error}"
        ) from error


def _emit_failure(error: DeployFailure) -> None:
    print(
        json.dumps(
            {
                "stage": error.stage,
                "exit_code": error.exit_code,
                "detail": error.detail,
            },
            separators=(",", ":"),
        )
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Deploy locally and emit only ``OK`` or one compact failure object."""

    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments:
        error = DeployFailure("arguments", INTERNAL_ERROR, "no arguments are accepted")
        _emit_failure(error)
        return error.exit_code
    try:
        deploy_local_package()
    except DeployFailure as error:
        _emit_failure(error)
        return error.exit_code if error.exit_code > 0 else 1
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
