"""Publish the current project version by creating its remote release tag.

The version and package name come only from ``pyproject.toml``. Before any
mutation, the helper requires a clean checkout, rejects a conflicting remote
tag, and refuses a version that already exists on PyPI. Pushing the exact HEAD
to the version tag is atomic and safe to retry: an existing tag succeeds only
when it already targets that commit. Output is limited to ``OK`` or compact
JSON so the deployment runner can consume it without subprocess logs.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
REMOTE = "origin"
INTERNAL_ERROR = 2
VERSION_PATTERN = re.compile(
    r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
)


class PublishError(Exception):
    """Carry one compact failed-stage result to the entrypoint."""

    def __init__(self, stage: str, exit_code: int) -> None:
        super().__init__(stage)
        self.stage = stage
        self.exit_code = exit_code


class CompactArgumentParser(argparse.ArgumentParser):
    """Raise parse errors so stdout remains machine-readable."""

    def error(self, message: str) -> None:
        raise PublishError("arguments", INTERNAL_ERROR)


def _emit_failure(stage: str, exit_code: int, tag: str | None = None) -> None:
    payload: dict[str, str | int] = {"stage": stage, "exit_code": exit_code}
    if tag is not None:
        payload["tag"] = tag
    print(json.dumps(payload, separators=(",", ":")))


def _git(stage: str, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as error:
        raise PublishError(stage, INTERNAL_ERROR) from error
    if result.returncode:
        raise PublishError(stage, result.returncode)
    return result.stdout.strip()


def _project_identity() -> tuple[str, str]:
    try:
        with (REPOSITORY_ROOT / "pyproject.toml").open("rb") as project_file:
            project = tomllib.load(project_file)["project"]
        name = project["name"]
        version = project["version"]
    except (KeyError, OSError, TypeError, tomllib.TOMLDecodeError) as error:
        raise PublishError("metadata", INTERNAL_ERROR) from error
    if not isinstance(name, str) or not isinstance(version, str):
        raise PublishError("metadata", INTERNAL_ERROR)
    if VERSION_PATTERN.fullmatch(version) is None:
        raise PublishError("version", INTERNAL_ERROR)
    return name, version


def _remote_tag_target(tag: str) -> str | None:
    reference = f"refs/tags/{tag}"
    output = _git("remote_tag", "ls-remote", "--tags", "--refs", REMOTE, reference)
    if not output:
        return None
    lines = output.splitlines()
    fields = lines[0].split() if len(lines) == 1 else []
    if len(fields) != 2 or fields[1] != reference:
        raise PublishError("remote_tag", INTERNAL_ERROR)
    return fields[0]


def _version_is_published(name: str, version: str) -> bool:
    package = urllib.parse.quote(name, safe="")
    release = urllib.parse.quote(version, safe="")
    request = urllib.request.Request(
        f"https://pypi.org/pypi/{package}/{release}/json",
        headers={"User-Agent": "pdf-form-tools-release-check/1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            if response.status != 200:
                raise PublishError("pypi", INTERNAL_ERROR)
            return True
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return False
        raise PublishError("pypi", error.code) from error
    except urllib.error.URLError as error:
        raise PublishError("pypi", INTERNAL_ERROR) from error


def publish_current_version(*, check_only: bool) -> int:
    tag: str | None = None
    try:
        if _git("git_status", "status", "--porcelain"):
            raise PublishError("working_tree", 1)
        name, version = _project_identity()
        tag = f"v{version}"
        head = _git("head", "rev-parse", "HEAD")
        remote_target = _remote_tag_target(tag)
        if remote_target is not None:
            if remote_target != head:
                raise PublishError("tag_conflict", 1)
            print("OK")
            return 0
        if _version_is_published(name, version):
            raise PublishError("pypi_version", 1)
        if not check_only:
            _git("push", "push", REMOTE, f"{head}:refs/tags/{tag}")
    except PublishError as error:
        _emit_failure(error.stage, error.exit_code, tag)
        return error.exit_code
    print("OK")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = CompactArgumentParser(add_help=False)
    parser.add_argument("--check-only", action="store_true")
    try:
        options = parser.parse_args(sys.argv[1:] if argv is None else argv)
    except PublishError as error:
        _emit_failure(error.stage, error.exit_code)
        return error.exit_code
    return publish_current_version(check_only=options.check_only)


if __name__ == "__main__":
    raise SystemExit(main())
