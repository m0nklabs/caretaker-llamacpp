"""Caretaker lifecycle manager skeleton.

This is the bootstrap skeleton (PLAN.md §1): the class shape that later phases
fill in. Phase A (lifecycle core), B (launch-signature drift), C (health/crash
watchdog + unload + VRAM slot) and D (idle-unload via gateway contract) port
the local lifecycle core out of
``guardian-llmprovider-gateway/app/engine/manager.py`` into this module. Today
every method is a stub — **no lifecycle logic lives here yet**.
"""

from __future__ import annotations


class Caretaker:
    """Owns the local llama-server lifecycle behind the control API.

    Instantiated by ``caretaker.server`` and wired to the ``/status``,
    ``/ensure`` and ``/unload`` routes once the phases implement the methods
    below. No state or behaviour yet at bootstrap time.
    """

    def __init__(self) -> None:
        pass

    def spawn(self, model: str) -> None:
        """Start llama-server for ``model``.

        Phase A (PLAN.md §2): port ``switch_model``/``_start_server``/
        ``_build_args_string`` from ``engine/manager.py``, behind a
        ``ServerProcess`` interface (systemd today, NSSM on Windows).
        """
        raise NotImplementedError("lifecycle core arrives in Phase A")

    def stop(self) -> None:
        """Stop the running llama-server.

        Phase A (PLAN.md §2): ``_stop_server``, unmounting the current model.
        """
        raise NotImplementedError("lifecycle core arrives in Phase A")

    def unload(self) -> None:
        """Unload the current model (guard against double-unload).

        Phase C (PLAN.md §4): ``unload()`` from ``engine/manager.py``.
        """
        raise NotImplementedError("lifecycle core arrives in Phase C")

    def reload(self, model: str) -> None:
        """Reload ``model``, detecting launch-signature drift.

        Phase A+B: idempotent swap; Phase B adds ``_config_drifted`` so a
        same-model request reloads only when the backend config changed.
        """
        raise NotImplementedError("lifecycle core arrives in Phase A")

    def health(self) -> dict[str, object]:
        """Return a status dict consumed by ``GET /status``.

        Phase A: ``_wait_for_health``; Phase C feeds crash/watchdog and VRAM
        state into this. Returns an empty placeholder until implemented.
        """
        raise NotImplementedError("lifecycle core arrives in Phase A")