"""F6 Phase E: the Windows ServerProcess implementation.

The caretaker on the Windows/14700K host (NSSM service, no systemd) spawns
llama-server.exe directly and owns its process tree.  These tests run on
Linux: the platform-dependent bits (CREATE_NEW_PROCESS_GROUP, taskkill) are
mocked/scripted and the selection logic is pinned via monkeypatched
``sys.platform``.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from unittest.mock import AsyncMock, MagicMock

from caretaker.manager import SystemdServerProcess, _default_server_process
from caretaker.windows_process import (
    WindowsDirectServerProcess,
    _split_args_windows,
)


def test_default_process_is_systemd_on_posix(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert isinstance(_default_server_process(), SystemdServerProcess)


def test_default_process_is_windows_impl_on_win32(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    impl = _default_server_process()
    assert isinstance(impl, WindowsDirectServerProcess)


def test_split_args_windows_keeps_backslash_paths():
    """POSIX shlex mangles backslash paths; the Windows splitter keeps them
    verbatim and strips the matching surrounding quotes."""
    args = "-m C:\\models\\qwen3 8b.gguf -ngl 99 --host 0.0.0.0"
    quoted = "-m \"C:\\models\\qwen3 8b.gguf\" -ngl 99 --host 0.0.0.0"
    assert _split_args_windows(quoted) == [
        "-m",
        "C:\\models\\qwen3 8b.gguf",
        "-ngl",
        "99",
        "--host",
        "0.0.0.0",
    ]
    # Unquoted simple tokens pass through untouched.
    assert _split_args_windows(args) == [
        "-m",
        "C:\\models\\qwen3",
        "8b.gguf",
        "-ngl",
        "99",
        "--host",
        "0.0.0.0",
    ]


async def test_windows_start_spawns_with_process_group(monkeypatch, tmp_path):
    """start() spawns llama-server.exe with the args-file argv and
    CREATE_NEW_PROCESS_GROUP on win32 (flag 0 elsewhere — tests run on
    POSIX)."""
    args_file = tmp_path / "current_model.args"
    args_file.write_text('-m "C:\\models\\qwen3.gguf" -ngl 99', encoding="utf-8")
    impl = WindowsDirectServerProcess(binary="llama-server.exe")

    captured: dict = {}
    fake_proc = MagicMock(pid=4242, returncode=None)
    fake_proc.wait = AsyncMock(return_value=0)

    async def _fake_exec(*argv, **kwargs):
        captured["argv"] = argv
        captured["creationflags"] = kwargs.get("creationflags")
        return fake_proc

    monkeypatch.setattr(
        "caretaker.windows_process.CURRENT_MODEL_ARGS_FILE", args_file
    )
    monkeypatch.setattr("asyncio.create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(sys, "platform", "win32")

    await impl.start()
    assert captured["argv"][:2] == ("llama-server.exe", "-m")
    assert "C:\\models\\qwen3.gguf" in captured["argv"]
    # Windows CREATE_NEW_PROCESS_GROUP (0x200); the constant itself only
    # exists on Windows, so the literal is pinned here (0x400 would be
    # CREATE_UNICODE_ENVIRONMENT).
    assert captured["creationflags"] == 0x200
    assert impl._proc is fake_proc


async def test_windows_stop_tree_kills_on_win32(monkeypatch):
    """stop() tree-kills via taskkill /PID /T /F on win32 (covers the
    llama-server worker children; POSIX killpg does not exist there)."""
    impl = WindowsDirectServerProcess()
    fake_proc = MagicMock(pid=4242, returncode=None)
    fake_proc.wait = AsyncMock(return_value=0)
    impl._proc = fake_proc

    captured: dict = {}
    killer = MagicMock(returncode=0)
    killer.wait = AsyncMock(return_value=0)

    async def _fake_exec(*argv, **kwargs):
        captured["argv"] = argv
        return killer

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr("asyncio.create_subprocess_exec", _fake_exec)

    await impl.stop()
    assert captured["argv"] == ("taskkill", "/PID", "4242", "/T", "/F")
    # taskkill only initiates termination (TerminateProcess is asynchronous
    # per MSDN) — stop() must still wait (bounded) for the tree to actually
    # exit so a follow-up start() can re-bind the port deterministically.
    fake_proc.wait.assert_awaited_once()
    assert impl._proc is None


async def test_windows_stop_falls_back_to_terminate_on_posix(monkeypatch):
    """On a POSIX host (tests) stop() uses terminate→kill, never taskkill —
    so the same code path stays runnable in the Linux test suite."""
    impl = WindowsDirectServerProcess()
    fake_proc = MagicMock(pid=99, returncode=None)
    fake_proc.terminate = MagicMock()
    fake_proc.kill = MagicMock()
    fake_proc.wait = AsyncMock(return_value=0)
    impl._proc = fake_proc

    exec_calls: list = []

    async def _fake_exec(*argv, **kwargs):
        exec_calls.append(argv)
        return MagicMock(wait=AsyncMock(return_value=0))

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("asyncio.create_subprocess_exec", _fake_exec)

    await impl.stop()
    fake_proc.terminate.assert_called_once()
    assert exec_calls == [], "no taskkill on POSIX"
    assert impl._proc is None


async def test_windows_stop_logs_taskkill_failure(monkeypatch, caplog):
    """A failed taskkill (non-zero exit) or a tree that survives the bounded
    wait must not fail silently: the win32 branch logs a warning. The manager
    clears its bookkeeping when stop() returns, so the service log is the
    only trace of a tree that is still alive."""
    impl = WindowsDirectServerProcess()
    fake_proc = MagicMock(pid=4242, returncode=None)

    async def _hang():
        await asyncio.sleep(30)

    # The tree survives the bounded wait → wait_for raises TimeoutError.
    fake_proc.wait = _hang
    impl._proc = fake_proc

    killer = MagicMock(returncode=1)
    killer.wait = AsyncMock(return_value=1)

    async def _fake_exec(*argv, **kwargs):
        return killer

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr("asyncio.create_subprocess_exec", _fake_exec)

    with caplog.at_level(logging.WARNING):
        await impl.stop()
    assert "taskkill /PID 4242 /T /F exited 1" in caplog.text
    assert "still alive 5s after taskkill" in caplog.text
    assert impl._proc is None
