"""Config hot-reload tests — mtime-based fail-open reload of the models file.

The caretaker historically loaded ``models.local.settings.yaml`` exactly once
(in ``Caretaker.__init__``); edits required a daemon restart. These tests pin
the ``_maybe_reload_config`` contract:

1. ``switch_model``/``health()`` pick up file changes without a restart
   (models edited, added, removed; aliases added; ``health_polls`` knob).
2. Unchanged files cost a single stat (no reload, no parse).
3. Fail-open: a broken edit warns once, keeps the previous in-memory config,
   and never breaks ``switch_model`` — a later valid edit still reloads.
4. ``health()`` is defensive when the loaded model disappears from the
   config (``needs_reload: true`` instead of a KeyError raise).
5. One route-level pin: ``POST /ensure`` hot-loads a model added to the file
   after a first 404, through ``caretaker.server.init`` injection.
6. Embedded-MTP args build: ``draft_cache_type_k``/``draft_cache_type_v``
   are honored (``--cache-type-k-draft``/``--cache-type-v-draft``) when
   explicitly configured and stay silent otherwise (dead-key fix).
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from caretaker import config as config_mod
from caretaker.manager import DEFAULT_HEALTH_POLLS
from caretaker.server import app, init

from test_phase_a import FakeServerProcess, _make_manager

AUTH = {"Authorization": "Bearer test-secret"}

MODEL_A = {"path": "/home/flip/models/minimal.gguf", "context": 4096}
MODEL_NEW = {"path": "/home/flip/models/newmodel.gguf", "context": 2048}


# --------------------------------------------------------------------------- helpers
def _set_models_file(
    tmp_path: Path,
    models: dict,
    aliases: dict | None = None,
    extra: dict | None = None,
) -> Path:
    """(Re)write the manager's models file — same fixed path ``_make_manager`` uses."""
    cfg = tmp_path / "models.local.settings.yaml"
    data: dict = {"models": models, "aliases": aliases or {}}
    if extra:
        data.update(extra)
    cfg.write_text(yaml.safe_dump(data), encoding="utf-8")
    return cfg


def _bump_mtime(cfg: Path, tick: list[int]) -> None:
    """Force a strictly increasing ``st_mtime_ns`` so the reload check fires.

    Real-time writes have sub-second mtime granularity surprises; an explicit
    ``os.utime`` with a monotonically bumped ns value makes the change
    deterministic regardless of filesystem timestamp resolution.
    """
    tick[0] += 10_000_000  # +10 ms
    os.utime(cfg, ns=(tick[0], tick[0]))


@pytest.fixture
def patch_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Mirror of ``test_phase_a.patch_paths``: per-test tmp deployment paths."""
    monkeypatch.setenv("CARETAKER_LLAMA_SLOTS_DIR", "/usr/local/share/caretaker/fixture_slots")
    monkeypatch.setenv("CARETAKER_SERVER_URL", "http://127.0.0.1:11440")
    import caretaker.manager as manager_mod

    monkeypatch.setattr(manager_mod, "CURRENT_MODEL_ARGS_FILE", tmp_path / "current_model.args")
    monkeypatch.setattr(manager_mod, "CURRENT_MODEL_ENV_FILE", tmp_path / "current_model.env")
    monkeypatch.setattr(manager_mod, "CURRENT_MODEL_SIG_FILE", tmp_path / "current_model.sig")


@pytest.fixture
def injection_reset(isolated_api_lifespan):
    yield
    init(None)


# --------------------------------------------------------- 1. ctx change pickup
async def test_switch_model_picks_up_ctx_change_without_restart(
    tmp_path: Path, patch_paths: None
) -> None:
    """Load ctx A → edit the file (same model, ctx B) + bump mtime → switch
    again → the written args file contains ctx B. No daemon restart involved."""
    proc = FakeServerProcess()
    mgr = _make_manager(tmp_path, process=proc, models={"minimal": dict(MODEL_A)})
    cfg = tmp_path / "models.local.settings.yaml"
    tick = [time.time_ns()]

    await mgr.switch_model("minimal")
    args_after_a = (tmp_path / "current_model.args").read_text()
    assert "-c 4096" in args_after_a

    _set_models_file(tmp_path, {"minimal": {"path": MODEL_A["path"], "context": 8192}})
    _bump_mtime(cfg, tick)
    await mgr.switch_model("minimal")

    args_after_b = (tmp_path / "current_model.args").read_text()
    assert "-c 8192" in args_after_b, args_after_b
    assert mgr.current_model == "minimal"
    # A real reload happened (drift against the new config), not a no-op.
    assert proc.events.count("start") == 2


# --------------------------------------------------------- 2. unchanged → no reload
async def test_unchanged_file_skips_reload(
    tmp_path: Path, patch_paths: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unchanged mtime/size → the cheap one-stat path; load_models_config is
    NOT called again (no reparse, no dict swap)."""
    mgr = _make_manager(tmp_path, process=FakeServerProcess(), models={"minimal": dict(MODEL_A)})

    calls = {"n": 0}
    orig_load = config_mod.load_models_config

    def counting_load(path=None):
        calls["n"] += 1
        return orig_load(path)

    monkeypatch.setattr(config_mod, "load_models_config", counting_load)

    await mgr.switch_model("minimal")
    assert calls["n"] == 0, "unchanged file: switch must take the one-stat cheap path"

    await mgr.switch_model("minimal")
    assert calls["n"] == 0, "unchanged file must not re-parse the models config"

    mgr.health()
    assert calls["n"] == 0, "health() on an unchanged file must not re-parse either"

    args = (tmp_path / "current_model.args").read_text()
    assert "-c 4096" in args


