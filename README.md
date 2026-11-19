# caretaker-llamacpp

Caretaker's mission is to become an exceptional **cross-platform resource and
service manager/controller**, complementary to Guardian. Caretaker owns host
lifecycle/resource execution; Guardian owns client policy, routing, traffic and
application protocols. This is a planned expansion, not a claim that a generic
controller, complete cross-platform support or state-of-the-art performance ships.

**Implemented today:** llama.cpp lifecycle and Comfy/TTS/STT adapters, behind an
authenticated control API (default loopback `:11441`), with workload-specific
health, idle and resource coordination. Inference remains on the engine endpoints;
Caretaker is not a second general inference gateway. Existing Comfy TCP wake-proxy
behavior is adapter-specific and does not share the control routes' Bearer gate.
Linux systemd and Windows direct-process/NSSM paths exist; macOS support is not
established (non-Windows llama process selection currently defaults to systemd).

[PLAN.md](PLAN.md) is the **canonical roadmap**, with evidence/status distinctions,
the OS/install/CPU/CUDA/ROCm/Metal matrix, R1–R7 planned tracks, dependencies and
acceptance gates: authoritative desired/observed state and generations, onboarded
adapters, reconciliation/drain/leases, multi-resource capacity, security/redacted
observability, reproducible debugging and baseline-calibrated innovation. Original
A–E history remains there, explicitly historical. Unit CI does not certify native
services or GPUs; untested cells remain explicit.

See [AGENTS.md](AGENTS.md) for engineering rules and [the handoff](docs/HANDOFF.md)
for dated evidence and open work. Deployment drift [#19](https://github.com/m0nklabs/caretaker-llamacpp/issues/19),
review infrastructure [#21](https://github.com/m0nklabs/caretaker-llamacpp/issues/21)
and local coverage [#22](https://github.com/m0nklabs/caretaker-llamacpp/issues/22)
remain distinct from the mission documentation; merged source is not proof of
running deployment. No runtime/service change follows from this roadmap.

## Development checks

Use a dedicated virtual environment rather than system or shared-runner packages:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pip check
.venv/bin/python -m ruff check caretaker tests
.venv/bin/python -m compileall -q caretaker tests
.venv/bin/python -m pytest tests/ -q -p no:cacheprovider \
  --cov=caretaker --cov-report=term-missing --cov-fail-under=80
```

CI runs the same installation and checks in isolated Python 3.12 and 3.14
virtual environments. Pytest, pytest-asyncio and Ruff versions are pinned in
`pyproject.toml`; Ruff also rejects a mismatched executable version. Its target
is the minimum supported Python 3.12, not the developer's interpreter. The local
coverage floor measures `caretaker/`, not the whole checkout; no Codecov upload or
external coverage status is required. Runtime and transitive dependencies are not
fully locked by these development-tool pins.
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