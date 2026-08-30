"""Windows backend for the caretaker ServerProcess interface (F6, Phase E).

No systemd on Windows: the caretaker itself runs as an NSSM service (see
``deploy/windows/NSSM.md``) and owns the ``llama-server.exe`` child process
tree directly.

Platform notes:
- spawn with ``CREATE_NEW_PROCESS_GROUP`` so the child group is addressable;
- stop via ``taskkill /PID <pid> /T /F`` (tree-kill covers worker children —
  the POSIX ``os.killpg`` used by :class:`DirectServerProcess` does not exist
  on Windows);
- args splitting: the POSIX shlexer mangles Windows backslash paths, so the
  non-POSIX shlexer is used and surrounding quotes are stripped per token
  (the args file is written by this same caretaker);
- health probing is platform-neutral (shared ``ServerProcess.health_ok``);
- crash introspection is unavailable → the abstract defaults apply (0 / False
  / "Unknown error …").
"""

from __future__ import annotations

import asyncio
import logging
import shlex
import subprocess
import sys
from pathlib import Path

from .manager import ServerProcess
from .paths import CURRENT_MODEL_ARGS_FILE, LLAMA_SERVER_BIN

logger = logging.getLogger("caretaker.windows_process")


def _split_args_windows(args_text: str) -> list[str]:
    """Split the args line the Windows-safe way.

    POSIX shlex would treat backslashes as escape characters (mangling
    ``C:\\models\\x.gguf``), so the non-POSIX shlexer keeps them verbatim;
    the surrounding quotes it preserves per token (double or single — the
    POSIX impl strips both via posix semantics, so the Windows splitter
    mirrors that for operator-quoted values) are stripped here.
    """
    tokens = shlex.split(args_text, posix=False)
    return [
        token[1:-1]
        if len(token) > 1 and token[0] == token[-1] and token[0] in "\"'"
        else token
        for token in tokens
    ]


class WindowsDirectServerProcess(ServerProcess):
    """Spawn ``llama-server.exe`` directly and own its process tree (F6)."""

    def __init__(self, binary: str = str(LLAMA_SERVER_BIN)) -> None:
        self.binary = binary
        self._proc: asyncio.subprocess.Process | None = None

    async def start(self) -> None:
        args_text = Path(CURRENT_MODEL_ARGS_FILE).read_text(encoding="utf-8").strip()
        argv = [self.binary, *_split_args_windows(args_text)]
        creationflags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
            if sys.platform == "win32"
            else 0
        )
        self._proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            creationflags=creationflags,
        )

    async def stop(self) -> None:
        proc = self._proc
        if proc is None:
            return
        if proc.returncode is None:
            if sys.platform == "win32":
                # Tree-kill: covers the llama-server worker children.
                killer = await asyncio.create_subprocess_exec(
                    "taskkill", "/PID", str(proc.pid), "/T", "/F",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await killer.wait()
                if killer.returncode != 0:
                    # taskkill also exits non-zero when the process is already
                    # gone — this warning keeps that apart from a real
                    # "access denied / tree still alive" failure.
                    logger.warning(
                        "taskkill /PID %s /T /F exited %s — llama-server tree "
                        "may still be running",
                        proc.pid,
                        killer.returncode,
                    )
                # taskkill only initiates termination (TerminateProcess is
                # asynchronous per MSDN); wait (bounded) for the tree to
                # actually exit so a follow-up start() can re-bind the port
                # without racing the dying llama-server.
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except TimeoutError:
                    logger.warning(
                        "llama-server pid %s still alive 5s after taskkill — "
                        "it may still hold its port/VRAM",
                        proc.pid,
                    )
            else:
                # POSIX host (tests): plain terminate/kill — no killpg here.
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except TimeoutError:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except TimeoutError:
                    pass
        self._proc = None
