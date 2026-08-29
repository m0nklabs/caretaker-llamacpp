"""Central deployment-path resolution for caretaker.

The lifecycle core relocated from ``guardian-llmprovider-gateway/app/engine/
manager.py`` must not carry hardcoded deployment literals (paths, ports, URLs,
file names). Every deployment-dependent value lives here, resolvable through
env vars and falling back to sane defaults — mirroring the guardian's
``app/paths.py`` pattern and its ``{{NAME}}_ROOT``-style env overrides.

The caretaker writes its own deployment files into **its own** config dir
(default ``<repo>/config``, override ``CARETAKER_CONFIG_DIR``) rather than the
guardian's config dir: a caretaker owns the lifecycle of its local host, and
the trailing ``.args`` / ``.env`` / ``.sig`` files are read back by
``scripts/start_llama.sh`` on that same host.
"""

from __future__ import annotations

import os
from pathlib import Path


def _expand_path(value: str) -> Path:
    """Expand a filesystem path without requiring it to exist yet."""
    return Path(value).expanduser()


REPO_ROOT = _expand_path(
    os.getenv("CARETAKER_ROOT", str(Path(__file__).resolve().parent.parent))
)
CONFIG_DIR = _expand_path(os.getenv("CARETAKER_CONFIG_DIR", str(REPO_ROOT / "config")))

# Deployment files caretaker writes when it (re)launches llama-server. Named
# identically to the guardian's so the same ``scripts/start_llama.sh`` shape
# sources them, but they live in the caretaker's own config dir.
CURRENT_MODEL_ARGS_FILE = CONFIG_DIR / "current_model.args"
CURRENT_MODEL_ENV_FILE = CONFIG_DIR / "current_model.env"
CURRENT_MODEL_SIG_FILE = CONFIG_DIR / "current_model.sig"

# Where llama-server persists context slots (auto_save_* files). Guardian
# default is ``~/llama_slots``; caretaker reuses the same location on the same
# host so context continuity is preserved during the transition. Use the
# :func:`llama_slots_dir` call-time helper (not this constant) when a value
# that honors late env overrides is needed (e.g. under tests).
LLAMA_SLOTS_DIR = _expand_path(
    os.getenv(
        "CARETAKER_LLAMA_SLOTS_DIR",
        os.getenv("GUARDIAN_LLMPROVIDER_GATEWAY_SLOTS_DIR", str(Path.home() / "llama_slots")),
    )
)


def llama_slots_dir() -> Path:
    """Resolve the llama-server slot-save dir, honoring env at call time."""
    return _expand_path(
        os.getenv(
            "CARETAKER_LLAMA_SLOTS_DIR",
            os.getenv("GUARDIAN_LLMPROVIDER_GATEWAY_SLOTS_DIR", str(Path.home() / "llama_slots")),
        )
    )

# The llama-server binary caretaker launches directly for the DirectServerProcess
# (Phase A fallback / tests; Windows impl in Phase E). Default mirrors the
# guardian's ``~/llama_cpp_official/build/bin/llama-server``.
LLAMA_CPP_OFFICIAL_ROOT = _expand_path(
    os.getenv("LLAMA_CPP_OFFICIAL_ROOT", str(Path.home() / "llama_cpp_official"))
)
LLAMA_SERVER_BIN = _expand_path(
    os.getenv(
        "LLAMA_SERVER_BINARY",
        str(LLAMA_CPP_OFFICIAL_ROOT / "build" / "bin" / "llama-server"),
    )
)

# The llama-server endpoint caretaker probes for health / context slots. The
# host/port derive from this URL (guardian default http://127.0.0.1:11440).
SERVER_URL = os.getenv("CARETAKER_SERVER_URL", "http://127.0.0.1:11440")


def server_url() -> str:
    """Resolve the llama-server endpoint, honoring env at call time."""
    return os.getenv("CARETAKER_SERVER_URL", "http://127.0.0.1:11440")

# systemd unit caretaker controls for the SystemdServerProcess (default
# ``llama-server`` — the same unit guardian controls today).
SYSTEMD_SERVICE = os.getenv("CARETAKER_SYSTEMD_SERVICE", "llama-server")