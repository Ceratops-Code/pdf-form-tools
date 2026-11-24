#!/usr/bin/env python3
"""Run repository Python tests, optionally retaining an exact reusable result.

Use the locked scripts environment: uv run --project scripts --locked python scripts/run-tests.py.
The caller supplies result identities and exact inputs. Current worktree results
share one append-only file, compacted at command boundaries by the generated
write_check_results module. Explicit standalone result files keep their original
contract. Without result arguments this remains a disposable test invocation.
Pytest scratch is disposable.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata
import json
import os
import pathlib
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_TARGETS: list[str] = ['tests/test_pdf_form_overlay.py', 'tests/test_validate_repository.py']


def test_support():
    """Load test support without writing launcher bytecode into the checkout."""
    sys.path.insert(0, str(ROOT / "scripts/testing"))
    original_bytecode_setting = sys.dont_write_bytecode
    try:
        sys.dont_write_bytecode = True
        return importlib.import_module("reuse_test_results")
    finally:
        sys.dont_write_bytecode = original_bytecode_setting


def without_basetemp(arguments: list[str]) -> list[str]:
    """Remove inherited locations before pytest validates every occurrence."""
    options = iter(arguments)
    retained = []
    for option in options:
        if option == "--basetemp":
            next(options, None)
        elif not option.startswith("--basetemp="):
            retained.append(option)
    return retained


sys.path.insert(0, str(ROOT / "scripts"))
from write_check_results import (  # noqa: E402
    CHECK_CONTEXT_ENV,
    CurrentCheckResults,
    add_result_arguments,
    describe_result_contract,
    run_recorded_check,
)


def _scratch_entry(path: pathlib.Path) -> os.stat_result | None:
    try:
        entry = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(entry.st_mode) or getattr(entry, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise OSError(f"Unsafe test scratch directory: {path}")
    return entry


if sys.platform == "win32":
    def _unlink_readonly_scratch_hardlink(path: str) -> None:
        """Remove the owned name while retaining attributes shared by other links."""
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        open_file = kernel.CreateFileW
        open_file.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        open_file.restype = wintypes.HANDLE
        set_info = kernel.SetFileInformationByHandle
        set_info.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
        set_info.restype = wintypes.BOOL
        close_handle = kernel.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL

        native_path = os.path.abspath(path)
        if not native_path.startswith("\\\\?\\"):
            if native_path.startswith("\\\\"):
                native_path = "\\\\?\\UNC\\" + native_path[2:]
            else:
                native_path = "\\\\?\\" + native_path
        # DELETE access, share read/write/delete, OPEN_EXISTING, OPEN_REPARSE_POINT.
        handle = open_file(native_path, 0x00010000, 7, None, 3, 0x00200000, None)
        if handle == ctypes.c_void_p(-1).value:
            raise OSError(f"Cannot open test scratch link {path}: {ctypes.WinError(ctypes.get_last_error())}")
        try:
            # FileDispositionInfoEx: unlink this name and ignore read-only.
            disposition = wintypes.DWORD(0x01 | 0x02 | 0x10)
            if not set_info(handle, 21, ctypes.byref(disposition), ctypes.sizeof(disposition)):
                raise OSError(f"Cannot unlink test scratch link {path}: {ctypes.WinError(ctypes.get_last_error())}")
        finally:
            close_handle(handle)


def _remove_readonly_scratch(
    remove: Callable[[str], object], path: str, error: BaseException,
) -> None:
    entry = pathlib.Path(path).lstat()
    if sys.platform == "win32":
        if (not isinstance(error, PermissionError)
                or entry.st_mode & stat.S_IWRITE
                or getattr(entry, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise error
        if stat.S_ISREG(entry.st_mode) and entry.st_nlink > 1:
            _unlink_readonly_scratch_hardlink(path)
            return
        os.chmod(path, entry.st_mode | stat.S_IWRITE)
        remove(path)
        return
    raise error


def _remove_test_scratch(directory: pathlib.Path) -> None:
    """Remove this runner's private directory without following a redirected path."""
    if any(path.is_symlink() or path.is_junction() for path in directory.parents):
        raise OSError(f"Test scratch has a linked parent: {directory}")
    entry = _scratch_entry(directory)
    if entry is None:
        return
    if sys.platform != "win32" and (entry.st_uid != os.getuid() or entry.st_mode & 0o077):
        raise OSError(f"Test scratch must be private to the current user: {directory}")
    try:
        shutil.rmtree(directory, onexc=_remove_readonly_scratch)
    except OSError as exc:
        raise OSError(f"Cannot remove test scratch {directory}: {exc}") from exc


