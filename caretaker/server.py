"""Control API for caretaker.

FastAPI app exposing the thin control contract to the gateway:
``GET /status``, ``POST /ensure {model}``, ``POST /unload``. Every control call
requires ``Authorization: Bearer ${CARETAKER_KEY}`` read from the environment
(never committed).

Error responses carry machine-readable bodies at the top level (no ``detail``
wrapper), e.g. ``{"error": "model_not_found", "message": "..."}``, so the
gateway's repair logic can branch on ``error`` directly.
"""

from __future__ import annotations

import hmac
import os
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse

from .manager import Caretaker, ModelLoadError

CARETAKER_KEY_ENV = "CARETAKER_KEY"

app = FastAPI(title="caretaker", version="0.1.0")

# Lazily-built manager singleton. Tests inject a manager (e.g. one backed by a
# fake ServerProcess) via :func:`init` so route tests never build a real one.
_manager_instance: Caretaker | None = None


def _configured_key() -> str | None:
    """Return the configured control key, or None if not set."""
    key = os.environ.get(CARETAKER_KEY_ENV)
    return key if key else None


async def require_caretaker_key(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Auth gate: enforce the single per-caretaker Bearer key.

    - No ``CARETAKER_KEY`` env var set → ``503`` "caretaker key not configured".
    - Non-ASCII ``CARETAKER_KEY`` → ``503`` (HTTP headers are latin-1 on the
      wire; a non-ASCII key can never be transmitted and would permanently
      lock the control API in a confusing 401 loop).
    - Wrong/missing key → ``401``.
    - Correct key → pass through.
    """
    expected = _configured_key()
    if expected is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="caretaker key not configured",
        )
    if not expected.isascii():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="CARETAKER_KEY must be ASCII: non-ASCII keys can never be "
            "transmitted by HTTP clients and would lock the control API",
        )
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:]
    if token is None or not hmac.compare_digest(
        token.encode("utf-8"), expected.encode("utf-8")
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid caretaker key",
        )


def init(manager: Caretaker | None = None) -> None:
    """Set the manager singleton used by the routes.

    Tests inject a ``Caretaker`` backed by a fake ``ServerProcess`` via this
    hook so route tests never build (or touch) a real backend. Passing ``None``
    resets the singleton so the next request lazily rebuilds it.
    """
    global _manager_instance
    _manager_instance = manager


def _manager() -> Caretaker:
    """Return the manager singleton, lazily building a real one if not set."""
    global _manager_instance
    if _manager_instance is None:
        _manager_instance = Caretaker()
    return _manager_instance


def _invalid_request(message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={"error": "invalid_request", "message": message},
    )


@app.get("/status", dependencies=[Depends(require_caretaker_key)])
async def get_status() -> dict:
    """Report loaded model + drift/"needs reload" status for discovery."""
    return _manager().health()


@app.post("/ensure", dependencies=[Depends(require_caretaker_key)])
async def ensure(request: Request) -> dict:
    """Load/swap a model idempotently: any drift is repaired via switch_model."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _invalid_request("request body must be valid JSON")
    if not isinstance(body, dict) or not isinstance(body.get("model"), str):
        return _invalid_request("body must be a JSON object with a string 'model' field")

    model = body["model"]
    enable_vision = body.get("enable_vision")
    if enable_vision is not None and not isinstance(enable_vision, bool):
        return _invalid_request("'enable_vision' must be a boolean or null")
    context_hint = body.get("context_hint")
    # bool is an int subclass — reject it explicitly so `context_hint: true`
    # cannot leak a boolean into the args builder (-c True).
    if context_hint is not None and (isinstance(context_hint, bool) or not isinstance(context_hint, int)):
        return _invalid_request("'context_hint' must be an integer or null")

    try:
        await _manager().switch_model(
            model,
            enable_vision=enable_vision,
            context_hint=context_hint,
        )
    except ValueError as exc:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"error": "model_not_found", "message": str(exc)},
        )
    except ModelLoadError as exc:
        crash = exc.crash_record
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "error": "model_load_failed",
                "message": str(exc),
                "crash_details": crash.to_dict() if crash is not None else None,
            },
        )

    mgr = _manager()
    return {
        "ok": True,
        "loaded_model": mgr.current_model,
        "needs_reload": False,
    }


@app.post("/unload", dependencies=[Depends(require_caretaker_key)])
async def unload() -> None:
    """Unload the current model (Phase C fills this in)."""
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="not implemented",
    )