"""Publish and verify the current project release end to end.

The version, package name, repository, and release notes come from tracked
project metadata. Preflight is read-only. Publication pushes the exact clean
HEAD to its version tag, waits for the tag-triggered trusted-publishing workflow,
verifies PyPI, creates the matching public GitHub release, and verifies the
result. Every completed boundary is safe to retry; stdout remains ``OK`` or one
compact failure object for the repository lifecycle operation runner.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
REMOTE = "origin"
WORKFLOW = "publish-pypi.yml"
WAIT_SECONDS = 900
POLL_SECONDS = 5
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


def _command(stage: str, executable: str, *arguments: str) -> str:
    """Run one exact argv command and convert failures to a compact stage."""

    try:
        result = subprocess.run(
            [executable, *arguments],
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


def _git(stage: str, *arguments: str) -> str:
    return _command(stage, "git", *arguments)


def _gh(stage: str, *arguments: str) -> str:
    return _command(stage, "gh", *arguments)


def _project_identity() -> tuple[str, str, str]:
    """Read the package identity and canonical GitHub repository slug."""

    try:
        with (REPOSITORY_ROOT / "pyproject.toml").open("rb") as project_file:
            project = tomllib.load(project_file)["project"]
        name = project["name"]
        version = project["version"]
        repository_url = project["urls"]["Repository"]
    except (KeyError, OSError, TypeError, tomllib.TOMLDecodeError) as error:
        raise PublishError("metadata", INTERNAL_ERROR) from error
    if not all(isinstance(value, str) for value in (name, version, repository_url)):
        raise PublishError("metadata", INTERNAL_ERROR)
    if VERSION_PATTERN.fullmatch(version) is None:
        raise PublishError("version", INTERNAL_ERROR)
    parsed = urllib.parse.urlparse(repository_url)
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if parsed.scheme != "https" or parsed.netloc.lower() != "github.com" or len(parts) != 2:
        raise PublishError("repository", INTERNAL_ERROR)
    owner, repository = parts
    repository = repository.removesuffix(".git")
    if not owner or not repository:
        raise PublishError("repository", INTERNAL_ERROR)
    return name, version, f"{owner}/{repository}"


def _changelog_notes(version: str) -> str:
    """Return exactly one nonempty version section from the tracked changelog."""

    try:
        lines = (REPOSITORY_ROOT / "CHANGELOG.md").read_text(
            encoding="utf-8"
        ).splitlines()
    except (OSError, UnicodeError) as error:
        raise PublishError("changelog", INTERNAL_ERROR) from error
    heading = f"## {version}"
    try:
        start = lines.index(heading) + 1
    except ValueError as error:
        raise PublishError("changelog", 1) from error
    end = next(
        (index for index in range(start, len(lines)) if lines[index].startswith("## ")),
        len(lines),
    )
    notes = "\n".join(lines[start:end]).strip()
    if not notes:
        raise PublishError("changelog", 1)
    return notes


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


def _json_request(url: str, stage: str) -> Mapping[str, Any] | None:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "pdf-form-tools-release/1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            if response.status != 200:
                raise PublishError(stage, INTERNAL_ERROR)
            value = json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise PublishError(stage, error.code) from error
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PublishError(stage, INTERNAL_ERROR) from error
    if not isinstance(value, Mapping):
        raise PublishError(stage, INTERNAL_ERROR)
    return value


def _version_is_published(name: str, version: str) -> bool:
    package = urllib.parse.quote(name, safe="")
    release = urllib.parse.quote(version, safe="")
    return (
        _json_request(
            f"https://pypi.org/pypi/{package}/{release}/json",
            "pypi",
        )
        is not None
    )


def _github_release(repository: str, tag: str) -> Mapping[str, Any] | None:
    owner, name = repository.split("/", 1)
    encoded_tag = urllib.parse.quote(tag, safe="")
    return _json_request(
        f"https://api.github.com/repos/{owner}/{name}/releases/tags/{encoded_tag}",
        "github_release",
    )


def _public_release(value: Mapping[str, Any] | None, tag: str) -> bool:
    return bool(
        value is not None
        and value.get("tag_name") == tag
        and value.get("draft") is False
        and value.get("prerelease") is False
        and isinstance(value.get("html_url"), str)
    )


def _workflow_run(head: str, tag: str) -> Mapping[str, Any] | None:
    """Return the exact tag-push workflow run, if GitHub has registered it."""

    raw = _gh(
        "workflow_lookup",
        "run",
        "list",
        "--workflow",
        WORKFLOW,
        "--event",
        "push",
        "--branch",
        tag,
        "--commit",
        head,
        "--limit",
        "20",
        "--json",
        "databaseId,headSha,status,conclusion,url",
    )
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise PublishError("workflow_lookup", INTERNAL_ERROR) from error
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        raise PublishError("workflow_lookup", INTERNAL_ERROR)
    matches = [item for item in value if item.get("headSha") == head]
    if not matches:
        return None
    if len(matches) != 1:
        raise PublishError("workflow_lookup", INTERNAL_ERROR)
    return matches[0]


def _wait_for_workflow(head: str, tag: str) -> Mapping[str, Any]:
    """Wait for one exact workflow run and require successful completion."""

    deadline = time.monotonic() + WAIT_SECONDS
    while True:
        run = _workflow_run(head, tag)
        if run is not None and run.get("status") == "completed":
            if run.get("conclusion") != "success":
                raise PublishError("workflow", 1)
            return run
        if time.monotonic() >= deadline:
            raise PublishError("workflow_timeout", 1)
        time.sleep(POLL_SECONDS)


def _wait_for_pypi(name: str, version: str) -> None:
    """Allow bounded registry propagation after the publishing workflow."""

    deadline = time.monotonic() + WAIT_SECONDS
    while not _version_is_published(name, version):
        if time.monotonic() >= deadline:
            raise PublishError("pypi_timeout", 1)
        time.sleep(POLL_SECONDS)


def _ensure_github_release(
    repository: str,
    tag: str,
    notes: str,
) -> None:
    """Create the public release once and verify its externally visible state."""

    existing = _github_release(repository, tag)
    if existing is not None:
        if not _public_release(existing, tag):
            raise PublishError("github_release", 1)
        return
    _gh(
        "github_release_create",
        "release",
        "create",
        tag,
        "--repo",
        repository,
        "--verify-tag",
        "--title",
        tag,
        "--notes",
        notes,
    )
    if not _public_release(_github_release(repository, tag), tag):
        raise PublishError("github_release_verify", 1)


def publish_current_version(*, check_only: bool) -> int:
    """Preflight or complete the current release through verified boundaries."""

    tag: str | None = None
    try:
        if _git("git_status", "status", "--porcelain"):
            raise PublishError("working_tree", 1)
        name, version, repository = _project_identity()
        notes = _changelog_notes(version)
        _gh(
            "github_preflight",
            "workflow",
            "view",
            WORKFLOW,
            "--repo",
            repository,
        )
        tag = f"v{version}"
        head = _git("head", "rev-parse", "HEAD")
        remote_target = _remote_tag_target(tag)
        if remote_target is not None and remote_target != head:
            raise PublishError("tag_conflict", 1)

        if remote_target is None:
            if _version_is_published(name, version):
                raise PublishError("pypi_version", 1)
            if _github_release(repository, tag) is not None:
                raise PublishError("github_release_conflict", 1)
            if check_only:
                print("OK")
                return 0
            _git("push", "push", REMOTE, f"{head}:refs/tags/{tag}")
            _wait_for_workflow(head, tag)
            _wait_for_pypi(name, version)
        elif check_only:
            print("OK")
            return 0
        else:
            published = _version_is_published(name, version)
            existing_release = _github_release(repository, tag)
            if published and _public_release(existing_release, tag):
                print("OK")
                return 0
            if not published:
                _wait_for_workflow(head, tag)
                _wait_for_pypi(name, version)

        _ensure_github_release(repository, tag, notes)
        if not _version_is_published(name, version):
            raise PublishError("pypi_verify", 1)
        if not _public_release(_github_release(repository, tag), tag):
            raise PublishError("github_release_verify", 1)
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
