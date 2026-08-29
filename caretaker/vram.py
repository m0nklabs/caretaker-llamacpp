"""VRAM budgeting for the caretaker.

Port of the guardian's ``VramScheduler`` (``app/local_inference/models.py``)
so caretaker can own the "fit within a VRAM budget before loading" gate on the
GPU host. A single ``VramScheduler`` instance tracks *active* model loads and
holds an ``asyncio.Condition`` when acquiring would exceed ``limit_mb``;
:meth:`VramScheduler.release` notifies waiters so a later acquire can proceed.

Deployment literals are avoided: the VRAM limit is constructor-injected (the
manager resolves it from config), and the per-model size comes from
:func:`get_model_size_mb` (config ``size_mb`` first, then name heuristics).
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any

logger = logging.getLogger("caretaker.vram")


class VramLimitExceededError(ValueError):
    """A model's footprint alone exceeds the whole VRAM budget.

    Raised by :meth:`VramScheduler.acquire` instead of blocking forever: no
    release can ever free enough space, so waiting could only deadlock the
    caller. Subclasses ``ValueError`` so generic handlers treat it as a
    request-level error.
    """


def get_model_size_mb(model_name: str, config: dict[str, Any] | None = None) -> int:
    """Return the estimated VRAM footprint of ``model_name`` in megabytes.

    Precedence:
    1. An explicit ``size_mb`` key in the model's config (the operator knows the
       true footprint; never guess over a configured value).
    2. Name-based heuristics mirroring the guardian's ``get_model_size`` chain
       (glm-4 / 35b / 31b / qwen3-30b / deepseek-r1-32b / 70b + generic).
    3. ``0`` for an unknown/unconfigured model — such a model claims **no** VRAM,
       so a missing size never deadlocks the scheduler. :meth:`VramScheduler.acquire`
       logs a warning when a model acquires with size 0.
    """
    if config and config.get("size_mb"):
        return int(config["size_mb"])

    if not model_name:
        return 0
    model_lower = model_name.lower()

    # Specific overrides for new models
    if "glm-4" in model_lower:
        return 26000
    if "35b" in model_lower:
        return 22000
    if "31b" in model_lower:
        return 20000
    if "qwen3" in model_lower and "30b" in model_lower:
        return 20000
    if "deepseek-r1" in model_lower and "32b" in model_lower:
        return 22000

    # Generic heuristics
    if "70b" in model_lower:
        return 40000
    if "32b" in model_lower:
        return 20000
    if "30b" in model_lower:
        return 20000
    if "27b" in model_lower:
        return 18000
    if "13b" in model_lower:
        return 10000
    if "14b" in model_lower:
        return 11000
    if "8b" in model_lower:
        return 6000
    if "7b" in model_lower:
        return 5000
    if "1.5b" in model_lower:
        return 1500

    # Small models
    if "0.5b" in model_lower:
        return 600
    if "embed" in model_lower:
        return 500

    return 0


class VramScheduler:
    """Serializes model loads so active VRAM usage stays within ``limit_mb``.

    ``active_counts`` maps model name → number of active acquires. A load is
    allowed through when the *sum of the sizes of distinct active models* plus
    the new model's own size (if not already active) fits within
    ``limit_mb``; otherwise :meth:`acquire` blocks on the shared condition
    until a ``release`` frees space.

    Usage: ``await scheduler.acquire(model, size_mb)`` before starting the
    model, and ``await scheduler.release(model)`` after stopping it. Since the
    scheduler tracks a *size* budget and not a fixed slot, multiple small
    models may be resident together until the limit is met.
    """

    def __init__(self, limit_mb: int) -> None:
        self.limit_mb = limit_mb
        self.active_counts: dict[str, int] = defaultdict(int)
        # The size each *distinct* active model was acquired with. Sizes must
        # be stored, not recomputed from the name: the model config (size_mb)
        # is only known at acquire time, and name heuristics would silently
        # count a configured-size model as 0 → the budget check lies.
        self.active_sizes: dict[str, int] = {}
        self.condition = asyncio.Condition()

    async def acquire(self, model_name: str, model_size_mb: int) -> None:
        """Block until ``model_name`` can be added within the VRAM budget.

        If ``model_size_mb`` is 0 (no config size and no heuristic matched),
        the model claims no VRAM: acquire proceeds immediately and logs a
        warning so an operator notices the under-counted model.

        Raises :class:`VramLimitExceededError` when the model alone is bigger
        than the whole budget — such a model can never fit, so fail fast.
        """
        if model_size_mb <= 0:
            logger.warning(
                "VRAM size unknown for '%s' (size_mb<=0) — acquiring without "
                "accounting for its footprint",
                model_name,
            )
        async with self.condition:
            # Fail fast for a model that can never fit: its footprint alone
            # exceeds the budget, so no release can ever create room. Waiting
            # would block this caller forever (only a restart would recover).
            if model_name not in self.active_sizes and model_size_mb > self.limit_mb:
                raise VramLimitExceededError(
                    f"Model '{model_name}' needs {model_size_mb}MB but the VRAM "
                    f"limit is {self.limit_mb}MB"
                )
            while True:
                needed_vram = sum(self.active_sizes.values())
                if model_name not in self.active_sizes:
                    needed_vram += model_size_mb

                if needed_vram <= self.limit_mb:
                    self.active_counts[model_name] += 1
                    if model_name not in self.active_sizes:
                        self.active_sizes[model_name] = model_size_mb
                    logger.info(
                        "VRAM acquired for '%s' (active: %s, %dMB <= %dMB)",
                        model_name,
                        list(self.active_sizes),
                        needed_vram,
                        self.limit_mb,
                    )
                    return

                logger.info(
                    "'%s' waits: active VRAM %dMB > limit %dMB",
                    model_name,
                    needed_vram,
                    self.limit_mb,
                )
                await self.condition.wait()

    async def release(self, model_name: str) -> None:
        """Decrement one active acquire of ``model_name`` and wake waiters."""
        async with self.condition:
            self.active_counts[model_name] -= 1
            if self.active_counts[model_name] <= 0:
                del self.active_counts[model_name]
                self.active_sizes.pop(model_name, None)
            self.condition.notify_all()
            logger.info("VRAM released for '%s'", model_name)