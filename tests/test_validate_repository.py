from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

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
