from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

import scripts.deploy_local_package as local_deployer
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


def test_local_deploy_builds_and_installs_exact_temporary_wheel(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    repository = tmp_path / "repository with spaces"
    repository.mkdir()
    (repository / "pyproject.toml").write_text(
        '[project]\nname = "pdf-form-tools"\nversion = "2.4.1"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(local_deployer, "REPOSITORY_ROOT", repository)
    installed_version = Mock(return_value="2.4.1")
    monkeypatch.setattr(local_deployer.importlib.metadata, "version", installed_version)
    source_dir: Path | None = None
    output_dir: Path | None = None
    wheel: Path | None = None

    def successful_run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
        nonlocal source_dir, output_dir, wheel
        if command == ["git", "ls-files", "-z"]:
            return subprocess.CompletedProcess(
                command, 0, stdout="pyproject.toml\0", stderr=""
            )
        if "wheel" in command:
            source_dir = Path(kwargs["cwd"])
            output_dir = Path(command[command.index("--wheel-dir") + 1])
            wheel = output_dir / "pdf_form_tools-2.4.1-py3-none-any.whl"
            wheel.write_text("wheel", encoding="utf-8")
            (source_dir / "build").mkdir()
            (source_dir / "src" / "pdf_form_tools.egg-info").mkdir(parents=True)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    run = Mock(side_effect=successful_run)
    monkeypatch.setattr(local_deployer.subprocess, "run", run)

    assert local_deployer.main([]) == 0
    assert capsys.readouterr().out == "OK\n"
    assert source_dir is not None
    assert output_dir is not None
    assert wheel is not None
    assert [call.args[0] for call in run.call_args_list] == [
        ["git", "ls-files", "-z"],
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
        [
            sys.executable,
            "-m",
            "pip",
            "--disable-pip-version-check",
            "install",
            "--force-reinstall",
            "--no-deps",
            str(wheel),
        ],
    ]
    for call, cwd in zip(
        run.call_args_list,
        [repository, source_dir, repository],
        strict=True,
    ):
        assert call.kwargs == {
            "cwd": cwd,
            "capture_output": True,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "check": False,
        }
    assert not output_dir.exists()
    assert not source_dir.exists()
    assert not (repository / "build").exists()
    assert not (repository / "src" / "pdf_form_tools.egg-info").exists()
    installed_version.assert_called_once_with("pdf-form-tools")


def test_local_deploy_rejects_missing_wheel_before_install(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "pyproject.toml").write_text(
        '[project]\nname = "pdf-form-tools"\nversion = "2.4.1"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(local_deployer, "REPOSITORY_ROOT", repository)

    def successful_run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
        if command == ["git", "ls-files", "-z"]:
            return subprocess.CompletedProcess(
                command, 0, stdout="pyproject.toml\0", stderr=""
            )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    run = Mock(side_effect=successful_run)
    monkeypatch.setattr(local_deployer.subprocess, "run", run)

    assert local_deployer.main([]) == local_deployer.INTERNAL_ERROR
    assert json.loads(capsys.readouterr().out) == {
        "stage": "artifact",
        "exit_code": local_deployer.INTERNAL_ERROR,
        "detail": "expected one wheel, found 0",
    }
    assert run.call_count == 2


def test_local_deploy_rejects_installed_version_mismatch(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "pyproject.toml").write_text(
        '[project]\nname = "pdf-form-tools"\nversion = "2.4.1"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(local_deployer, "REPOSITORY_ROOT", repository)

    def successful_run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
        if command == ["git", "ls-files", "-z"]:
            return subprocess.CompletedProcess(
                command, 0, stdout="pyproject.toml\0", stderr=""
            )
        if "wheel" in command:
            output_dir = Path(command[command.index("--wheel-dir") + 1])
            (output_dir / "pdf_form_tools-2.4.1-py3-none-any.whl").write_text(
                "wheel", encoding="utf-8"
            )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    run = Mock(side_effect=successful_run)
    monkeypatch.setattr(local_deployer.subprocess, "run", run)
    monkeypatch.setattr(
        local_deployer.importlib.metadata,
        "version",
        Mock(return_value="2.3.0"),
    )

    assert local_deployer.main([]) == local_deployer.INTERNAL_ERROR
    assert json.loads(capsys.readouterr().out) == {
        "stage": "verify",
        "exit_code": local_deployer.INTERNAL_ERROR,
        "detail": "expected 2.4.1, found 2.3.0",
    }
    assert run.call_count == 3


def _configure_release_project(tmp_path: Path, monkeypatch) -> Path:
    repository = tmp_path / "release repository with spaces"
    repository.mkdir()
    (repository / "pyproject.toml").write_text(
        '[project]\nname = "pdf-form-tools"\nversion = "2.2.0"\n'
        '[project.urls]\n'
        'Repository = "https://github.com/ceratops-code/pdf-form-tools"\n',
        encoding="utf-8",
    )
    (repository / "CHANGELOG.md").write_text(
        "# Changelog\n\n## 2.2.0\n\n- Release notes.\n\n## 2.1.0\n",
        encoding="utf-8",
        newline="\n",
    )
    monkeypatch.setattr(publisher, "REPOSITORY_ROOT", repository)
    monkeypatch.setattr(publisher, "_gh", Mock(return_value=""))
    return repository


def _published_release() -> dict[str, object]:
    return {
        "tag_name": "v2.2.0",
        "draft": False,
        "prerelease": False,
        "html_url": "https://example.invalid/releases/v2.2.0",
    }


def test_release_preflight_rejects_authenticated_draft_before_push(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    repository = _configure_release_project(tmp_path, monkeypatch)
    head = "a" * 40
    draft = {
        "tag_name": "v2.2.0",
        "draft": True,
        "prerelease": False,
        "html_url": "https://example.invalid/releases/v2.2.0",
    }
    run = Mock(
        side_effect=[
            subprocess.CompletedProcess([], 0, stdout=""),
            subprocess.CompletedProcess([], 0, stdout=f"{head}\n"),
            subprocess.CompletedProcess([], 0, stdout=""),
            subprocess.CompletedProcess([], 0, stdout=json.dumps(draft)),
        ]
    )
    monkeypatch.setattr(publisher.subprocess, "run", run)
    monkeypatch.setattr(publisher, "_version_is_published", Mock(return_value=False))

    exit_code = publisher.main(["--check-only"])

    assert exit_code == 1
    assert json.loads(capsys.readouterr().out) == {
        "stage": "github_release_conflict",
        "exit_code": 1,
        "tag": "v2.2.0",
    }
    assert run.call_args_list[-1].args[0] == [
        "gh",
        "api",
        "--method",
        "GET",
        "--header",
        "Accept: application/vnd.github+json",
        "--header",
        "X-GitHub-Api-Version: 2022-11-28",
        "repos/ceratops-code/pdf-form-tools/releases/tags/v2.2.0",
    ]
    assert all(call.kwargs["cwd"] == repository for call in run.call_args_list)
    assert not any(call.args[0][:2] == ["git", "push"] for call in run.call_args_list)


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
    release_check = Mock(return_value=None)
    monkeypatch.setattr(publisher.subprocess, "run", run)
    monkeypatch.setattr(publisher, "_version_is_published", registry_check)
    monkeypatch.setattr(publisher, "_github_release", release_check)

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
    release_check.assert_called_once_with(
        "ceratops-code/pdf-form-tools", "v2.2.0"
    )


def test_release_publication_is_rejected_outside_github_actions(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _configure_release_project(tmp_path, monkeypatch)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    run = Mock()
    monkeypatch.setattr(publisher.subprocess, "run", run)

    exit_code = publisher.main(["--github-actions-release"])

    assert exit_code == 1
    assert json.loads(capsys.readouterr().out) == {
        "stage": "publication_context",
        "exit_code": 1,
        "tag": "v2.2.0",
    }
    run.assert_not_called()


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
    registry_check = Mock(return_value=True)
    release_check = Mock(return_value=_published_release())
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REF", "refs/tags/v2.2.0")
    monkeypatch.setenv("GITHUB_REF_NAME", "v2.2.0")
    monkeypatch.setattr(publisher.subprocess, "run", run)
    monkeypatch.setattr(publisher, "_version_is_published", registry_check)
    monkeypatch.setattr(publisher, "_github_release", release_check)

    exit_code = publisher.main(["--github-actions-release"])

    assert exit_code == 0
    assert capsys.readouterr().out == "OK\n"
    assert run.call_count == 3
    assert registry_check.call_count == 2
    assert release_check.call_count == 2


def test_release_retry_completes_an_interrupted_tag_publish(
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
    registry_check = Mock(return_value=True)
    release_check = Mock(side_effect=[None, _published_release(), _published_release()])
    gh = Mock(return_value="")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REF", "refs/tags/v2.2.0")
    monkeypatch.setenv("GITHUB_REF_NAME", "v2.2.0")
    monkeypatch.setattr(publisher.subprocess, "run", run)
    monkeypatch.setattr(publisher, "_version_is_published", registry_check)
    monkeypatch.setattr(publisher, "_github_release", release_check)
    monkeypatch.setattr(publisher, "_gh", gh)

    exit_code = publisher.main(["--github-actions-release"])

    assert exit_code == 0
    assert capsys.readouterr().out == "OK\n"
    assert run.call_count == 3
    assert gh.call_args.args[:4] == (
        "github_release_create",
        "release",
        "create",
        "v2.2.0",
    )


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

    exit_code = publisher.main(["--check-only"])

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
    registry_check = Mock(return_value=True)
    monkeypatch.setattr(publisher.subprocess, "run", run)
    monkeypatch.setattr(publisher, "_version_is_published", registry_check)

    exit_code = publisher.main(["--check-only"])

    assert exit_code == 1
    assert json.loads(capsys.readouterr().out) == {
        "stage": "tag_conflict",
        "exit_code": 1,
        "tag": "v2.2.0",
    }
    assert run.call_count == 3
    registry_check.assert_not_called()


@pytest.mark.parametrize("existing_tag", [False, True])
def test_local_release_triggers_once_and_verifies_actions_results(
    tmp_path: Path, monkeypatch, capsys, existing_tag: bool
) -> None:
    _configure_release_project(tmp_path, monkeypatch)
    head = "b" * 40
    run = Mock(side_effect=[
        subprocess.CompletedProcess([], 0, stdout=""),
        subprocess.CompletedProcess([], 0, stdout=head),
        subprocess.CompletedProcess(
            [], 0, stdout=f"{head}\trefs/tags/v2.2.0" if existing_tag else ""
        ),
        subprocess.CompletedProcess([], 0, stdout=""),
    ])
    monkeypatch.setattr(publisher.subprocess, "run", run)
    registry = Mock(side_effect=[True] if existing_tag else [False, True])
    releases = Mock(side_effect=[_published_release()] if existing_tag else [None, _published_release()])
    events = []
    workflow = Mock(side_effect=lambda *args: events.append("workflow"))
    pypi = Mock(side_effect=lambda *args: events.append("pypi"))
    create = Mock()
    monkeypatch.setattr(publisher, "_version_is_published", registry)
    monkeypatch.setattr(publisher, "_github_release", releases)
    monkeypatch.setattr(publisher, "_wait_for_workflow", workflow)
    monkeypatch.setattr(publisher, "_wait_for_pypi", pypi)
    monkeypatch.setattr(publisher, "_ensure_github_release", create)

    assert publisher.main(["--trigger-release"]) == 0
    assert capsys.readouterr().out == "OK\n"
    pushes = [call.args[0] for call in run.call_args_list if call.args[0][:2] == ["git", "push"]]
    assert pushes == ([] if existing_tag else [["git", "push", "origin", f"{head}:refs/tags/v2.2.0"]])
    assert events == ["workflow", "pypi"]
    workflow.assert_called_once_with(head, "v2.2.0")
    pypi.assert_called_once_with("pdf-form-tools", "2.2.0")
    create.assert_not_called()


@pytest.mark.parametrize("conclusion", ["failure", "cancelled"])
def test_failed_actions_release_stops_local_completion(
    tmp_path: Path, monkeypatch, capsys, conclusion: str
) -> None:
    _configure_release_project(tmp_path, monkeypatch)
    head = "a" * 40
    monkeypatch.setattr(publisher, "_git", Mock(side_effect=["", head]))
    monkeypatch.setattr(publisher, "_remote_tag_target", Mock(return_value=head))
    monkeypatch.setattr(publisher, "_workflow_run", Mock(return_value={
        "headSha": head, "status": "completed", "conclusion": conclusion
    }))
    pypi, release = Mock(), Mock()
    monkeypatch.setattr(publisher, "_wait_for_pypi", pypi)
    monkeypatch.setattr(publisher, "_ensure_github_release", release)

    assert publisher.main(["--trigger-release"]) == 1
    assert json.loads(capsys.readouterr().out)["stage"] == "workflow"
    pypi.assert_not_called()
    release.assert_not_called()


@pytest.mark.parametrize("published_release", [None, {"draft": True}])
def test_local_release_requires_a_verified_public_github_release(
    tmp_path: Path, monkeypatch, capsys, published_release
) -> None:
    _configure_release_project(tmp_path, monkeypatch)
    head = "a" * 40
    monkeypatch.setattr(publisher, "_git", Mock(side_effect=["", head]))
    monkeypatch.setattr(publisher, "_remote_tag_target", Mock(return_value=head))
    monkeypatch.setattr(publisher, "_wait_for_workflow", Mock())
    monkeypatch.setattr(publisher, "_wait_for_pypi", Mock())
    monkeypatch.setattr(publisher, "_version_is_published", Mock(return_value=True))
    monkeypatch.setattr(publisher, "_github_release", Mock(return_value=published_release))
    create = Mock()
    monkeypatch.setattr(publisher, "_ensure_github_release", create)

    assert publisher.main(["--trigger-release"]) == 1
    assert json.loads(capsys.readouterr().out)["stage"] == "github_release_verify"
    create.assert_not_called()


def test_release_workflow_lookup_is_bound_to_exact_tag_and_commit(monkeypatch) -> None:
    head = "a" * 40
    gh = Mock(return_value=json.dumps([{
        "headSha": head, "status": "completed", "conclusion": "success"
    }]))
    monkeypatch.setattr(publisher, "_gh", gh)

    assert publisher._workflow_run(head, "v2.2.0")["conclusion"] == "success"
    arguments = gh.call_args.args
    assert arguments[arguments.index("--workflow") + 1] == "publish-pypi.yml"
    assert arguments[arguments.index("--event") + 1] == "push"
    assert arguments[arguments.index("--branch") + 1] == "v2.2.0"
    assert arguments[arguments.index("--commit") + 1] == head


def test_release_modes_cannot_be_combined(capsys) -> None:
    assert publisher.main(["--trigger-release", "--github-actions-release"]) == 2
    assert json.loads(capsys.readouterr().out)["stage"] == "arguments"