# --------------------------------------------------------- 3. broken YAML fail-open
async def test_broken_yaml_keeps_previous_config_and_warns_once(
    tmp_path: Path, patch_paths: None, caplog: pytest.LogCaptureFixture
) -> None:
    """Broken YAML on a changed file: switch still succeeds with the PREVIOUS
    config, a warning is logged, no exception; a later valid edit reloads."""
    proc = FakeServerProcess()
    mgr = _make_manager(tmp_path, process=proc, models={"minimal": dict(MODEL_A)})
    cfg = tmp_path / "models.local.settings.yaml"
    tick = [time.time_ns()]
    await mgr.switch_model("minimal")

    cfg.write_text("models: [oops\n  broken", encoding="utf-8")  # invalid YAML
    _bump_mtime(cfg, tick)

    with caplog.at_level(logging.WARNING, logger="caretaker.manager"):
        # No exception: the previous in-memory config keeps the daemon alive.
        await mgr.switch_model("minimal")

    assert mgr.current_model == "minimal"
    assert mgr.models["minimal"]["context"] == 4096, "broken edit must keep the old config"
    assert "-c 4096" in (tmp_path / "current_model.args").read_text()
    assert any(
        "hot-reload failed" in record.getMessage() for record in caplog.records
    ), [record.getMessage() for record in caplog.records]

    # Subsequent VALID edit reloads normally (the broken sig was stored once).
    _set_models_file(tmp_path, {"minimal": {"path": MODEL_A["path"], "context": 8192}})
    _bump_mtime(cfg, tick)
    await mgr.switch_model("minimal")
    assert mgr.models["minimal"]["context"] == 8192
    assert "-c 8192" in (tmp_path / "current_model.args").read_text()


# --------------------------------------------------------- 4. added model loadable
async def test_added_model_becomes_loadable_without_restart(
    tmp_path: Path, patch_paths: None
) -> None:
    proc = FakeServerProcess()
    mgr = _make_manager(tmp_path, process=proc, models={"minimal": dict(MODEL_A)})
    cfg = tmp_path / "models.local.settings.yaml"
    tick = [time.time_ns()]

    with pytest.raises(ValueError):
        await mgr.switch_model("newmodel")

    _set_models_file(tmp_path, {"minimal": dict(MODEL_A), "newmodel": dict(MODEL_NEW)})
    _bump_mtime(cfg, tick)
    await mgr.switch_model("newmodel")

    assert mgr.current_model == "newmodel"
    assert "-c 2048" in (tmp_path / "current_model.args").read_text()


# --------------------------------------------------------- 5. removed model → needs_reload
async def test_health_reports_needs_reload_when_loaded_model_removed(
    tmp_path: Path, patch_paths: None
) -> None:
    """A reload that removes the LOADED model must not make ``health()`` raise
    (defensive branch — ``_compute_launch_signature`` would KeyError);
    ``needs_reload: true`` is reported instead."""
    mgr = _make_manager(tmp_path, process=FakeServerProcess(), models={"minimal": dict(MODEL_A)})
    cfg = tmp_path / "models.local.settings.yaml"
    tick = [time.time_ns()]

    await mgr.switch_model("minimal")
    assert mgr.health()["needs_reload"] is False

    _set_models_file(tmp_path, {"other": {"path": "/home/flip/models/other.gguf"}})
    _bump_mtime(cfg, tick)

    health = mgr.health()  # must not raise
    assert health["loaded_model"] == "minimal"
    assert health["needs_reload"] is True


# --------------------------------------------------------- 6. added alias resolves
async def test_alias_added_by_file_change_resolves_in_switch(
    tmp_path: Path, patch_paths: None
) -> None:
    proc = FakeServerProcess()
    mgr = _make_manager(tmp_path, process=proc, models={"minimal": dict(MODEL_A)})
    cfg = tmp_path / "models.local.settings.yaml"
    tick = [time.time_ns()]

    with pytest.raises(ValueError):
        await mgr.switch_model("mini")

    _set_models_file(tmp_path, {"minimal": dict(MODEL_A)}, aliases={"mini": "minimal"})
    _bump_mtime(cfg, tick)
    await mgr.switch_model("mini")

    assert mgr.current_model == "minimal", "the reloaded alias map must resolve 'mini'"


