"""Strict /ensure model verification — 2026-09-01 false-positive incident.

Incident: the (then-buggy) launcher relaunched the OLD model after the
caretaker's stop/start, the manager's warn-only verification logged the
mismatch but did not fail, and a subsequent ``POST /ensure`` returned HTTP 200
("caretaker confirmed model loaded") while llama-server still served the
previous model. These tests pin the repaired contract:

(a) the ``/ensure`` success path actually performs the ``GET /props``
    verification — on the fresh (re)load path AND on the no-op fast-path;
(b) a /props mismatch right after a (re)start retries exactly ONCE (one more
    stop/start cycle + wait + verify) and then returns ``503`` with
    ``{"error": "model_mismatch", "expected", "actual", "model"}`` — never
    success; a retry that verifies OK returns 200;
(c) the no-switch-needed ("already active") path also verifies, refuses the
    no-op on mismatch, heals via a real (re)load, and returns ``503
    model_mismatch`` when the backend keeps serving the wrong model.

HTTP to llama-server is mocked at the ``_fetch_props`` boundary (the
manager's single short-timeout GET /props call), matching the repo's
fake-ServerProcess style: no real backend is ever touched.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

# Ensure tests/ is importable even when not running under pytest's prepend mode.
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from caretaker.manager import Caretaker, ModelLoadError, ModelMismatchError
from caretaker.server import app, init

from test_phase_a import FakeServerProcess, _make_manager

AUTH_HEADER = {"Authorization": "Bearer test-secret"}

# The configured path of the default fixture model ("minimal").
EXPECTED = "/home/flip/models/minimal.gguf"
# What the incident's launcher actually served instead of the requested model.
WRONG = "/home/flip/models/gemma-4-E4B-it-uncensored-Q4_K_M.gguf"


@pytest.fixture
def isolated_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Isolate the manager's deployment files + env to a per-test tmp dir."""
    monkeypatch.setenv("CARETAKER_LLAMA_SLOTS_DIR", "/usr/local/share/caretaker/fixture_slots")
    monkeypatch.setenv("CARETAKER_SERVER_URL", "http://127.0.0.1:11440")
    import caretaker.manager as manager_mod

    monkeypatch.setattr(manager_mod, "CURRENT_MODEL_ARGS_FILE", tmp_path / "current_model.args")
    monkeypatch.setattr(manager_mod, "CURRENT_MODEL_ENV_FILE", tmp_path / "current_model.env")
    monkeypatch.setattr(manager_mod, "CURRENT_MODEL_SIG_FILE", tmp_path / "current_model.sig")


