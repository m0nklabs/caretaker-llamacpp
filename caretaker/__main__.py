"""uvicorn entrypoint for caretaker.

Binds the control API on ``127.0.0.1:11441`` by default. Override with
``CARETAKER_HOST`` (e.g. the LAN interface when the gateway reaches this
caretaker from another GPU host) and ``CARETAKER_PORT``.
Run with: ``./venv/bin/python -m caretaker``.
"""

from __future__ import annotations

import os

import uvicorn

DEFAULT_HOST_ENV = "CARETAKER_HOST"
DEFAULT_PORT_ENV = "CARETAKER_PORT"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 11441


def resolve_bind_host() -> str:
    """Return the control-API bind host (``CARETAKER_HOST`` or loopback).

    Loopback is the safe default for the local bootstrap; a remote gateway
    (management_url on another GPU host, F5/F6) sets ``CARETAKER_HOST`` to
    the host's LAN address.
    """
    return os.environ.get(DEFAULT_HOST_ENV, DEFAULT_HOST)


def resolve_bind_port() -> int:
    """Return the control-API port (``CARETAKER_PORT`` or 11441)."""
    raw = os.environ.get(DEFAULT_PORT_ENV, str(DEFAULT_PORT))
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_PORT


def main() -> None:
    uvicorn.run(
        "caretaker.server:app",
        host=resolve_bind_host(),
        port=resolve_bind_port(),
    )


if __name__ == "__main__":
    main()