# --------------------------------------------------------- 7. health_polls knob
async def test_health_polls_refreshed_from_file_on_reload(
    tmp_path: Path, patch_paths: None
) -> None:
    """Without an explicit constructor override, the file's ``health_polls``
    knob is applied on hot-reload (``health()`` is the lock-free call site)."""
    mgr = _make_manager(tmp_path, process=FakeServerProcess(), models={"minimal": dict(MODEL_A)})
    cfg = tmp_path / "models.local.settings.yaml"
    tick = [time.time_ns()]
    assert mgr.health_polls == DEFAULT_HEALTH_POLLS

    _set_models_file(tmp_path, {"minimal": dict(MODEL_A)}, extra={"health_polls": 777})
    _bump_mtime(cfg, tick)
    mgr.health()

    assert mgr.health_polls == 777


async def test_explicit_health_polls_override_wins_over_file_reload(
    tmp_path: Path, patch_paths: None
) -> None:
    """An explicit constructor ``health_polls`` wins over the models-file knob,
    also after a hot reload."""
    mgr = _make_manager(
        tmp_path, process=FakeServerProcess(), models={"minimal": dict(MODEL_A)}, health_polls=99
    )
    cfg = tmp_path / "models.local.settings.yaml"
    tick = [time.time_ns()]
    assert mgr.health_polls == 99

    _set_models_file(tmp_path, {"minimal": dict(MODEL_A)}, extra={"health_polls": 777})
    _bump_mtime(cfg, tick)
    mgr.health()

    assert mgr.health_polls == 99


# --------------------------------------------------------- 10. route-level ensure pin
def test_ensure_hot_loads_model_added_after_first_404(
    tmp_path: Path,
    patch_paths: None,
    injection_reset: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hot reload through the control API: ``POST /ensure`` for an unknown
    model → 404 ``model_not_found``; after the model is ADDED to the file,
    a second ``POST /ensure`` returns 200 — no daemon restart."""
    monkeypatch.setenv("CARETAKER_KEY", "test-secret")
    proc = FakeServerProcess()
    mgr = _make_manager(tmp_path, process=proc, models={"other": {"path": "/home/flip/models/other.gguf"}})
    cfg = tmp_path / "models.local.settings.yaml"
    tick = [time.time_ns()]
    init(mgr)
    client = TestClient(app)

    r1 = client.post("/ensure", json={"model": "minimal"}, headers=AUTH)
    assert r1.status_code == 404, r1.text
    assert r1.json()["error"] == "model_not_found"

    _set_models_file(
        tmp_path,
        {"other": {"path": "/home/flip/models/other.gguf"}, "minimal": dict(MODEL_A)},
    )
    _bump_mtime(cfg, tick)

    r2 = client.post("/ensure", json={"model": "minimal"}, headers=AUTH)
    assert r2.status_code == 200, r2.text
    assert r2.json()["ok"] is True
    assert r2.json()["loaded_model"] == "minimal"
    assert proc.events.count("start") >= 1


# --------------------------------------------------- 8./9. embedded-MTP draft cache
MTP_PATH = "/home/flip/models/mtp-embedded.gguf"
MTP_SLOTS = "/usr/local/share/caretaker/fixture_slots"


def test_embedded_mtp_explicit_draft_cache_keys_emitted(
    tmp_path: Path, patch_paths: None
) -> None:
    """Embedded-MTP + explicit ``draft_cache_type_k/v`` → the draft-KV flags
    are emitted (previously dead letter on this branch, 2026-10-25 finding)."""
    mgr = _make_manager(
        tmp_path,
        process=FakeServerProcess(),
        models={
            "mtp": {
                "path": MTP_PATH,
                "context": 8192,
                "ngl": 99,
                "spec_type": "draft-mtp",
                "draft_cache_type_k": "turbo4",
                "draft_cache_type_v": "turbo4",
            }
        },
    )
    args_str, _ = mgr._build_args_string(mgr.build_runtime_config("mtp"))
    assert "--cache-type-k-draft turbo4" in args_str
    assert "--cache-type-v-draft turbo4" in args_str


def test_embedded_mtp_without_draft_cache_keys_stays_silent(
    tmp_path: Path, patch_paths: None
) -> None:
    """Embedded-MTP without the draft cache keys → NO draft-cache flags, args
    byte-identical to the pre-fix behavior (f16 default stays silent)."""
    mgr = _make_manager(
        tmp_path,
        process=FakeServerProcess(),
        models={"mtp": {"path": MTP_PATH, "context": 8192, "ngl": 99, "spec_type": "draft-mtp"}},
    )
    args_str, _ = mgr._build_args_string(mgr.build_runtime_config("mtp"))

    # Exact substring absence (the pin, word-level):
    assert "--cache-type-k-draft" not in args_str
    assert "--cache-type-v-draft" not in args_str
    # Byte-identical golden: only --spec-type, nothing else spec-related.
    golden = (
        f"-m {MTP_PATH} -c 8192 -ngl 99 -ctk q4_0 -ctv q4_0 "
        f"--host 127.0.0.1 --port 11440 --slot-save-path {MTP_SLOTS} --load-mode none "
        "--spec-type draft-mtp"
    )
    assert args_str == golden
