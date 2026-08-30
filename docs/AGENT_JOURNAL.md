# AGENT_JOURNAL — caretaker-llamacpp

> Append-only findings-log (cold file): feiten die moesten worden opgegraven, live-testresultaten, lessons.
> Nieuwe entries **onderaan** appen; niets herschrijven.

## 2026-08-30 avond — PR #8 (Phase E): CI-flake-diagnose + review-cycle-les

- **Lesson:** A PR-Piet review of the last push can arrive AFTER the worker turn ends — the thread-check right before the merge signal is mandatory, a "clean" verdict has a shelf life.
  (Live on PR #8: after the last code push `d6ff3e2` there still followed a thread reply + resolve, 2 concurrency-cancels of the pull_request review check, and a fresh review run — an earlier "ready" statement was already stale by the time the next session looked.)
- **Lesson:** CI TimeoutError on a test unrelated to the PR diff: verify flake first (local 3x + pre-PR base + prior runs history), then rerun --failed — never push speculative fixes for un-reproduced failures.
  (Live: 2× the same TimeoutError on 2 different runners vs 9× green locally; the "rerun failed ⇒ regression" inference was wrong — the real cause sat in the test environment, not in the PR diff.)
- **Fact (test isolation):** the Phase-C/D swap tests talk to whatever listens on `http://127.0.0.1:11440` on the runner host (`isolated_paths` fixture sets `CARETAKER_SERVER_URL`) — on ai-kvm2 that is the production llama-server (CI log shows "Backend model mismatch … qwen3.8-27b…"). On a clean runner the connect is refused instantly and the test moves on; on ai-kvm2 the outcome depends on backend load against the 2 s `wait_for` budget. Follow-up candidate: per-test HTTP mock backend (or hard-isolate the backend URL).
- **Fact (test bypasses platform selection):** `test_switch_model_swap_frees_old_slot_without_deadlock` injects `FakeServerProcess` via `_make_manager(..., server_process=proc)` — `_default_server_process()` (the Phase-E change in `manager.py`) is never called by that test. When lifecycle tests fail in CI, check the injection path before suspecting the platform selection.
- **Fact (workflow):** a rerun of a concurrency-cancelled pr-piet run can itself be concurrency-cancelled by a newer run in the same group (20:03:19: the rerun's tier-1 job was cancelled by fresh `pull_request` run `33332549005`, started 20:03:08). Always check for newer runs in the same concurrency group before concluding a rerun "failed".
