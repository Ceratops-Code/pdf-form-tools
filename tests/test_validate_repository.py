from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import scripts.publish_current_version as publisher
import scripts.validate_repository as validator


def test_commands_use_isolated_artifacts_and_preserve_paths_with_spaces(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    repository = tmp_path / "repository with spaces"
    stale_dist = repository / "dist"
    stale_dist.mkdir(parents=True)
    stale_artifact = stale_dist / "stale.whl"
    stale_artifact.write_text("stale", encoding="utf-8")
    monkeypatch.setattr(validator, "REPOSITORY_ROOT", repository)

    build_dir = tmp_path / "fresh build output"
    evidence_path = tmp_path / "validation evidence" / "full output.log"
    wheel = build_dir / "package-1.0-py3-none-any.whl"
    source = build_dir / "package-1.0.tar.gz"

    def successful_run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
        kwargs["stdout"].write(f"output from {command[2]}\n")
        if command[2] == "build":
            wheel.write_text("wheel", encoding="utf-8")
            source.write_text("source", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0)

    run = Mock(side_effect=successful_run)
    monkeypatch.setattr(validator.subprocess, "run", run)

    exit_code = validator.main(
        [
            "--build-dir",
            str(build_dir),
            "--evidence-file",
            str(evidence_path),
        ]
    )

    assert exit_code == 0
    assert capsys.readouterr().out == "OK\n"
    assert [call.args[0] for call in run.call_args_list] == [
        [sys.executable, "-m", "ruff", "check", "."],
        [
            sys.executable,
            "-m",
            "pytest",
            "-W",
            "error::DeprecationWarning",
        ],
        [sys.executable, "-m", "build", "--outdir", str(build_dir)],
        [
            sys.executable,
            "-m",
            "twine",
            "check",
            str(wheel),
            str(source),
        ],
    ]
    for call in run.call_args_list:
        assert call.kwargs["cwd"] == repository
        assert call.kwargs["stderr"] is subprocess.STDOUT
        assert call.kwargs["text"] is True
        assert call.kwargs["check"] is False
        assert Path(call.kwargs["stdout"].name) == evidence_path
    assert str(stale_artifact) not in run.call_args_list[-1].args[0]
    assert evidence_path.read_text(encoding="utf-8").count("output from") == 4


def test_failure_is_compact_json_and_stops_later_stages(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    build_dir = tmp_path / "build"
    evidence_path = tmp_path / "evidence.log"
    run = Mock(
        side_effect=[
            subprocess.CompletedProcess([], 0),
            subprocess.CompletedProcess([], 7),
        ]
    )
    monkeypatch.setattr(validator.subprocess, "run", run)

    exit_code = validator.main(
        [
            "--build-dir",
            str(build_dir),
            "--evidence-file",
            str(evidence_path),
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 7
    assert output == json.dumps(
        {
            "stage": "pytest",
            "exit_code": 7,
            "evidence_path": str(evidence_path),
        },
        separators=(",", ":"),
    ) + "\n"
    assert run.call_count == 2


def test_existing_build_directory_is_rejected_before_subprocesses(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    build_dir = tmp_path / "existing build"
    build_dir.mkdir()
    (build_dir / "stale.tar.gz").write_text("stale", encoding="utf-8")
    evidence_path = tmp_path / "evidence.log"
    run = Mock()
    monkeypatch.setattr(validator.subprocess, "run", run)

    exit_code = validator.main(
        [
            "--build-dir",
            str(build_dir),
            "--evidence-file",
            str(evidence_path),
        ]
    )

    assert exit_code == validator.INTERNAL_ERROR
    assert json.loads(capsys.readouterr().out) == {
        "stage": "prepare",
        "exit_code": validator.INTERNAL_ERROR,
        "evidence_path": str(evidence_path),
    }
    run.assert_not_called()
    assert "FileExistsError" in evidence_path.read_text(encoding="utf-8")


def test_successful_build_without_artifacts_fails_before_twine(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    build_dir = tmp_path / "build"
    evidence_path = tmp_path / "evidence.log"
    run = Mock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(validator.subprocess, "run", run)

    exit_code = validator.main(
        [
            "--build-dir",
            str(build_dir),
            "--evidence-file",
            str(evidence_path),
        ]
    )

    assert exit_code == validator.INTERNAL_ERROR
    assert json.loads(capsys.readouterr().out)["stage"] == "build"
    assert run.call_count == 3
    assert "No package artifacts were created." in evidence_path.read_text(
        encoding="utf-8"
    )


def _configure_release_project(tmp_path: Path, monkeypatch) -> Path:
    repository = tmp_path / "release repository with spaces"
    repository.mkdir()
    (repository / "pyproject.toml").write_text(
        '[project]\nname = "pdf-form-tools"\nversion = "2.2.0"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(publisher, "REPOSITORY_ROOT", repository)
    return repository


def test_release_preflight_constructs_checks_without_push(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    repository = _configure_release_project(tmp_path, monkeypatch)
    head = "a" * 40
    run = Mock(
        side_effect=[
            subprocess.CompletedProcess([], 0, stdout=""),
            subprocess.CompletedProcess([], 0, stdout=f"{head}\n"),
            subprocess.CompletedProcess([], 0, stdout=""),
        ]
    )
    registry_check = Mock(return_value=False)
    monkeypatch.setattr(publisher.subprocess, "run", run)
    monkeypatch.setattr(publisher, "_version_is_published", registry_check)

    exit_code = publisher.main(["--check-only"])

    assert exit_code == 0
    assert capsys.readouterr().out == "OK\n"
    assert [call.args[0] for call in run.call_args_list] == [
        ["git", "status", "--porcelain"],
        ["git", "rev-parse", "HEAD"],
        [
            "git",
            "ls-remote",
            "--tags",
            "--refs",
            "origin",
            "refs/tags/v2.2.0",
        ],
    ]
    assert all(call.kwargs["cwd"] == repository for call in run.call_args_list)
    registry_check.assert_called_once_with("pdf-form-tools", "2.2.0")


def test_release_pushes_exact_head_to_new_version_tag(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _configure_release_project(tmp_path, monkeypatch)
    head = "b" * 40
    run = Mock(
        side_effect=[
            subprocess.CompletedProcess([], 0, stdout=""),
            subprocess.CompletedProcess([], 0, stdout=f"{head}\n"),
            subprocess.CompletedProcess([], 0, stdout=""),
            subprocess.CompletedProcess([], 0, stdout=""),
        ]
    )
    monkeypatch.setattr(publisher.subprocess, "run", run)
    monkeypatch.setattr(publisher, "_version_is_published", Mock(return_value=False))

    exit_code = publisher.main([])

    assert exit_code == 0
    assert capsys.readouterr().out == "OK\n"
    assert run.call_args_list[-1].args[0] == [
        "git",
        "push",
        "origin",
        f"{head}:refs/tags/v2.2.0",
    ]


def test_release_retry_accepts_only_the_same_remote_commit(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _configure_release_project(tmp_path, monkeypatch)
    head = "c" * 40
    run = Mock(
        side_effect=[
            subprocess.CompletedProcess([], 0, stdout=""),
            subprocess.CompletedProcess([], 0, stdout=f"{head}\n"),
            subprocess.CompletedProcess(
                [],
                0,
                stdout=f"{head}\trefs/tags/v2.2.0\n",
            ),
        ]
    )
    registry_check = Mock()
    monkeypatch.setattr(publisher.subprocess, "run", run)
    monkeypatch.setattr(publisher, "_version_is_published", registry_check)

    exit_code = publisher.main([])

    assert exit_code == 0
    assert capsys.readouterr().out == "OK\n"
    assert run.call_count == 3
    registry_check.assert_not_called()


def test_release_rejects_existing_pypi_version_with_compact_json(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _configure_release_project(tmp_path, monkeypatch)
    head = "d" * 40
    run = Mock(
        side_effect=[
            subprocess.CompletedProcess([], 0, stdout=""),
            subprocess.CompletedProcess([], 0, stdout=f"{head}\n"),
            subprocess.CompletedProcess([], 0, stdout=""),
        ]
    )
    monkeypatch.setattr(publisher.subprocess, "run", run)
    monkeypatch.setattr(publisher, "_version_is_published", Mock(return_value=True))

    exit_code = publisher.main([])

    assert exit_code == 1
    assert json.loads(capsys.readouterr().out) == {
        "stage": "pypi_version",
        "exit_code": 1,
        "tag": "v2.2.0",
    }
    assert run.call_count == 3


def test_release_rejects_conflicting_remote_tag(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _configure_release_project(tmp_path, monkeypatch)
    head = "e" * 40
    other = "f" * 40
    run = Mock(
        side_effect=[
            subprocess.CompletedProcess([], 0, stdout=""),
            subprocess.CompletedProcess([], 0, stdout=f"{head}\n"),
            subprocess.CompletedProcess(
                [],
                0,
                stdout=f"{other}\trefs/tags/v2.2.0\n",
            ),
        ]
    )
    registry_check = Mock()
    monkeypatch.setattr(publisher.subprocess, "run", run)
    monkeypatch.setattr(publisher, "_version_is_published", registry_check)

    exit_code = publisher.main([])

    assert exit_code == 1
    assert json.loads(capsys.readouterr().out) == {
        "stage": "tag_conflict",
        "exit_code": 1,
        "tag": "v2.2.0",
    }
    assert run.call_count == 3
    registry_check.assert_not_called()