@contextmanager
def _owned_test_scratch(
    checkout: pathlib.Path, parent: pathlib.Path | None, environ: Mapping[str, str],
) -> Iterator[pathlib.Path]:
    """One top-level test tree owns this checkout's scratch until its workers exit."""
    checkout = checkout.resolve()
    linked = checkout.parent.parent.name.casefold() == "worktrees"
    project = checkout.parent.name if linked else checkout.name
    projects = checkout.parents[2] if linked else checkout.parent
    configured_temp = pathlib.Path(environ.get("CERATOPS_TASK_TEMP_ROOT") or projects / "tmp")
    parent = pathlib.Path(os.path.abspath(checkout / (parent or environ.get("PYTEST_DEBUG_TEMPROOT") or configured_temp)))
    project_temp = configured_temp / project
    # SDLC can pass an already-qualified task directory. Always take the
    # checkout identity from the working tree, including on the release branch.
    if parent == project_temp or parent.parent == project_temp:
        base = configured_temp
    else:
        base = parent
    key = checkout.name
    configured_projects = environ.get("CERATOPS_PROJECTS_ROOT")
    if ((configured_projects and projects != pathlib.Path(configured_projects).resolve())
            or (not configured_projects and (base != configured_temp or configured_temp.parent != projects))
            or (linked and key.casefold() == project.casefold())):
        # A caller can share a temp base across unrelated same-named clones.
        key = hashlib.sha256(os.fsencode(os.path.normcase(str(checkout)))).hexdigest()[:16]
    scope = base / project / key / ".pytest-scratch"
    inherited = environ.get("CERATOPS_PYTEST_SCRATCH_ROOT")
    active = pathlib.Path(inherited) if inherited else None
    owner = next((p for p in (active, *active.parents) if p.name == ".pytest-scratch"), None) if active else None
    nested = active is not None and owner is not None and (
        parent == active or scope == owner or parent == owner.parent.parent.parent
    )
    target = active if nested and active is not None else scope
    for path in (parent, *parent.parents, target, *target.parents):
        if path.is_symlink() or path.is_junction():
            raise OSError(f"Test scratch cannot pass through a linked directory: {path}")
    if nested:
        directory = pathlib.Path(tempfile.mkdtemp(prefix="_nested-", dir=target))
    else:
        directory = scope
        _remove_test_scratch(directory)
        directory.mkdir(parents=True, mode=0o700)
    try:
        yield directory
    finally:
        # Test commands join their workers before returning. After a hard stop,
        # the next top-level invocation runs only once that worker tree has exited.
        _remove_test_scratch(directory)


