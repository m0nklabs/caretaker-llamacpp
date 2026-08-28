"""uvicorn entrypoint for caretaker.

Binds the control API on ``127.0.0.1:11441`` (override with ``CARETAKER_PORT``).
Run with: ``./venv/bin/python -m caretaker``.
"""

from __future__ import annotations

import os

import uvicorn

DEFAULT_PORT = int(os.environ.get("CARETAKER_PORT", "11441"))


def main() -> None:
    uvicorn.run(
        "caretaker.server:app",
        host="127.0.0.1",
        port=DEFAULT_PORT,
    )


if __name__ == "__main__":
    main()