"""Ordinary pytest runs never contact production or launch host processes."""

import os
import signal
import subprocess
from contextlib import ExitStack
from unittest.mock import AsyncMock

import pytest

from io_guard import IOGuard

# Capture only for the explicitly opted-in external parity bridge below.
_REAL_POPEN = subprocess.Popen
_REAL_KILL = os.kill


def pytest_addoption(parser):
    parser.addoption(
        "--run-guardian-parity", action="store_true", default=False,
        help="Run the external Guardian parity subprocess (not covered by the unit I/O sandbox)",
    )


def pytest_configure(config):
    config.addinivalue_line("markers", "integration: explicitly opted-in external integration")
    config.addinivalue_line("markers", "guardian_parity: external Guardian args parity bridge")


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--run-guardian-parity"):
        for item in items:
            if item.get_closest_marker("guardian_parity"):
                item.add_marker(pytest.mark.skip(reason="Requires explicit --run-guardian-parity"))


@pytest.fixture
def guardian_parity_bridge(request, monkeypatch, io_guard):
    """One exact bridge invocation; all other parent I/O stays guarded."""
    if not request.config.getoption("--run-guardian-parity"):
        pytest.skip("Requires explicit --run-guardian-parity")
    from test_phase_a import _GUARDIAN_BRIDGE_SCRIPT, GUARDIAN_ROOT, GUARDIAN_VENV

    used = False

    def run(config_path):
        nonlocal used
        if used:
            io_guard.reject("repeated Guardian parity subprocess", config_path)
        used = True
        argv = [GUARDIAN_VENV, "-c", _GUARDIAN_BRIDGE_SCRIPT]
        env = {**os.environ, "GG_ROOT": GUARDIAN_ROOT, "GG_CFG": str(config_path)}
        child = None

        def popen(args, **kwargs):
            nonlocal child
            expected = {
                "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
                "text": True, "env": env,
            }
            if child is not None or args != argv or kwargs != expected:
                io_guard.reject("unexpected Guardian parity subprocess", args)
            child = _REAL_POPEN(args, **kwargs)
            return child

        def kill(pid, sig):
            # subprocess.run's timeout cleanup may kill only its own bridge.
            if child is None or pid != child.pid or sig != signal.SIGKILL:
                io_guard.reject("unexpected process signal", (pid, sig))
            return _REAL_KILL(pid, sig)

        with monkeypatch.context() as patch:
            patch.setattr(subprocess, "Popen", popen)
            patch.setattr(os, "kill", kill)
            return subprocess.run(
                argv, capture_output=True, text=True, env=env, timeout=60, check=False,
            )

    return run


@pytest.fixture(autouse=True)
def io_guard(monkeypatch):
    guard = IOGuard()
    guard.install(monkeypatch)
    yield guard
    # Also detect code that caught BaseException or discarded a task exception.
    guard.assert_clean()


@pytest.fixture
def isolated_api_lifespan(monkeypatch):
    """API contract tests do not own Comfy's background poller or llama watchdog."""
    from caretaker import comfy

    monkeypatch.setattr(comfy, "init_async", AsyncMock(return_value=None))
    monkeypatch.setattr(comfy, "shutdown_async", AsyncMock(return_value=None))
    monkeypatch.setenv("CARETAKER_WATCHDOG_ENABLED", "0")


@pytest.fixture
def allow_test_listener(io_guard):
    """Register a test-created listener until fixture teardown; never whitelist ports."""
    with ExitStack() as stack:
        yield lambda server: stack.enter_context(io_guard.allow_listener(server))
