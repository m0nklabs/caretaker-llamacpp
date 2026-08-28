"""Bootstrap tests for the caretaker skeleton (PLAN.md §1).

Covers the auth gate, the config file loader, and route registration. No
lifecycle logic is exercised here — the handlers are stubs returning 501.
"""

from __future__ import annotations

import pytest
from caretaker import __main__ as entrypoint
from caretaker import config as config_mod
from caretaker.config import ModelsConfigError
from caretaker.server import app
from fastapi.testclient import TestClient

client = TestClient(app)


def _unset_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure CARETAKER_KEY is not set during the test."""
    monkeypatch.delenv("CARETAKER_KEY", raising=False)


def test_auth_gate_no_key_returns_503(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without CARETAKER_KEY env, control calls are refused with 503."""
    _unset_key(monkeypatch)
    assert client.get("/status").status_code == 503


def test_auth_gate_wrong_key_returns_401(monkeypatch: pytest.MonkeyPatch) -> None:
    """A wrong bearer key is rejected with 401."""
    monkeypatch.setenv("CARETAKER_KEY", "super-secret")
    resp = client.get("/status", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401


def test_auth_gate_non_ascii_configured_key_returns_401_not_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-ASCII CARETAKER_KEY must not crash the gate (compare_digest
    rejects non-ASCII str; the key is compared as UTF-8 bytes). HTTP headers
    are ASCII-only, so a non-ASCII client token cannot even be sent."""
    monkeypatch.setenv("CARETAKER_KEY", "süper-çrète")
    resp = client.get("/status", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401


def test_auth_gate_correct_key_reaches_route_501(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A correct bearer key passes the gate and reaches the 501 stub route."""
    monkeypatch.setenv("CARETAKER_KEY", "super-secret")
    resp = client.get("/status", headers={"Authorization": "Bearer super-secret"})
    assert resp.status_code == 501


def test_config_missing_file_raises_clear_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
) -> None:
    """A missing models file raises a clear ModelsConfigError."""
    missing = tmp_path / "does-not-exist.settings.yaml"
    monkeypatch.setenv("CARETAKER_MODELS_FILE", str(missing))
    with pytest.raises(ModelsConfigError) as excinfo:
        config_mod.load_models_config()
    assert "not found" in str(excinfo.value)


def test_config_non_mapping_yaml_raises_clear_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
) -> None:
    """A parseable-but-non-mapping YAML file raises a clear ModelsConfigError."""
    cfg_file = tmp_path / "bad.settings.yaml"
    cfg_file.write_text("- just\n- a\n- list\n", encoding="utf-8")
    monkeypatch.setenv("CARETAKER_MODELS_FILE", str(cfg_file))
    with pytest.raises(ModelsConfigError) as excinfo:
        config_mod.load_models_config()
    assert "must be a YAML mapping" in str(excinfo.value)


def test_config_invalid_utf8_raises_clear_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
) -> None:
    """Invalid UTF-8 bytes in the models file raise a clear ModelsConfigError."""
    cfg_file = tmp_path / "bad-utf8.settings.yaml"
    cfg_file.write_bytes(b"models:\n  name: \xff\xfe broken\n")
    monkeypatch.setenv("CARETAKER_MODELS_FILE", str(cfg_file))
    with pytest.raises(ModelsConfigError) as excinfo:
        config_mod.load_models_config()
    assert "failed to read/parse" in str(excinfo.value)


def test_entrypoint_defaults_to_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind defaults to loopback:11441; CARETAKER_HOST/PORT override (F5/F6
    remote gateways reach the caretaker on its LAN interface)."""
    monkeypatch.delenv("CARETAKER_HOST", raising=False)
    monkeypatch.delenv("CARETAKER_PORT", raising=False)
    assert entrypoint.resolve_bind_host() == "127.0.0.1"
    assert entrypoint.resolve_bind_port() == 11441

    monkeypatch.setenv("CARETAKER_HOST", "192.168.1.99")
    monkeypatch.setenv("CARETAKER_PORT", "12441")
    assert entrypoint.resolve_bind_host() == "192.168.1.99"
    assert entrypoint.resolve_bind_port() == 12441

    monkeypatch.setenv("CARETAKER_PORT", "not-a-number")
    assert entrypoint.resolve_bind_port() == 11441



def test_app_routes_registered() -> None:
    """The three control endpoints are registered on the app."""
    paths = {route.path for route in app.routes if hasattr(route, "path")}
    assert {"/status", "/ensure", "/unload"} <= paths

