"""``VramScheduler.update_size`` unit pins — PR-#15 review, finding 2.

A config hot-reload can change a model's configured ``size_mb`` while its
slot is held; ``update_size`` re-books the reservation in place, strict and
never blocking (the same-name switch could only free the space it needs, so
waiting would deadlock — swap-deadlock rule).
"""

from __future__ import annotations

import pytest
from caretaker.vram import VramLimitExceededError, VramScheduler


async def test_update_size_raises_valueerror_for_not_held_model() -> None:
    sched = VramScheduler(500)
    with pytest.raises(ValueError, match="not held"):
        await sched.update_size("ghost", 300)
    assert sched.active_sizes == {}


async def test_update_size_exceeding_budget_fails_fast_without_changes() -> None:
    """Growing beyond the budget raises immediately (no waiting) and leaves
    ``active_sizes``/``active_counts`` untouched (all-or-nothing)."""
    sched = VramScheduler(500)
    await sched.acquire("a", 300)
    with pytest.raises(VramLimitExceededError):
        await sched.update_size("a", 600)
    assert sched.active_sizes == {"a": 300}
    assert sched.active_counts["a"] == 1


async def test_update_size_fitting_change_swaps_size_keeps_counts() -> None:
    """A fitting (grow or shrink) re-size swaps the size in place; counts are
    untouched and the change never blocks."""
    sched = VramScheduler(500)
    await sched.acquire("a", 300)
    await sched.update_size("a", 400)
    assert sched.active_sizes == {"a": 400}
    assert sched.active_counts["a"] == 1

    await sched.update_size("a", 100)  # shrink: just updates in place
    assert sched.active_sizes == {"a": 100}
    assert sched.active_counts["a"] == 1


async def test_update_size_accounts_for_other_active_models() -> None:
    """The budget check replaces the model's OWN size in the active sum, so
    co-resident models are accounted for."""
    sched = VramScheduler(500)
    await sched.acquire("a", 200)
    await sched.acquire("b", 200)
    # needed = 200 + (200 - 200 + 200) = 400 <= 500 → fits (no-op re-size)
    await sched.update_size("a", 200)
    # needed = 400 - 200 + 150 = 350 <= 500 → fits
    await sched.update_size("b", 150)
    assert sched.active_sizes == {"a": 200, "b": 150}
    # needed = 400 - 150 + 360 = 610 > 500 → rejected
    with pytest.raises(VramLimitExceededError):
        await sched.update_size("b", 360)
    assert sched.active_sizes == {"a": 200, "b": 150}


# ---------------------------------------------------------------- restore_held
async def test_restore_held_rebooks_released_slot_exactly() -> None:
    """A swap releases the running model's slot; the failure path re-books the
    EXACT captured size (not a recomputed one) with a single count."""
    sched = VramScheduler(500)
    await sched.acquire("a", 300)
    held = sched.active_sizes["a"]
    await sched.release("a")
    assert sched.active_sizes == {}

    await sched.restore_held("a", held)
    assert sched.active_sizes == {"a": 300}
    assert sched.active_counts["a"] == 1


async def test_restore_held_is_idempotent_when_slot_still_held() -> None:
    """Restoring over a slot that is still held must not double-count."""
    sched = VramScheduler(500)
    await sched.acquire("a", 300)
    await sched.restore_held("a", 300)
    assert sched.active_sizes == {"a": 300}
    assert sched.active_counts["a"] == 1


async def test_restore_held_ignores_budget_but_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Reality outranks the budget: a running backend keeps its reservation
    even when a failed swap left the target booked too (over-limit is logged,
    never blocked — waiting could not free anything on this path)."""
    sched = VramScheduler(500)
    await sched.acquire("b", 400)  # the failed target's leftover slot
    with caplog.at_level("WARNING", logger="caretaker.vram"):
        await sched.restore_held("a", 300)
    assert sched.active_sizes == {"a": 300, "b": 400}
    assert any(
        "over budget after restoring" in record.getMessage() for record in caplog.records
    ), [record.getMessage() for record in caplog.records]
