"""Control API for caretaker.

FastAPI app exposing the thin control contract to the gateway:
``GET /status``, ``POST /ensure {model}``, ``POST /unload``. Every control call
requires ``Authorization: Bearer ${CARETAKER_KEY}`` read from the environment
(never committed). Handlers are bootstrap stubs that return ``501 Not
Implemented`` — the lifecycle phases (A–E) fill them in later.
"""

from __future__ import annotations

import hmac
import os
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, status

CARETAKER_KEY_ENV = "CARETAKER_KEY"

app = FastAPI(title="caretaker", version="0.1.0")


def _configured_key() -> str | None:
    """Return the configured control key, or None if not set."""
    key = os.environ.get(CARETAKER_KEY_ENV)
    return key if key else None


async def require_caretaker_key(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Auth gate: enforce the single per-caretaker Bearer key.

    - No ``CARETAKER_KEY`` env var set → ``503`` "caretaker key not configured".
    - Wrong/missing key → ``401``.
    - Correct key → pass through.
    """
    expected = _configured_key()
    if expected is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="caretaker key not configured",
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


@app.get("/status", dependencies=[Depends(require_caretaker_key)])
async def get_status() -> None:
    """Report loaded model + gpu/vram status (Phase A/C fills this in)."""
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="not implemented",
    )


@app.post("/ensure", dependencies=[Depends(require_caretaker_key)])
async def ensure() -> None:
    """Load/swap a model idempotently (Phase A/B/C fills this in)."""
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="not implemented",
    )


@app.post("/unload", dependencies=[Depends(require_caretaker_key)])
async def unload() -> None:
    """Unload the current model (Phase C/D fills this in)."""
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="not implemented",
    )