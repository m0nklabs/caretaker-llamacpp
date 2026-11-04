"""Backend-death contract on ``GET /status`` (Guardian ``last_death``).

Guardian polls ``GET <caretaker>/status`` and, when the body carries
``last_death``, records a crash with ``origin: reported_death``. Without this
contract an OOM or crash during an idle session is invisible: nobody asked the
daemon to load anything, so nothing reports it.

The wire vocabulary is Guardian's (``docs/API_REFERENCE.md``): ``cause`` is one
of ``oom`` / ``signal`` / ``exit_code``, ``timestamp`` is when the daemon
observed the death, and the key must be **present** — ``null`` means
"implemented, nothing to report", absent means "predates the contract".

Two exclusions are pinned here because they decide whether the signal is
truthful:

* a death during an ``/ensure`` the daemon served is that caller's
  ``model_load_failed`` + ``crash_details``, not an unattended death;
* ``unload()`` is a deliberate stop, not a death.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yaml
from caretaker.manager import Caretaker, CrashRecord, death_cause


def _make_manager(tmp_path: Path) -> Caretaker:
    """A manager over a throwaway config with one model, no network I/O."""
    cfg = tmp_path / "models.yaml"
    cfg.write_text(
        yaml.safe_dump({"models": {"minimal": {"path": "/nonexistent/minimal.gguf"}}}),
        encoding="utf-8",
    )
    mgr = Caretaker(config_path=str(cfg), check_vram=False)
    mgr._save_context = AsyncMock(return_value=None)
    mgr._load_context = AsyncMock(return_value=None)
    mgr._free_gpu_memory = AsyncMock(return_value=None)
    return mgr


def _crash(**kwargs) -> CrashRecord:
    """A crash record with the boring fields filled in; ``kwargs`` override."""
    return CrashRecord(
        timestamp=kwargs.pop("timestamp", "2026-11-04T04:00:00+00:00"),
        model=kwargs.pop("model", "minimal"),
        error_message=kwargs.pop("error_message", "backend died"),
        exit_code=kwargs.pop("exit_code", None),
        **kwargs,
    )


# ── the cause derivation ────────────────────────────────────────────────


def test_oom_crash_is_reported_as_oom() -> None:
    cause, exit_code, signal_name = death_cause(
        _crash(oom=True, oom_source="cuda", exit_code=1, error_message="CUDA out of memory")
    )
    assert (cause, exit_code, signal_name) == ("oom", 1, None)


def test_signal_exit_is_reported_as_signal_with_its_name() -> None:
    """systemd/POSIX report a signal death as ``128 + N``; 137 is SIGKILL."""
    cause, exit_code, signal_name = death_cause(_crash(exit_code=137))
    assert (cause, exit_code, signal_name) == ("signal", 137, "SIGKILL")


def test_plain_exit_is_reported_as_exit_code() -> None:
    assert death_cause(_crash(exit_code=2)) == ("exit_code", 2, None)


def test_unknown_exit_status_is_still_exit_code_and_never_invented() -> None:
    """No exit status known: ``exit_code`` with no code, never a guess."""
    assert death_cause(_crash(exit_code=None)) == ("exit_code", None, None)


def test_an_out_of_range_status_is_not_reported_as_a_signal() -> None:
    assert death_cause(_crash(exit_code=999)) == ("exit_code", 999, None)


def test_a_boolean_exit_code_is_not_mistaken_for_a_signal() -> None:
    """``bool`` is an ``int`` subclass — ``exit_code=True`` must not read as 129."""
    assert death_cause(_crash(exit_code=True)) == ("exit_code", True, None)


# ── the wire shape ──────────────────────────────────────────────────────


def test_a_fresh_daemon_reports_last_death_present_and_null(tmp_path: Path) -> None:
    """``null`` and *absent* are different statements; the key must exist."""
    health = _make_manager(tmp_path).health()
    assert "last_death" in health
    assert health["last_death"] is None
    assert health["backend_alive"] is False


def test_a_published_death_carries_every_documented_field(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    mgr._publish_last_death(_crash(exit_code=137, error_message="killed by signal\nmore"))

    death = mgr.health()["last_death"]
    assert death["cause"] == "signal"
    assert death["model"] == "minimal"
    assert death["timestamp"] == "2026-11-04T04:00:00+00:00"
    assert death["exit_code"] == 137
    assert death["signal"] == "SIGKILL"
    assert death["detail"] == "killed by signal"
    assert death["event_id"]


def test_event_id_is_stable_across_reads_and_distinct_per_death(tmp_path: Path) -> None:
    """Guardian dedupes on ``event_id``: stable per death, unique between them."""
    mgr = _make_manager(tmp_path)
    crash = _crash(exit_code=1, error_message="died")

    mgr._publish_last_death(crash)
    first = mgr.health()["last_death"]["event_id"]
    assert mgr.health()["last_death"]["event_id"] == first

    mgr._publish_last_death(crash)  # byte-identical death, seconds later
    assert mgr.health()["last_death"]["event_id"] != first


def test_backend_alive_is_true_while_a_model_is_loaded(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    mgr.current_model = "minimal"
    mgr.is_unloaded = False
    assert mgr.health()["backend_alive"] is True


# ── the two exclusions ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_death_during_a_served_ensure_does_not_move_last_death(
    tmp_path: Path,
) -> None:
    """``/ensure`` reports the failure to its caller; ``last_death`` stays put.

    ``_detect_crash`` is what both ensure paths call. It records a
    ``CrashRecord`` for the caller's ``crash_details`` and must not publish an
    unattended death — otherwise a failed load masquerades as a crash that
    happened to an idle backend.
    """
    mgr = _make_manager(tmp_path)
    mgr.server_process.crash_error = AsyncMock(return_value="model_load_failed")
    mgr.server_process.service_exit_code = AsyncMock(return_value=1)
    mgr.server_process.service_signal = AsyncMock(return_value=None)
    mgr.server_process.stop = AsyncMock(return_value=None)

    crash = await mgr._detect_crash("minimal")

    assert crash.model == "minimal"
    assert mgr.last_crash is crash
    assert mgr.health()["last_death"] is None


def test_an_unload_keeps_the_last_death_and_reports_not_alive(tmp_path: Path) -> None:
    """A deliberate stop is not a death and must not erase the real one."""
    mgr = _make_manager(tmp_path)
    mgr._publish_last_death(_crash(exit_code=137))
    published = mgr.health()["last_death"]

    mgr.is_unloaded = True
    health = mgr.health()

    assert health["is_unloaded"] is True
    assert health["backend_alive"] is False
    assert health["last_death"] == published


# ── the route ───────────────────────────────────────────────────────────


def test_status_route_exposes_the_contract(tmp_path: Path) -> None:
    """``GET /status`` is what Guardian polls; the fields must survive the route."""
    from caretaker import server
    from fastapi.testclient import TestClient

    mgr = _make_manager(tmp_path)
    mgr._publish_last_death(_crash(oom=True, exit_code=1, error_message="CUDA out of memory"))
    server.init(mgr)
    try:
        client = TestClient(server.app)
        headers = {"Authorization": f"Bearer {_test_key()}"}
        body = client.get("/status", headers=headers).json()
    finally:
        server.init(None)

    assert body["last_death"]["cause"] == "oom"
    assert body["last_death"]["model"] == "minimal"
    assert "backend_alive" in body


def _test_key() -> str:
    import os

    os.environ.setdefault("CARETAKER_KEY", "test-key")
    return os.environ["CARETAKER_KEY"]


# ── the systemd path (the one production actually uses) ─────────────────


def test_a_backend_reported_signal_wins_over_the_status_heuristic() -> None:
    """systemd puts the signal *number* in ``ExecMainStatus``: 9, not 137.

    Without the backend's own signal report, a SIGKILL would be published as
    ``cause: exit_code`` with ``exit_code: 9`` — an ordinary-looking non-zero
    exit, which is exactly the misreading this contract exists to prevent.
    """
    cause, exit_code, signal_name = death_cause(_crash(exit_code=9, signal="SIGKILL"))
    assert (cause, exit_code, signal_name) == ("signal", 9, "SIGKILL")


@pytest.mark.asyncio
async def test_systemd_signal_death_is_read_from_exec_main_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ExecMainCode`` distinguishes a signal death from an ordinary exit."""
    from caretaker.manager import SystemdServerProcess

    proc = SystemdServerProcess(service="llama-server")

    async def _show(*args, **kwargs):
        class _P:
            async def communicate(self):
                return (b"ExecMainCode=2\nExecMainStatus=9\n", b"")

        return _P()

    monkeypatch.setattr("asyncio.create_subprocess_exec", _show)
    assert await proc.service_signal() == "SIGKILL"


