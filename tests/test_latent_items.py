"""Latent-item fixes (caretaker AGENTS.md list, 2026-09-02):

1. Alias resolution in ``switch_model`` — the host config's ``aliases`` map was
   loaded by the config layer and then dropped, so a /ensure with an alias
   name 404'd even though that name is operator-configured. Aliases now
   resolve at the switch entry (bounded walk). The gateway stays the alias
   authority for its own registry (F4 contract); double resolution is
   harmless because canonical names are not alias keys.
2. Failed-switch bookkeeping truthfulness — a failure after the backend stop
   left ``_loaded_at`` pointing at the PREVIOUS model while ``current_model``
   was already ``None`` (health() reported a phantom load-age), and could
   leave ``_skip_next_context_save`` armed (a phantom save-skip on the NEXT
   switch). Both are cleared on the failure path now.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from caretaker.manager import Caretaker
from caretaker.server import app, init
from fastapi.testclient import TestClient

from test_ensure_verification import AUTH_HEADER
from test_phase_a import FakeServerProcess, _stub_props_ok

MINIMAL = "/home/flip/models/minimal.gguf"
OTHER = "/home/flip/models/other.gguf"


def _make_manager_with_aliases(
    tmp_path: Path,
    models: dict[str, dict],
    aliases: dict[str, str] | None = None,
    process: FakeServerProcess | None = None,
    **kwargs,
) -> Caretaker:
    """Build a Caretaker whose host config carries BOTH models and aliases."""
    cfg = tmp_path / "models.local.settings.yaml"
    cfg.write_text(
        yaml.safe_dump({"models": models, "aliases": aliases}),
        encoding="utf-8",
    )
    mgr = Caretaker(
        config_path=str(cfg),
        server_process=process or FakeServerProcess(),
        **kwargs,
    )
    _stub_props_ok(mgr)
    return mgr


def _client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def injection_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CARETAKER_KEY", "test-secret")
    yield
    init(None)


@pytest.fixture
def isolated_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Isolate the manager's deployment files + env to a per-test tmp dir."""
    monkeypatch.setenv("CARETAKER_LLAMA_SLOTS_DIR", "/usr/local/share/caretaker/fixture_slots")
    monkeypatch.setenv("CARETAKER_SERVER_URL", "http://127.0.0.1:11440")
    import caretaker.manager as manager_mod

    monkeypatch.setattr(manager_mod, "CURRENT_MODEL_ARGS_FILE", tmp_path / "current_model.args")
    monkeypatch.setattr(manager_mod, "CURRENT_MODEL_ENV_FILE", tmp_path / "current_model.env")
    monkeypatch.setattr(manager_mod, "CURRENT_MODEL_SIG_FILE", tmp_path / "current_model.sig")


# ------------------------------------------------------------ (1) alias routing


def test_ensure_via_alias_loads_canonical_model(
    tmp_path: Path, isolated_paths: None, injection_reset: None
) -> None:
    """/ensure with an alias name resolves and loads the canonical model."""
    mgr = _make_manager_with_aliases(
        tmp_path,
        models={"minimal": {"path": MINIMAL}},
        aliases={"mini": "minimal"},
    )
    init(mgr)
    with _client() as client:
        resp = client.post("/ensure", json={"model": "mini"}, headers=AUTH_HEADER)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["loaded_model"] == "minimal", "the CANONICAL name must be reported"
    assert body["fresh_load"] is True


def test_alias_chain_resolves_bounded(
    tmp_path: Path, isolated_paths: None, injection_reset: None
) -> None:
    """alias→alias→canonical chains resolve through the bounded walk."""
    mgr = _make_manager_with_aliases(
        tmp_path,
        models={"minimal": {"path": MINIMAL}},
        aliases={"a1": "minimal", "a0": "a1"},
    )
    init(mgr)
    with _client() as client:
        resp = client.post("/ensure", json={"model": "a0"}, headers=AUTH_HEADER)

    assert resp.status_code == 200, resp.text
    assert resp.json()["loaded_model"] == "minimal"


