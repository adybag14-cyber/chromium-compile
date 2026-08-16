#!/usr/bin/env python3
"""Run a command while enforcing bounded durable Ninja-progress liveness."""
from __future__ import annotations

import argparse
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Sequence

TIMEOUT_EXIT_CODE = 124
WATCHDOG_ERROR_EXIT_CODE = 85
STALL_EXIT_CODE = 86
MIN_COMPILER_STALL_SECONDS = 30 * 60
MAX_COMPILER_STALL_SECONDS = 180 * 60
MAX_COMPILER_TIMEOUT_SECONDS = 340 * 60
DEFAULT_PROGRESS_LOG = Path("out/Release_x86/.ninja_log")
DEFAULT_STALL_MARKER = Path(".ninja-stall-watchdog.marker")
DEFAULT_ERROR_MARKER = Path(".ninja-stall-watchdog.error")


class WatchdogError(RuntimeError):
    pass


def progress_fingerprint(path: Path) -> tuple[int, int] | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise WatchdogError(f"could not inspect Ninja progress log {path}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise WatchdogError(f"Ninja progress log is not a regular file: {path}")
    return info.st_size, info.st_mtime_ns


def _signal_child(proc: subprocess.Popen[bytes], signum: int) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signum)
        else:
            proc.terminate()
    except ProcessLookupError:
        pass


def _kill_child(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except ProcessLookupError:
        pass


def terminate_child(proc: subprocess.Popen[bytes], grace_seconds: int) -> None:
    if proc.poll() is not None:
        return
    _signal_child(proc, signal.SIGTERM)
    deadline = time.monotonic() + grace_seconds
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.2)
    if proc.poll() is None:
        _kill_child(proc)
    try:
        proc.wait(timeout=max(grace_seconds, 1))
    except subprocess.TimeoutExpired as exc:
        raise WatchdogError("stalled compiler process tree did not terminate after SIGKILL") from exc