@pytest.fixture
def injection_reset(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Configure the control key + reset the server singleton after each API test."""
    monkeypatch.setenv("CARETAKER_KEY", "test-secret")
    yield
    init(None)


def _client() -> TestClient:
    return TestClient(app)


def _install_props(mgr: Caretaker, responses: list[dict | None]) -> None:
    """Script successive ``_fetch_props`` responses; the last one repeats forever.

    Counts every /props call on ``mgr.props_calls`` so tests can assert the
    verification actually ran (and how often).
    """
    mgr.props_calls = 0

    async def _fake_props() -> dict | None:
        mgr.props_calls += 1  # type: ignore[attr-defined]
        if len(responses) > 1:
            return responses.pop(0)
        return responses[0]

    mgr._fetch_props = _fake_props  # type: ignore[method-assign]


# ----------------------------------------------------------------- (a) success


def test_api_ensure_success_performs_props_verification(
    tmp_path: Path, isolated_paths: None, injection_reset: None
) -> None:
    """A 200 from /ensure must be backed by an actual GET /props verification."""
    proc = FakeServerProcess()
    mgr = _make_manager(tmp_path, process=proc)
    _install_props(mgr, [{"model_path": EXPECTED}])
    init(mgr)
    with _client() as client:
        resp = client.post("/ensure", json={"model": "minimal"}, headers=AUTH_HEADER)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["loaded_model"] == "minimal"
    assert body["fresh_load"] is True
    # Backward-compatible success shape: legacy + contract fields all intact.
    assert set(body) >= {"ok", "loaded_model", "fresh_load", "vision_enabled", "needs_reload"}
    assert mgr.props_calls >= 1, "/ensure success must actually query GET /props"


def test_api_ensure_noop_path_also_verifies_props(
    tmp_path: Path, isolated_paths: None, injection_reset: None
) -> None:
    """The "already active, no switch needed" fast-path verifies /props too."""
    proc = FakeServerProcess()
    mgr = _make_manager(tmp_path, process=proc)
    _install_props(mgr, [{"model_path": EXPECTED}])
    init(mgr)
    with _client() as client:
        client.post("/ensure", json={"model": "minimal"}, headers=AUTH_HEADER)
        starts_after_cold = proc.events.count("start")
        calls_after_cold = mgr.props_calls

        r2 = client.post("/ensure", json={"model": "minimal"}, headers=AUTH_HEADER)

    assert r2.status_code == 200, r2.text
    assert r2.json()["fresh_load"] is False  # no-op fast-path taken
    assert proc.events.count("start") == starts_after_cold  # no restart on the no-op
    assert mgr.props_calls > calls_after_cold, "the no-switch path must verify /props too"


# ------------------------------------------- (b) mismatch after a (re)start


def test_api_ensure_mismatch_after_restart_retries_once_then_503(
    tmp_path: Path, isolated_paths: None, injection_reset: None
) -> None:
    """Stubborn wrong model after a restart → exactly one retry → 503 model_mismatch."""
    proc = FakeServerProcess()
    mgr = _make_manager(tmp_path, process=proc)
    _install_props(mgr, [{"model_path": WRONG}])  # backend keeps serving the old model
    init(mgr)
    with _client() as client:
        resp = client.post("/ensure", json={"model": "minimal"}, headers=AUTH_HEADER)

    assert resp.status_code == 503, resp.text
    body = resp.json()
    assert body["error"] == "model_mismatch"
    assert body["expected"] == EXPECTED
    assert body["actual"] == WRONG
    assert body["model"] == "minimal"
    # Bounded: the initial load plus exactly ONE retry (a stop/start cycle each) —
    # never an unbounded loop, never a success.
    assert proc.events.count("start") == 2
    assert proc.events.count("stop") == 2
    assert mgr.props_calls == 2  # initial verification + the single retry's verification


def test_api_ensure_mismatch_recovers_on_single_retry(
    tmp_path: Path, isolated_paths: None, injection_reset: None
) -> None:
    """First /props shows the old model, the retried start serves the right one → 200."""
    proc = FakeServerProcess()
    mgr = _make_manager(tmp_path, process=proc)
    _install_props(mgr, [{"model_path": WRONG}, {"model_path": EXPECTED}])
    init(mgr)
    with _client() as client:
        resp = client.post("/ensure", json={"model": "minimal"}, headers=AUTH_HEADER)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["loaded_model"] == "minimal"
    assert body["fresh_load"] is True
    assert proc.events.count("start") == 2  # initial load + exactly one retry
    assert mgr.props_calls == 2


def test_api_ensure_unqueryable_props_returns_503_model_mismatch(
    tmp_path: Path, isolated_paths: None, injection_reset: None
) -> None:
    """Verification must be proven, never assumed: unreachable /props → 503, not 200."""
    proc = FakeServerProcess()
    mgr = _make_manager(tmp_path, process=proc)
    _install_props(mgr, [None])  # /props unreachable / non-200 / unparseable
    init(mgr)
    with _client() as client:
        resp = client.post("/ensure", json={"model": "minimal"}, headers=AUTH_HEADER)

    assert resp.status_code == 503, resp.text
    body = resp.json()
    assert body["error"] == "model_mismatch"
    assert body["expected"] == EXPECTED
    assert body["actual"] is None
    assert body["model"] == "minimal"


async def test_switch_model_mismatch_clears_active_state(
    tmp_path: Path, isolated_paths: None
) -> None:
    """After the final verification failure nothing is marked active (truthful state)."""
    proc = FakeServerProcess()
    mgr = _make_manager(tmp_path, process=proc, health_polls=3, health_interval=0.0)
    _install_props(mgr, [{"model_path": WRONG}])
    with pytest.raises(ModelMismatchError) as excinfo:
        await mgr.switch_model("minimal")

    assert excinfo.value.expected == EXPECTED
    assert excinfo.value.actual == WRONG
    assert excinfo.value.model == "minimal"
    assert mgr.current_model is None
    assert mgr.current_vision_enabled is False
    assert proc.events.count("start") == 2  # initial + exactly one bounded retry


async def test_switch_model_retry_health_failure_is_model_load_error(
    tmp_path: Path, isolated_paths: None
) -> None:
    """A backend that dies on the retried start is a load failure, not a mismatch."""
    proc = FakeServerProcess(health_ok=True)
    mgr = _make_manager(tmp_path, process=proc, health_polls=3, health_interval=0.0)
    _install_props(mgr, [{"model_path": WRONG}])

    started: list[int] = []

    async def _start() -> None:
        await FakeServerProcess.start(proc)
        started.append(1)

    async def _health_ok(url: str = "") -> bool:
        # Healthy after the first start only: the retried start never comes up.
        return len(started) <= 1

    proc.start = _start  # type: ignore[method-assign]
    proc.health_ok = _health_ok  # type: ignore[method-assign]

    with pytest.raises(ModelLoadError):
        await mgr.switch_model("minimal")

    assert mgr.current_model is None
    assert mgr.crash_history, "the retried load failure must be crash-recorded"
    assert proc.events.count("start") == 2  # initial + the single retry


# --------------------------------------- (c) no-switch path refuses + heals


def test_api_ensure_noop_mismatch_returns_503_instead_of_200(
    tmp_path: Path, isolated_paths: None, injection_reset: None
) -> None:
    """The incident's exact false positive: bookkeeping says 'already active',
    /props shows the old model → the no-op is refused, a heal reload runs with
    its single bounded retry, and the response is 503 model_mismatch — never 200.
    """
    proc = FakeServerProcess()
    mgr = _make_manager(tmp_path, process=proc)
    _install_props(
        mgr,
        [
            {"model_path": EXPECTED},  # cold load verifies fine
            {"model_path": WRONG},  # backend now serves the old model (launcher race)
        ],
    )
    init(mgr)
    with _client() as client:
        r1 = client.post("/ensure", json={"model": "minimal"}, headers=AUTH_HEADER)
        assert r1.status_code == 200, r1.text
        starts_after_cold = proc.events.count("start")

        r2 = client.post("/ensure", json={"model": "minimal"}, headers=AUTH_HEADER)

    assert r2.status_code == 503, r2.text
    body = r2.json()
    assert body["error"] == "model_mismatch"
    assert body["expected"] == EXPECTED
    assert body["actual"] == WRONG
    assert body["model"] == "minimal"
    # The no-op was refused: a real heal (re)load + its one bounded retry ran.
    assert proc.events.count("start") == starts_after_cold + 2
    # Truthful state afterwards: /status no longer claims the wrong model is loaded.
    status = client.get("/status", headers=AUTH_HEADER).json()
    assert status["loaded_model"] is None
    assert status["needs_reload"] is True


def test_api_ensure_noop_mismatch_heals_via_reload(
    tmp_path: Path, isolated_paths: None, injection_reset: None
) -> None:
    """A transient launcher race heals on the next /ensure: no-op refused, the
    real reload serves the right model, verified 200 with fresh_load=True."""
    proc = FakeServerProcess()
    mgr = _make_manager(tmp_path, process=proc)
    _install_props(
        mgr,
        [
            {"model_path": EXPECTED},  # cold load verified
            {"model_path": WRONG},  # no-op verification sees the wrong model…
            {"model_path": EXPECTED},  # …the heal reload serves the right one
        ],
    )
    init(mgr)
    with _client() as client:
        client.post("/ensure", json={"model": "minimal"}, headers=AUTH_HEADER)
        starts_after_cold = proc.events.count("start")

        r2 = client.post("/ensure", json={"model": "minimal"}, headers=AUTH_HEADER)

    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["ok"] is True
    assert body["fresh_load"] is True  # the healing reload actually restarted
    assert body["loaded_model"] == "minimal"
    assert proc.events.count("start") == starts_after_cold + 1


# ------------------------------------------- unknown model stays a 4xx error


def test_api_ensure_unknown_model_404_without_props_call(
    tmp_path: Path, isolated_paths: None, injection_reset: None
) -> None:
    """Unknown model → 404 model_not_found (preserved), before any verification."""
    proc = FakeServerProcess()
    mgr = _make_manager(tmp_path, process=proc)
    _install_props(mgr, [{"model_path": EXPECTED}])
    init(mgr)
    with _client() as client:
        resp = client.post("/ensure", json={"model": "nope"}, headers=AUTH_HEADER)

    assert resp.status_code == 404, resp.text
    assert resp.json()["error"] == "model_not_found"
    assert mgr.props_calls == 0  # validation happens before any backend probing


# ----------------------------------------------- (review r1) path normalization


def test_verification_accepts_symlink_resolved_props_path(
    tmp_path: Path, isolated_paths: None, injection_reset: None
) -> None:
    """A /props path that resolves to the same file must verify OK (no 503).

    Regression pin for the PR-Piet review finding (2026-09-02, PR #9): llama-server
    may echo the launched path in resolved form while the configured path is a
    symlink/relative variant. Strict string equality would report a correctly
    loaded model as model_mismatch and burn a pointless stop/start retry.
    """
    proc = FakeServerProcess()
    mgr = _make_manager(tmp_path, process=proc)
    # Real model file + a symlink used as the CONFIGURED path.
    real_file = tmp_path / "real-model.gguf"
    real_file.write_bytes(b"gguf")
    symlink = tmp_path / "link-to-model.gguf"
    symlink.symlink_to(real_file)
    mgr.models["minimal"]["path"] = str(symlink)
    # Backend echoes the RESOLVED path (what llama-server would report).
    _install_props(mgr, [{"model_path": str(real_file)}])
    init(mgr)
    with _client() as client:
        resp = client.post("/ensure", json={"model": "minimal"}, headers=AUTH_HEADER)

    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True


def test_verification_realpath_does_not_mask_true_mismatch(
    tmp_path: Path, isolated_paths: None, injection_reset: None
) -> None:
    """A genuinely different file is still a model_mismatch after realpath."""
    proc = FakeServerProcess()
    mgr = _make_manager(tmp_path, process=proc)
    other_file = tmp_path / "other-model.gguf"
    other_file.write_bytes(b"gguf")
    _install_props(mgr, [{"model_path": str(other_file)}])
    init(mgr)
    with _client() as client:
        resp = client.post("/ensure", json={"model": "minimal"}, headers=AUTH_HEADER)

    assert resp.status_code == 503
    body = resp.json()
    assert body["error"] == "model_mismatch"


def test_model_paths_diverge_helper_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Direct contract of the path-normalization helper."""
    from caretaker.manager import Caretaker as _C

    real = tmp_path / "m.gguf"
    real.write_bytes(b"x")
    link = tmp_path / "l.gguf"
    link.symlink_to(real)
    # Same file via symlink / relative segment / ~-form: NOT divergent.
    assert _C._model_paths_diverge(str(real), str(link)) is False
    assert _C._model_paths_diverge(str(tmp_path / "sub" / ".." / "m.gguf"), str(real)) is False
    monkeypatch.setenv("HOME", str(tmp_path))
    assert _C._model_paths_diverge("~/m.gguf", str(real)) is False
    # A different file IS divergent, in both string-equal-looking forms.
    other = tmp_path / "other.gguf"
    other.write_bytes(b"y")
    assert _C._model_paths_diverge(str(other), str(real)) is True