@pytest.mark.asyncio
async def test_an_ordinary_exit_is_not_reported_as_a_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ExecMainCode=1`` is a normal exit, whatever the status number is."""
    from caretaker.manager import SystemdServerProcess

    proc = SystemdServerProcess(service="llama-server")

    async def _show(*args, **kwargs):
        class _P:
            async def communicate(self):
                return (b"ExecMainCode=1\nExecMainStatus=9\n", b"")

        return _P()

    monkeypatch.setattr("asyncio.create_subprocess_exec", _show)
    assert await proc.service_signal() is None


@pytest.mark.asyncio
async def test_an_unreadable_systemd_state_is_not_a_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Introspection failure must degrade to "unknown", never to a guess."""
    from caretaker.manager import SystemdServerProcess

    proc = SystemdServerProcess(service="llama-server")

    async def _boom(*args, **kwargs):
        raise OSError("systemctl missing")

    monkeypatch.setattr("asyncio.create_subprocess_exec", _boom)
    assert await proc.service_signal() is None


# ── the production call site (the coverage that makes the rest meaningful) ──


@pytest.mark.asyncio
async def test_the_watchdog_tick_publishes_the_death(tmp_path: Path) -> None:
    """Drive the real ``_watchdog_tick``: the publication must come from there.

    Every other test here calls ``_publish_last_death`` directly, so deleting
    its only production call would leave them all green while unattended deaths
    became invisible again — the exact failure this PR exists to fix. This test
    fails if the watchdog stops publishing.
    """
    mgr = _make_manager(tmp_path)
    mgr.current_model = "minimal"
    mgr.is_unloaded = False

    mgr.server_process.health_ok = AsyncMock(return_value=False)  # backend is gone
    mgr.server_process.crash_error = AsyncMock(return_value="killed by signal")
    mgr.server_process.service_exit_code = AsyncMock(return_value=9)
    mgr.server_process.service_signal = AsyncMock(return_value="SIGKILL")
    mgr.server_process.stop = AsyncMock(return_value=None)

    async def _noop(*args, **kwargs):
        return None

    # Stop the tick before the restart leg: the death is published when the
    # crash is detected, which is what this test is about.
    mgr._switch_model_locked = _noop

    await mgr._watchdog_tick()

    health = mgr.health()
    assert health["last_death"] is not None, "the watchdog must publish unattended deaths"
    assert health["last_death"]["cause"] == "signal"
    assert health["last_death"]["signal"] == "SIGKILL"
    assert health["last_death"]["model"] == "minimal"
