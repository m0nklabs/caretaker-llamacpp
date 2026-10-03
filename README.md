# caretaker-llamacpp

`caretaker-llamacpp` is the **per-GPU-host manager daemon that owns the llama-server
lifecycle**: it spawns, stops, reloads and monitors the local `llama-server` behind a
thin, authenticated control API on port `:11441` (`GET /status`, `POST /ensure`,
`POST /unload`). OpenAI inference stays direct to `llama-server` on `:11440/v1` and is
**not** handled by this daemon. See the phased implementation plan in
[`PLAN.md`](./PLAN.md) for the full roadmap (phases A–E) and the context/goals in
section 0 of that file.

## Development checks

Use a dedicated virtual environment rather than system or shared-runner packages:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pip check
.venv/bin/python -m ruff check caretaker tests
.venv/bin/python -m compileall -q caretaker tests
.venv/bin/python -m pytest tests/ -q -p no:cacheprovider
```

CI runs the same installation and checks in isolated Python 3.12 and 3.14
virtual environments. Pytest, pytest-asyncio and Ruff versions are pinned in
`pyproject.toml`; Ruff also rejects a mismatched executable version. Its target
is the minimum supported Python 3.12, not the developer's interpreter. Runtime
and transitive dependencies are not fully locked by these development-tool pins.
The repository owns this CI job because the shared workflow installs unpinned
Ruff and uses preinstalled runtime dependencies; a local dev pin alone would not
control that workflow.

### Test isolation

The default pytest suite installs an I/O guard (`tests/conftest.py`,
`tests/io_guard.py`). Network/DNS and host-process operations must be mocked;
Comfy TCP tests explicitly register their own loopback listeners. An unexpected
operation fails the test instead of being silently treated as a backend outage.
Use `httpx.MockTransport` for HTTP contract tests and injected process doubles
for lifecycle tests. This is a regression guard, not an OS sandbox for untrusted
code; importing a module before fixtures or deliberately replacing the guard
can bypass it. The guard also records violations so swallowing its exception
does not turn an accidental production request into a passing test.

The cross-repository Guardian argument-parity test is skipped by default. On an
explicitly approved integration host, run only that test with:

```sh
.venv/bin/python -m pytest tests/test_phase_a.py -k crosscheck \
  --run-guardian-parity -q -p no:cacheprovider
```

This permits one exact Guardian bridge subprocess, not general parent-process
I/O. Code imported by that external child is outside the unit-test guard; review
its effects before opting in. CI does not enable this integration.

For TTS/STT launches, the parent closes its log-file handle after process creation,
including failed or cancelled starts; the child keeps its inherited output handle.
Log-file opening remains a small synchronous operation with a documented local
lint exception, not a background open that could outlive cancellation.