def test_alias_cycle_404s_without_hang(
    tmp_path: Path, isolated_paths: None, injection_reset: None
) -> None:
    """A cyclic alias map (x→y→x) must 404 promptly, never loop forever."""
    mgr = _make_manager_with_aliases(
        tmp_path,
        models={"minimal": {"path": MINIMAL}},
        aliases={"x": "y", "y": "x"},
    )
    init(mgr)
    with _client() as client:
        resp = client.post("/ensure", json={"model": "x"}, headers=AUTH_HEADER)

    assert resp.status_code == 404
    assert resp.json()["error"] == "model_not_found"


def test_unknown_model_still_404_with_aliases_present(
    tmp_path: Path, isolated_paths: None, injection_reset: None
) -> None:
    """The unknown-model contract is unchanged when an alias map exists."""
    mgr = _make_manager_with_aliases(
        tmp_path,
        models={"minimal": {"path": MINIMAL}},
        aliases={"mini": "minimal"},
    )
    init(mgr)
    with _client() as client:
        resp = client.post("/ensure", json={"model": "nope"}, headers=AUTH_HEADER)

    assert resp.status_code == 404
    assert resp.json()["error"] == "model_not_found"


# ------------------------------------- (2) failed-switch bookkeeping truthfulness


def test_failed_switch_clears_loaded_at_and_skip_flag(
    tmp_path: Path, isolated_paths: None, injection_reset: None
) -> None:
    """A switch failing on health must leave NO stale bookkeeping behind.

    After the backend stop, current_model is already None; a failure there
    used to keep _loaded_at from the PREVIOUS model (health() reported a
    phantom load-age) and could keep _skip_next_context_save armed (phantom
    save-skip on the next switch). Both must be cleared.
    """
    proc = FakeServerProcess()
    mgr = _make_manager_with_aliases(
        tmp_path,
        models={"minimal": {"path": MINIMAL}, "other": {"path": OTHER}},
        aliases={},
        health_polls=3,
        health_interval=0.0,
        process=proc,
    )
    init(mgr)
    with _client() as client:
        r1 = client.post("/ensure", json={"model": "minimal"}, headers=AUTH_HEADER)
        assert r1.status_code == 200, r1.text
        assert mgr._loaded_at is not None, "a successful load sets _loaded_at"

        # The next load will never become healthy: _wait_for_health fails and
        # switch_model raises ModelLoadError (crash-recorded) → 503.
        proc._health_ok = False
        # Simulate a stale one-shot flag armed before this failing switch.
        mgr._skip_next_context_save = True

        r2 = client.post("/ensure", json={"model": "other"}, headers=AUTH_HEADER)

    assert r2.status_code == 503, r2.text
    assert r2.json()["error"] == "model_load_failed"
    assert mgr.current_model is None
    assert mgr._loaded_at is None, "no phantom load-age after a failed switch"
    assert mgr._skip_next_context_save is False, "one-shot flag must not survive a failure"
    assert mgr.health()["loaded_at"] is None


def test_stale_loaded_at_cleared_even_without_vram(
    tmp_path: Path, isolated_paths: None, injection_reset: None
) -> None:
    """The _loaded_at reset must not depend on the VRAM scheduler being active."""
    proc = FakeServerProcess()
    mgr = _make_manager_with_aliases(
        tmp_path,
        models={"minimal": {"path": MINIMAL}, "other": {"path": OTHER}},
        aliases={},
        health_polls=3,
        health_interval=0.0,
        process=proc,
        check_vram=False,
    )
    init(mgr)
    with _client() as client:
        r1 = client.post("/ensure", json={"model": "minimal"}, headers=AUTH_HEADER)
        assert r1.status_code == 200, r1.text

        proc._health_ok = False
        r2 = client.post("/ensure", json={"model": "other"}, headers=AUTH_HEADER)

    assert r2.status_code == 503, r2.text
    assert mgr.current_model is None
    assert mgr._loaded_at is None