def execute_check(args: argparse.Namespace, targets: list[str], inherited: list[str]) -> int:
    """Both the observable probe and pytest use the same result lifecycle."""
    if args.probe_command is None:
        for target in targets:
            path = (ROOT / target.split("::", 1)[0]).resolve()
            if not path.is_relative_to(ROOT) or not path.exists():
                raise ValueError(f"test target must exist inside this repository: {target}")
    temporary = args.scratch_directory
    environment = os.environ.copy()
    environment.pop(CHECK_CONTEXT_ENV, None)
    environment["CERATOPS_PYTEST_SCRATCH_ROOT"] = str(temporary)
    environment.update(dict.fromkeys(("TEMP", "TMP", "TMPDIR", "PYTEST_DEBUG_TEMPROOT"), str(temporary)))
    environment["PYTHONPYCACHEPREFIX"] = str(temporary / "python-cache")
    environment["PYTEST_ADDOPTS"] = shlex.join(inherited)
    test_command = [
        sys.executable, "-m", "pytest", *targets, *without_basetemp(args.pytest_arg),
    ]
    command = args.probe_command or [
        *test_command,
        "--basetemp", str(temporary / "pytest"),
        "-o", "cache_dir=" + str(temporary / "pytest-cache"),
    ]
    if args.probe_command is None:
        reuse_test_results = test_support()
        environment.update(reuse_test_results.result_environment(
            test_command, ROOT, os.environ, result_writer=ROOT / "scripts/write_check_results.py",
        ))
        environment["CERATOPS_TEST_ARTIFACTS"] = json.dumps(args.artifacts)
        environment["PYTHONPATH"] = os.pathsep.join(filter(None, (
            str(ROOT / "scripts/testing"), environment.get("PYTHONPATH"),
        )))
        environment[reuse_test_results.RESULT_SUMMARY_ENV] = str(temporary / "pytest-results.json")
        command[3:3] = ["-p", "reuse_test_results"]
    args.executed = True
    launch_error = None
    try:
        result = subprocess.run(command, cwd=ROOT, env=environment, check=False,
                                capture_output=True, text=True, encoding="utf-8", errors="replace")
    except OSError as exc:
        launch_error = str(exc)
        result = subprocess.CompletedProcess(command, 2, "", str(exc))
    if args.probe_command is None:
        result.stdout = reuse_test_results.redact_test_diagnostic(result.stdout)
        result.stderr = reuse_test_results.redact_test_diagnostic(result.stderr)
    print(result.stdout, end="")
    print(result.stderr, end="", file=sys.stderr)
    summary_path = temporary / "pytest-results.json"
    if summary_path.is_file():
        observed = json.loads(summary_path.read_text(encoding="utf-8"))
        args.check_summary = observed["check_summary"]
        args.observed_checks = [*observed["result_references"], *observed["nonreusable_checks"],
                                *observed["skipped_checks"], *observed["passed_checks"]]
    elif args.probe_command is None:
        args.observed_checks = [{"id": "pytest", "status": "scope-unrepresented", "reusable": False,
                                 "reason": "Individual reuse needs declared tests/test-impact.json inputs and completed pytest outcomes."}]
    if result.returncode and args.probe_command is None:
        reuse_test_results.save_test_diagnostic(args.diagnostic_path, {
            "schema": "ceratops-ai-agents-kit-pytest-diagnostic.v1", "cwd": str(ROOT),
            "command": command, "runtime": sys.executable, "request": args.invocation,
            "result_request": args.invocation, "exit_code": None if launch_error else result.returncode,
            "stdout": result.stdout, "stderr": result.stderr,
        }, root=ROOT, current=args.current_results, summary=args.check_summary)
    return result.returncode if result.returncode >= 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("targets", nargs="*", help="Repository-relative test files or directories.")
    parser.add_argument("--pytest-arg", action="append", default=[], help="Repeat using --pytest-arg=VALUE.")
    parser.add_argument("--check-group", help="One suite ID from tests/test-impact.json.")
    parser.add_argument("--temporary-root", type=pathlib.Path,
                        help="Temp base for checkout-scoped pytest scratch; SDLC may supply the checkout directory.")
    parser.add_argument("--artifacts", default="{}", help="JSON mapping deliverable references to selected file-path arrays.")
    parser.add_argument("--diagnostic-output", type=pathlib.Path,
                        help="Full failure report; files outside .test-diagnostics are caller-retained.")
    add_result_arguments(parser)
    args = parser.parse_args(argv)
    if args.describe_test_results:
        describe_result_contract()
        return 0
    if args.temporary_root is None:
        parser.error("--temporary-root is required for test execution")
    try:
        parent = pathlib.Path(os.path.abspath(ROOT / args.temporary_root))
        if any(path.is_symlink() or path.is_junction() for path in (parent, *parent.parents)):
            raise ValueError(f"--temporary-root must not pass through a directory link: {parent}")
        args.temporary_root = parent
        args.artifacts = json.loads(args.artifacts)
        support = test_support() if args.probe_command is None or args.artifacts or args.check_group else None
        if support is not None:
            support.measure_selected_artifacts(ROOT, args.artifacts)
        args.observed_checks = []
        args.check_summary = None
        args.executed = False
        args.diagnostic_path = (ROOT / (args.diagnostic_output or pathlib.Path(".test-diagnostics/pytest-failure.json"))).resolve()
        args.current_results = CurrentCheckResults(ROOT) if support is not None and (ROOT / ".git").exists() else None
        if support is not None:
            support.remove_test_diagnostic(args.diagnostic_path, root=ROOT, current=args.current_results)
        if args.probe_command is not None:
            args.probe_command = json.loads(args.probe_command)
            if (not isinstance(args.probe_command, list) or not args.probe_command
                    or any(not isinstance(item, str) or "\0" in item for item in args.probe_command)
                    or not args.probe_command[0]):
                raise ValueError("probe-command must be a nonempty JSON argv")
        inherited = without_basetemp(shlex.split(os.environ.get("PYTEST_ADDOPTS", "")))
        targets = args.targets or DEFAULT_TARGETS
        if args.check_group:
            assert support is not None
            manifest = support.load_manifest(ROOT / "tests/test-impact.json")
            if args.check_group not in manifest.suites:
                raise ValueError(f"Unknown test check-group: {args.check_group}")
            if args.targets:
                raise ValueError("Choose test targets or --check-group, not both")
            targets = list(manifest.suites[args.check_group].pytest)
        invocation = {
            "targets": [] if args.probe_command else targets,
            "pytest_args": [] if args.probe_command else without_basetemp(args.pytest_arg),
            "pytest_addopts": [] if args.probe_command else inherited,
            "probe_command": args.probe_command,
        }
        if args.check_group or args.artifacts:
            invocation.update(check_group=args.check_group, artifacts=args.artifacts)
        args.invocation = invocation
        token = support.CHECK_REQUEST_INPUTS.set({"checkGroup": args.check_group, "artifacts": args.artifacts,
                                                 "diagnosticPath": str(args.diagnostic_path)}) if support else None
        request_executed = False

        def execute_request() -> int:
            nonlocal request_executed
            request_executed = True
            return execute_check(args, targets, inherited)

        try:
            with _owned_test_scratch(ROOT, args.temporary_root, os.environ) as args.scratch_directory:
                code = run_recorded_check(execute_request, args, invocation, repo_root=ROOT,
                                         checks=(lambda: args.observed_checks) if args.probe_command is None else None,
                                         summary=(lambda: args.check_summary) if args.probe_command is None else None)
            if support is not None:
                support.remove_test_diagnostic(args.diagnostic_path, root=ROOT, current=args.current_results,
                                               successful_request=invocation if code == 0 and (args.executed or not request_executed) else None)
            return code
        finally:
            if support is not None:
                support.CHECK_REQUEST_INPUTS.reset(token)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