def write_stall_marker(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("stalled\n", encoding="utf-8")
    except OSError as exc:
        raise WatchdogError(f"could not write Ninja stall marker {path}: {exc}") from exc


def validate_compiler_stall_seconds(stall_seconds: int) -> int:
    if (
        not isinstance(stall_seconds, int)
        or isinstance(stall_seconds, bool)
        or not MIN_COMPILER_STALL_SECONDS <= stall_seconds <= MAX_COMPILER_STALL_SECONDS
    ):
        raise WatchdogError(
            "stall_seconds must be an integer from "
            f"{MIN_COMPILER_STALL_SECONDS} through {MAX_COMPILER_STALL_SECONDS}"
        )
    return stall_seconds


def validate_compiler_timeout_seconds(timeout_seconds: int) -> int:
    if (
        not isinstance(timeout_seconds, int)
        or isinstance(timeout_seconds, bool)
        or not 1 <= timeout_seconds <= MAX_COMPILER_TIMEOUT_SECONDS
    ):
        raise WatchdogError(
            f"timeout_seconds must be an integer from 1 through {MAX_COMPILER_TIMEOUT_SECONDS}"
        )
    return timeout_seconds


def compiler_command() -> list[str]:
    return [
        "autoninja",
        "-C",
        "out/Release_x86",
        "-j3",
        "chrome",
        "chrome/installer/linux:installer_deps",
    ]


def clear_marker(path: Path, label: str) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        raise WatchdogError(f"could not clear stale {label} marker {path}: {exc}") from exc


def write_error_marker(path: Path, detail: str) -> None:
    try:
        path.write_text(detail.rstrip() + "\n", encoding="utf-8")
    except OSError as exc:
        raise WatchdogError(f"could not write Ninja watchdog error marker {path}: {exc}") from exc


def run_with_watchdog(
    command: Sequence[str],
    *,
    progress_log: Path,
    stall_seconds: int,
    poll_seconds: int,
    kill_grace_seconds: int,
    stall_marker: Path,
    timeout_seconds: int | None = None,
    timeout_kill_grace_seconds: int = 120,
) -> int:
    if not command:
        raise WatchdogError("a child command is required")
    for value, name, minimum, maximum in (
        (stall_seconds, "stall_seconds", 1, 24 * 60 * 60),
        (poll_seconds, "poll_seconds", 1, 60),
        (kill_grace_seconds, "kill_grace_seconds", 1, 120),
        (timeout_kill_grace_seconds, "timeout_kill_grace_seconds", 1, 180),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
            raise WatchdogError(f"{name} must be an integer from {minimum} through {maximum}")

    if timeout_seconds is not None:
        timeout_seconds = validate_compiler_timeout_seconds(timeout_seconds)
    clear_marker(stall_marker, "Ninja stall")

    popen_kwargs: dict[str, object] = {}
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(list(command), **popen_kwargs)
    except OSError as exc:
        raise WatchdogError(f"could not start compiler command: {exc}") from exc

    last_fingerprint = progress_fingerprint(progress_log)
    started = time.monotonic()
    last_progress = started
    absolute_deadline = started + timeout_seconds if timeout_seconds is not None else None
    forwarded_signal: int | None = None

    def forward(signum: int, _frame: object) -> None:
        nonlocal forwarded_signal
        forwarded_signal = signum
        _signal_child(proc, signum)

    previous_handlers: dict[int, object] = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, forward)

    try:
        while True:
            returncode = proc.poll()
            if returncode is not None:
                if forwarded_signal is not None:
                    return 128 + forwarded_signal
                if returncode < 0:
                    return 128 + (-returncode)
                return returncode

            current = progress_fingerprint(progress_log)
            now = time.monotonic()
            if current != last_fingerprint:
                last_fingerprint = current
                last_progress = now

            if absolute_deadline is not None and now >= absolute_deadline:
                print(
                    "::warning::Compiler slice reached its absolute checkpoint deadline; "
                    "terminating the compiler process tree so state can be preserved.",
                    file=sys.stderr,
                    flush=True,
                )
                terminate_child(proc, timeout_kill_grace_seconds)
                return TIMEOUT_EXIT_CODE

            if now - last_progress >= stall_seconds:
                write_stall_marker(stall_marker)
                print(
                    f"::warning::Ninja durable progress log did not change for {stall_seconds} seconds; "
                    "terminating this compiler slice early so its checkpoint can move to a fresh runner.",
                    file=sys.stderr,
                    flush=True,
                )
                terminate_child(proc, kill_grace_seconds)
                return STALL_EXIT_CODE

            sleep_seconds = float(poll_seconds)
            if absolute_deadline is not None:
                sleep_seconds = min(sleep_seconds, max(0.05, absolute_deadline - now))
            sleep_seconds = min(sleep_seconds, max(0.05, stall_seconds - (now - last_progress)))
            time.sleep(sleep_seconds)
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        if proc.poll() is None:
            terminate_child(proc, kill_grace_seconds)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stall-seconds", required=True, type=int)
    parser.add_argument("--timeout-seconds", required=True, type=int)
    args = parser.parse_args()
    try:
        stall_seconds = validate_compiler_stall_seconds(args.stall_seconds)
        timeout_seconds = validate_compiler_timeout_seconds(args.timeout_seconds)
        command = compiler_command()
    except WatchdogError as exc:
        parser.error(str(exc))

    try:
        clear_marker(DEFAULT_ERROR_MARKER, "Ninja watchdog error")
        return run_with_watchdog(
            command,
            progress_log=DEFAULT_PROGRESS_LOG,
            stall_seconds=stall_seconds,
            poll_seconds=15,
            kill_grace_seconds=30,
            stall_marker=DEFAULT_STALL_MARKER,
            timeout_seconds=timeout_seconds,
            timeout_kill_grace_seconds=120,
        )
    except WatchdogError as exc:
        detail = f"Ninja stall watchdog internal failure: {exc}"
        try:
            write_error_marker(DEFAULT_ERROR_MARKER, detail)
        except WatchdogError as marker_exc:
            print(f"::error::{detail}; additionally {marker_exc}", file=sys.stderr, flush=True)
            return WATCHDOG_ERROR_EXIT_CODE
        print(f"::error::{detail}", file=sys.stderr, flush=True)
        return WATCHDOG_ERROR_EXIT_CODE


if __name__ == "__main__":
    raise SystemExit(main())
