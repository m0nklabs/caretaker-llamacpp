# Caretaker — Resource and Service Controller Roadmap

> Canonical roadmap for [caretaker-llamacpp](https://github.com/m0nklabs/caretaker-llamacpp).
> Mission expansion: [issue #24](https://github.com/m0nklabs/caretaker-llamacpp/issues/24),
> complementary to [Guardian #113](https://github.com/m0nklabs/guardian-llmprovider-gateway/issues/113).
> Evidence snapshot: current-main implementation at `89ad9ac` (2026-11-18), not a live-host audit.
> Current work and verification: [HANDOFF](docs/HANDOFF.md). No runtime change is authorized by this plan.

## Mission and status vocabulary

Build an exceptional **cross-platform resource and service manager/controller** for
explicitly onboarded workloads, complementary to Guardian. Modern, maintainable,
tested and secure engineering, reproducible debugging and measured innovation are
requirements, not claims that a generic controller or state-of-the-art product ships.
The repository name reflects its llama.cpp origin; it does not limit the planned mission.

- **Implemented:** source and regression tests exist; this alone says nothing about deployment.
- **Historical native evidence:** an identified host/build was exercised on a recorded date;
  it does not certify all adapters, backends, platforms or today's installation.
- **Planned:** specified below, not delivered. **Untested/unestablished:** evidence absent;
  never interpret as supported. **Blocked:** a named dependency or human gate is missing.

## Current-main evidence and preserved active work

| Area | Observed implementation / evidence | Boundary and next evidence |
|---|---|---|
| llama.cpp lifecycle | [manager](caretaker/manager.py), [API](caretaker/server.py), [lifecycle race tests](tests/test_lifecycle_concurrency.py): ensure/unload, drift/config hot reload, verified model identity, lock + generation guard, crash recovery | Model-specific implementation, not a generalized desired/observed service controller |
| Other workloads | [Comfy](caretaker/comfy.py), [TTS](caretaker/tts.py), [STT](caretaker/stt.py) and authenticated adapter routes in the API | Existing workload-specific adapters, commands, timers and VRAM coordination; not a generic plugin or dependency graph |
| Death reporting | Manager `last_death`, UUID `event_id`, `backend_alive`, text-evidenced OOM and `crash_details` | Daemon-known state, not a fresh liveness probe or durable event ledger; R6 extends correlation, redaction and restart-safe deduplication |
| Portable checks | [Python CI](.github/workflows/python-ci.yml): self-hosted Linux, Python 3.12/3.14, isolated dev install, pip check, Ruff, compilation, guarded pytest | Unit/contract portability is not native OS, real process-tree, accelerator or installed-service certification |
| Coverage | Local package gate `--cov=caretaker --cov-report=term-missing --cov-fail-under=80`; no Codecov uploader required | [Issue #22](https://github.com/m0nklabs/caretaker-llamacpp/issues/22) remains open. Historical exact-head baseline: 81.57%, 335 passed/1 skipped on `011b4b0`, not a new measurement or current-head result; see handoff |
| Deployment identity | [Issue #19](https://github.com/m0nklabs/caretaker-llamacpp/issues/19) records a divergent running feature branch | Still open: current main, merged code and deployed code must not be conflated. No switch/restart/live verification in this task |
| Review infrastructure | [Issue #21](https://github.com/m0nklabs/caretaker-llamacpp/issues/21) records plain `/review` infrastructure failure and a model-suffixed workaround | Still open: infrastructure failure/cancellation or a requested review is not a completed clean review; a suffixed request does not replace the required exact `/review` |

The older **bootstrap and Phases A–D merged** status and **Phase E implementation +
native Windows handoffs** are preserved in [AGENTS](AGENTS.md) and the handoff. The
A–E specification below records the original scope, not a fresh delivery checklist.
No phase-wide, platform-wide or deployment-wide completion is inferred from those entries.

## Authority contract: one lifecycle/resource owner

**Planned target (R1–R5):** Caretaker is authoritative for each onboarded service's
validated desired state, observed state, configuration/lifecycle generation,
process identity, health/readiness and bounded host-resource reservations. Guardian
owns client authentication/authorization, service selection, routing/failover,
traffic policy, queues and application/API protocols. Guardian sends authorized
intent, requests admission/leases and supplies traffic/drain evidence; it must not
also spawn, kill or independently restart a Caretaker-owned process. Caretaker
reports capacity facts and enforces host safety; it does not duplicate Guardian's
client-facing policy engine, service resolver or inference/protocol gateway.

The existing Comfy wake proxy is an adapter-specific lifecycle trigger/TCP path,
not precedent for a second general traffic gateway. Generalization requires an
explicit compatibility contract and tests, not renaming the current adapters.
Current VRAM accounting is estimated-size bookkeeping, not a hard GPU reservation;
Comfy/TTS/STT free-memory probes are NVIDIA-specific and fail open on probe errors.
R5's conservative unknown-telemetry policy is a planned change, not current behavior.

**Current ownership is narrower:** llama lifecycle execution and watchdog live in
Caretaker; server lifespan arms/stops the watchdog (config-gated). The manager's
lock and generation invalidate stale recovery observations. `idle_since` for llama
is **load time**, not last traffic: Guardian decides traffic-idle unload and calls
`/unload`. TTS/STT have local ensure-refreshed idle timers; Comfy observes its own
queue/idle/wake path. They do not currently share a general active-request lease
protocol. Do not claim today's idle timers guarantee drain safety for all workloads.

**Supervisor boundary:** systemd/NSSM and Caretaker must not run competing recovery
loops. R1/R4 must specify who restarts daemon vs child, reconcile external child
restarts and process identity, and test restart storms. Existing unit restart
settings are evidence to audit, not permission to change deployment. Native
llama-server sleep is not unload, crash or a request to restart.

## Installation, OS and accelerator evidence matrix (R2)

Cells below describe evidence **at this snapshot**. “Historical” refers to dated
Linux/Windows records in the handoff/journal, not a newly repeated test. CPU,
CUDA, ROCm and Metal are distinct execution/telemetry contracts; detecting a GPU
or passing mocked `nvidia-smi` tests does not certify any backend.

| OS / installation path | CPU backend | CUDA backend | ROCm backend | Metal backend | Native/service evidence |
|---|---|---|---|---|---|
| Linux / isolated Python install; systemd template; direct-process class available | Untested native CPU-only workload matrix | Historical selected NVIDIA workload evidence only; current matrix untested | Untested/unestablished | Not applicable to this OS | Linux CI is unit/contract only; systemd paths and dated host evidence exist, no fresh install/upgrade/uninstall certification |
| Windows / Python process + NSSM daemon wrapper; Windows direct llama child | Untested native CPU-only workload matrix | Historical selected NVIDIA workload evidence only; current matrix untested | Untested/unestablished | Not applicable to this OS | Windows direct process and NSSM documentation/history exist; no native Windows CI matrix or fresh service-account/process-tree certification |
| macOS / installation and native supervision path planned | Untested/unestablished | Unestablished; no supported path claimed | Untested/unestablished | Untested/unestablished | `_default_server_process()` selects systemd on every non-`win32` host. macOS/launchd support is **not established** by Python portability |

For **each OS × process/supervisor × adapter × CPU/CUDA/ROCm/Metal cell**, R2 must
record OS/architecture, Python/package and service versions, install method,
binary/driver/device identity, permissions/service account, probe implementation,
exact commands/test revision, result and limitations. “Not applicable” needs a
reason; absent evidence stays untested. Publish reproducible install, isolated
upgrade, rollback/recovery and uninstall steps that preserve operator data/secrets;
never assume removing a package may delete models or shared environments.
Native gates exercise cold/warm ensure, readiness, paths/quoting/Unicode,
permissions, cancellation, process-tree stop, restart, resource probes and failure
cleanup. Run accelerator workloads on actual matching hardware, including CPU-only
and telemetry-unavailable cases. Portable CI remains required but cannot promote
these cells to supported. Native integrations require explicit host authorization.

## Planned tracks, dependencies and acceptance

All R tracks below are **planned**, even where existing code is a useful seed.
Implementation issues should reference these IDs and attach evidence here or in the
single handoff, not create a competing roadmap. Deliver small tested increments;
only replace old paths once compatibility and recovery evidence pass.

### R1 — Authoritative state and control contract

**Depends on:** current-main inventory, Guardian contract agreement; deployment
identity from #19 before any live conformance claim.

Define typed service identity, desired state (`running`/`stopped` plus validated
configuration revision), observed state (`unknown`, starting, ready, draining,
stopped, failed), ownership, monotonic generation and operation identity. Distinguish
accepted intent from observed readiness and stale/unknown observations. Define
adoption/rejection of pre-existing children after daemon restart, and compatibility
for `/ensure`, `/unload`, `fresh_load`, vision and `last_death` consumers.

**Acceptance:** documented state-transition/API tables and contract tests prove
same-intent idempotency, stale-generation rejection, no stale “ready” response,
intentional stop versus death, crash/restart/adoption behavior and single-owner
supervision. Guardian/Caretaker disagreement produces an explicit bounded error,
not a second lifecycle controller or arbitrary remote command execution.

### R2 — Installability and native platform/backend support

**Depends on:** R1 contract; R6 threat model before expanding remote exposure.

Implement the evidence matrix and installation/native test gates above, selecting
platform-appropriate supervision deliberately rather than falling through to
systemd. Feature-detect tools and permissions; report unsupported capabilities
without silently substituting accelerator telemetry or claiming zero usage.

**Acceptance:** every advertised supported cell has linked native evidence and
repeatable install/upgrade/recovery/uninstall instructions. Untested cells remain
explicit; CI adds native coverage only with approved suitable runners, never by
probing production. Reconfirm #19 deployment identity separately from source tests.

### R3 — Onboarded service adapters and dependencies

**Depends on:** R1; R2 for each advertised native cell; R6 trust boundaries.

Extract a small validated adapter contract from llama.cpp/Comfy/TTS/STT: discover
capabilities, validate local configuration, observe, start, health/readiness,
drain, stop and diagnose. Add explicitly onboarded non-LLM/CPU services to prove
generality. Define a dependency DAG, readiness barriers, reverse shutdown order,
cycle/missing-dependency rejection and partial-failure rollback. Keep engines,
protocols, hardware backends and supervisors separate capabilities.

**Acceptance:** shared conformance tests plus adapter-specific tests cover all four
existing adapters and at least one non-LLM CPU service; demonstrate dependency
failure/cancellation without orphan processes or leaked reservations. Only
operator-owned validated definitions may select local commands; control requests
reference onboarded identities/typed options, never caller-supplied shell text,
executable paths or remotely installed plugins. Document migration, not a second
parallel controller, and preserve existing adapter API behavior until agreed change.

### R4 — Reconciliation, idempotency, drain and leases

**Depends on:** R1, R3; R5 admission interface; R6 authenticated intent boundary.

Build a bounded reconciliation loop comparing desired/observed generation, with
coalesced operations, cancellation, timeout, jittered capped retry/backoff and
explicit terminal failures. Coordinate leases for active work and reservations:
owner/expiry, renewal, release, duplicate and lost messages, clock assumptions,
crashed clients and safe expiry. Admission closes before drain; define deadline
and explicit authorized forced-stop behavior. Resource safety and operator stop
intent beat automatic recovery; Guardian owns traffic stop/routing policy.

**Acceptance:** deterministic races/fault injection cover concurrent ensure,
reload, unload, watchdog, external restart, stale probe, daemon restart, duplicate
and out-of-order intent, disconnect and cancelled drain. One process/owner per
service generation, no resurrection after stop, bounded recovery and no reservation
leak after failure/cancellation. Lost traffic telemetry/expired lease must not be
assumed proof of idle; test the documented conservative behavior and visible reason.

### R5 — Multi-resource bounded admission and capacity

**Depends on:** R1, R3; coordinated with R4 lease/release semantics and R2 probes.

Extend beyond today's VRAM accounting and adapter-specific NVIDIA gates. Track
CPU slots/pressure, host RAM, per-device VRAM/compute capacity, disk/temporary
storage and I/O constraints as applicable; distinguish configured demand,
reservation, measured consumption and hard OS enforcement. Define bounded queues,
wait deadlines, fairness/priorities, starvation prevention and explicit rejection.
Never evict busy work solely because a free-memory sample fluctuates.

**Acceptance:** table-driven multi-service contention tests show no configured
budget oversubscription or leaks, including partial-start failure and repeated
cycles. Telemetry carries source/device identity, units, timestamp, freshness and
confidence. Missing/stale/unsupported telemetry is **unknown**, never zero/free:
default conservatively refuse new resource-dependent admission, preserve existing
work and surface why; any bounded configured fallback requires evidence and an
explicit risk decision. Validate CPU-only, multi-GPU and backend-specific probe
behavior natively before support claims. Report scheduler estimates separately
from measured or OS-enforced limits.

### R6 — Redacted observability and security

**Depends on:** R1; gates remote exposure and generalized R3/R4 control.

Threat-model control credentials, local command definitions/plugins, elevated
service control, endpoint/health-probe SSRF, path/argument injection, untrusted
backend output, wake proxy exposure, supply chain and malicious clients/tenants.
Preserve distinct control/backend credentials and constant-time comparison;
principle of least privilege, scoped allowlists, rotation/revocation and auditability
must be explicit. Bearer authentication over plain LAN HTTP is not protected
transport: require an approved TLS/mTLS or authenticated encrypted tunnel design
for non-loopback control, certificate validation and negative tests; no new
exposure follows from this document. Auth/secrets changes remain human-gated.

Extend structured logs/status/metrics with service, operation, generation,
correlation and deployment/build identity, readiness/queue/admission/drain reasons
and dependency state. Preserve current UUID death events and distinguish deliberate
stop, failed load and unattended death. Define restart/consumer persistence,
replay/loss and bounded retention so polling cannot double-count death events.

**Acceptance:** negative auth/authorization/transport/injection/SSRF tests and
redaction fixtures prove no keys, bearer headers, prompts, environment dumps or
sensitive raw config/path/log content escapes diagnostics. Bounded event/log sizes,
retention and metric cardinality are configured/tested. Existing `crash_details`,
config snapshots and adapter status must be audited, not assumed redacted. Correlate
one injected death across daemon/Guardian without duplicates, including repeated
polling, consumer restart and daemon restart; document remaining loss windows.

### R7 — Bounded reproducible debugging and measured innovation

**Depends on:** R1 identities, R6 sanitization; R2 native cells for hardware claims;
R4/R5 workloads for controller comparisons. Baseline first, optimize second.

Provide opt-in, sanitized diagnostic bundles and replayable fault scenarios:
manifest/revisions, OS/backend/driver, validated non-secret config, seed, fake clock,
operation timeline and minimized reproduction. Cover failed start, readiness stall,
OOM evidence versus signal-only death, telemetry loss, permission/port conflict,
deadlock, leaked child/descriptor/reservation and controller/Guardian disconnect.
No uncontrolled production probes, recursive retries or unbounded log collection.

**Acceptance — budget protocol:** before an experiment, record baseline samples
and **numeric** limits for wall time, iterations/retries, event/log bytes, CPU/RAM/
VRAM use and native GPU occupancy; abort/cleanup on each bound. Declare dataset,
service mix/concurrency, hardware/software, warm/cold state, repetitions, variation
and stop/rollback criteria. Unknown baselines are an explicit **not yet calibrated**
blocker to a performance gate, not permission to invent production SLOs.

For candidate improvements (e.g. event-driven reconciliation vs polling, adaptive
admission vs static reservations, alternate probes/supervisors), compare the **same
workload** and failure schedule against current stable behavior. Measure p50/p95
control latency, cold-start/readiness and recovery time, throughput, queue wait,
resource peaks, controller overhead and invariant violations. Publish raw sanitized
results and limitations; declare numeric regression/improvement budgets derived
from the measured baseline and operator workload requirements **before** the
candidate run. Pass correctness/security first, then the declared budgets; reject
or retain as experimental when benefits are unproven. No arbitrary universal
latency/CPU/memory budget or “best/SOTA” claim is accepted without that evidence.

## Delivery gates and sequencing

1. R1 inventory/contract and R6 threat model; R2 evidence inventory can proceed in parallel.
2. R3 adapter conformance; R4/R5 developed together around one lifecycle/resource authority.
3. R2 native support promotion per cell; R6 negative/security tests before exposure;
   R7 calibrated replay/benchmark evidence before optimization promotion.
4. Each implementation increment preserves the Linux Python 3.12/3.14 lint,
   compilation, guarded test and package-local 80% coverage gate. Add deterministic
   state-machine/property/concurrency/fault tests and authorized native checks;
   a coverage percentage is not correctness, security or platform certification.
5. Parent/lead owns publication/review. After the final push (and every later push),
   request review with exactly `/review` from the human account here (bots ignored).
   Require a **completed independent review of the current head**, all findings
   fixed or evidence-answered and threads handled, and green checks. #21 cannot be
   bypassed by treating an absent/failed/cancelled review as clean.
6. Apply the account's **2-hour window**: anchor at the last outstanding review
   answer, or for no findings at completed current-head review plus green checks,
   whichever is later; never before the latest push. Record the anchor. Read all
   latest comments/reviews/commits and handle new activity (restart clock when it
   needs a reply/fix/decision); refresh current base, head, merge state and checks
   at merge time. A closing comment is normal, **not a prerequisite**. No self-approval.
   Authentication, secrets, migrations, deployment and dependency majors retain
   their explicit human gates regardless of time. This docs task performs none
   of those actions and does not close #19, #21 or #22.

---

## Historical extraction specification — bootstrap, A–E and F5–F6

The following original phase specification is retained for history and compatibility,
not as the active roadmap or proof of current deployment. References to “today”,
“empty”, “next”, a gateway-owned lifecycle and future Windows implementation describe
the original 2026-08 plan. Bootstrap and A–D subsequently landed; Windows direct
process and Comfy/TTS/STT adapters now exist. The evidence and ownership sections
above supersede old ambiguity about idle and watchdog ownership, old plaintext LAN
examples and the suggestion that Windows lifecycle is operator-only. The original
full plan (including its replaced plan-only header/footer) is preserved verbatim in
[the declared archive](docs/archive/agent-instructions-2026-09-22.md).

## 1. Repo bootstrap (empty → walkable skeleton) — **Phase 1 deliverable, not today**

Repo is empty; this phase happens after local `git init` (operator/lead). Deliverables:

- **Python 3.14** (matches the stack), `pyproject.toml` (package `caretaker`, uvicorn
  runner). `.gitignore` (venv, data/, `.env`, logs — no secrets).
- **`README.md`** — one-paragraph "what this is" + pointer to the master plan.
- **`AGENTS.md`** — already scaffolded (init-scaffold, 2026-08-26); update to index this
  PLAN.md and keep the same "read, don't duplicate" rule for guardian docs.
- **Minimal empty-package layout** (stubs, not logic):
  - `caretaker/__main__.py` — uvicorn entrypoint (binds `127.0.0.1:11441`, LAN bind in E).
  - `caretaker/config.py` — reads `models.local.settings.yaml` (shared settings source).
  - `caretaker/manager.py` — subprocess wrapper (spawn/stop/health) — fill in Phase A.
  - `caretaker/server.py` — FastAPI app with the 3 endpoints.
- **`deploy/systemd/caretaker-llamacpp.service`** (Linux template) + a note on Windows
  (NSSM service, §6).
- **Deployment impact:** none (not activated until a phase runs it).

---

## 2. Phase A — Lifecycle core (spawn/stop/reload + args build)

Carried over from `engine/manager.py`. This is the heart.

**Goal:** caretaker can start, stop, reload `llama-server` for a model from a runtime
config, exactly as the gateway's `ModelManager` does today.

**Changes:**
- `spawn/stop/unload/reload`: `switch_model` orchestrator (manager.py ~833-952: auto-save
  context → `_stop_server` → `build_runtime_config` → `_write_server_args` → `_free_gpu_memory`
  → `_start_server` → `_wait_for_health` → persist sig → verify → restore context).
- `_build_args_string` (manager.py ~1234-1338) — **single source of truth** both for launch
  and for the signature. Keep byte-identical: `-m/-c/-ngl/-ctk/-ctv/--host/--port/--slot-save-path`,
  tensor-split, mmproj, speculative-decoding (draft / spec-type), `--parallel`, extra-args,
  and the `CUDA_VISIBLE_DEVICES` env_dict. Re-route all literal paths/ports through
  `config.py`/paths (rule: no hardcoded deployment literals).
- `_write_server_args` (manager.py ~1340-1364) persists `current_model.args` / `.env`;
  `scripts/start_llama.sh` sources them. **Note:** today `_start_server`/`_stop_server`
  shell out to `sudo systemctl start|stop llama-server` (manager.py ~1423-1440). Decide
  whether caretaker keeps delegating to the unit or owns the subprocess directly; the
  NSSM/Windows path requires a process-level control, so abstract this behind a
  `ServerProcess` interface now.
- Keep `_free_gpu_memory` (ComfyUI `/free` + nvidia-smi log, ~973-998) and context
  save/restore (`_save_context`/`_load_context` with `LLAMA_SLOTS_DIR`).

**Tests:** unit tests that build args strings and assert byte-parity with the persisted
`current_model.args` fixture; a fake process for stop/start; a load/swap cycle against a
stub llama-server.
**Deployment impact:** runs only in a staging harness; production gateway still manages
`llama-server` (dual-path not yet active).
**Acceptance:** caretaker can load model X, see `/health` 200, swap to Y, unload — with
identical args to today's `current_model.args`.

---

## 3. Phase B — Launch-signature drift detection

Copy the mechanism so a same-model request reloads when config changed.

**Goal:** detect that the running backend no longer matches current `models.local.settings.yaml`
and reload before serving.

**Changes:**
- `_compute_launch_signature` (manager.py ~1366-1387): `build_runtime_config` (resolves
  text/vision overrides + client `context_hint`) → `_build_args_string` → store
  `args_sha256` + `env_sha256` (hashlib.sha256), plus `model` + `vision` in a dict.
- Persist via `_write_persisted_signature`/`_read_persisted_signature` (`current_model.sig`
  JSON, manager.py ~1389-1400); `_config_drifted` (manager.py ~1402-1421) returns true when
  sig missing, or model/vision differ, or either hash differs.
- **Contract:** `POST /ensure {model}` must re-run drift detection (as `switch_model`
  does) — if not drifted, no-op; if drifted, reload. This is what makes `/ensure` a
  correct, idempotent repair primitive for the gateway.
- Move `_config_drifted` logic into `caretaker/manager.py`; keep a `--check-drift` mode so
  `/status` can report "needs reload" for discovery.

**Tests:** drift triggers on args change / vision toggle / context hint change; no-op when
identical (compare against the current `_config_drifted` unit expectations).
**Deployment impact:** none yet (standalone).
**Acceptance:** `/ensure` is idempotent and reloads exactly when drifted.

---

## 4. Phase C — Health/crash watchdog + auto-restart + unload + VRAM slot + recover-from-connect-error

**Goal:** caretaker independently keeps `llama-server` alive and within VRAM budget, and
exposes idle-unload + recovery primitives.

**Changes:**
- **Health + crash watchdog:** `_wait_for_health` (manager.py ~1442-1480, 120s poll on
  `/health`, crash-loop detection via `NRestarts` + `systemctl is-failed`), `_detect_crash`
  (manager.py ~1510-1540, `journalctl` error extraction → `CrashRecord`, `crash_history`
  capped at `MAX_CRASH_HISTORY=50`), `backend_health_ok` (~821-831). Add a background
  watchdog task that restarts the server on crash (today the unit's `Restart=on-failure`
  does this; caretaker should own it for the Windows path).
- **Unload:** `unload()` (manager.py ~963-971) — guard `is_unloaded`, `_stop_server`,
  set flag. Reuse for `/unload`.
- **VRAM slot:** port `VramScheduler` (`models.py` ~194-230): `acquire(model, size_mb)`
  holds on an `asyncio.Condition` until sum-of-active fits `limit_mb`; `release(model)`
  notifies. Caretaker owns acquire/release (VRAM-limit from config), e.g. `POST /ensure`
  acquires before load, `/unload` releases.
- **`reload_backend_after_connect_error`** (models.py ~232-277): on a stale backend, reload
  once before retry and surface 503 `{error, message, crash_details}` on failure. Caretaker
  exposes this as `POST /ensure` (idempotent) — the gateway catches 503 and calls `/ensure`,
  not its own reload.
- **Control-API auth:** from the first LAN exposure, all control endpoints (`/status`,
  `/ensure`, `/unload`) require `Authorization: Bearer ${CARETAKER_KEY}` (single
  per-caretaker key; see Implementation note below). Add a shared FastAPI/route dependency
  so the gate applies uniformly.

**Tests:** crash-loop → watchdog restart with backoff; acquire blocks until VRAM frees;
release notifies; unload guarded against double-unload; reload-after-connect-error path.
**Deployment impact:** first time caretaker may take over `llama-server` control on
ai-kvm-2 — **staged**, keep the systemd unit as fallback.
**Acceptance:** a killed `llama-server` is restarted by caretaker; `/status` reflects real
VRAM; requests never served against a stale/absent backend without an `/ensure` retry.

---

## 5. Phase D — Idle-unload via gateway contract + ensure-recovery

**Goal:** idle-unload execution lives here, but the **decision stays in the gateway** (it
sees the queue). The gateway tells caretaker it is safe to unload.

**Changes:**
- Implements `/unload` and an optional `/status` field (e.g. `idle_since`) so the gateway
  has the data to decide.
- **Idle decision (gateway)** = today's `idle_unload_watcher` logic in
  `app/proxy/lifespan.py` (~222-241): if `active_requests > 0` or
  `_inference_queue.active_count > 0` or `waiting_count > 0` → don't unload; else after
  `idle_minutes → /unload`. Move the *execution* (the actual unload call) to caretaker;
  the gateway reads the queue and calls `POST /unload` only when safe.
- **Ensure-recovery:** gateway catches `model_not_loaded` / 503 (from `/status` mismatch or
  a failed forward) and calls `POST /ensure {model}` before retry — mirrors today's
  `reload_backend_after_connect_error` recovery.

**Tests:** idle-unload fires only when the gateway declares the queue empty; `/ensure`
after a manual `/unload` reloads the model; 503 → `/ensure` → 200 recovery.
**Deployment impact:** gateway change (F5 wiring) — **operator-run restart** of gateway.
**Acceptance:** no unload while a request/queue is pending; any unloaded state is
transparently recovered by `/ensure`.

---

## 6. Phase E — Multi-host + Windows (14700K)

**Goal:** a second caretaker on the Windows 14700K serves `llama-server.exe` over the LAN;
the gateway reaches both via `management_url`.

**Changes (caretaker):**
- **Per-host config:** each caretaker reads its **own** `models.local.settings.yaml`
  (14700K file has Windows GGUF paths — no copy of the Linux list).
- **LAN bind:** the control API binds to the LAN interface in this phase (today `127.0.0.1`);
  inference remains on `:11440`, control on `:11441` (`http://192.168.1.35:11441` +
  `http://192.168.1.x:11441`). **Auth is mandatory from first LAN bind:** the single
  per-caretaker key (see Context → Security) gates every control call — this is exactly
  when the key model becomes load-bearing.
- **Windows service:** no systemd → **NSSM** wrapper (or scheduled task at startup) running
  the same caretaker package + `llama-server.exe`; `ServerProcess` interface from Phase A
  gets a Windows impl (no `sudo systemctl`, process-group kill, `--host 0.0.0.0`).

**Changes (gateway repo — dependency, do here):** new provider entry `14700k-local`
(`config/providers/14700k-local.settings.yaml`: `base_url http://192.168.1.x:11440/v1`,
`catalog_url: /v1/models`, `management_url http://192.168.1.x:11441`); firewall inbound
ports; context overrides per model (llama-server reports no context). F2+F3 machinery makes
routing/discovery/failover appear automatically. **What does NOT apply:** lifecycle /
idle-unload / VRAM scheduler / auto-switch for the Windows GPU (operator manages it).

**Tests:** LAN `/status` + `/ensure` from ai-kvm-2 against the 14700K caretaker.
**Deployment impact:** Windows caretaker running as a service on the 14700K —
Windo ws-side ops; gateway entry is **config-only → hot-reload**.
**Acceptance:** `curl <win-ip>:11440/v1/models` → 200 cross-LAN; gateway chat to
`14700k-local/<model>` (non-stream + stream) → 200; management_url works both ways.

---

## 7. Wiring into the gateway (gateway repo, F5–F6)

- `local` becomes a **passive provider entry**:
  ```yaml
  providers:
    local:
      base_url:       http://127.0.0.1:11440/v1
      management_url: http://127.0.0.1:11441
      managed: false
    ```
- Gateway calls `POST /ensure {model}` before forwarding (when model not already loaded),
  then `POST /v1/chat/completions` on llama-server. `GET /status` feeds discovery.
- **Ensure-response contract (2026-08-30, verrijkt):** the 200 response now carries
  `loaded_model` (what the daemon actually loaded), `fresh_load` (True = this call
  actually (re)started llama-server — in-memory session state is gone and the gateway
  may restore the saved context; False = the no-op fast-path ran, the live session is
  authoritative) and `vision_enabled` (the daemon's own resolution of the flag the
  loaded process runs with — authoritative over any gateway-side probe). This lets the
  gateway drop its heuristic probes in favour of daemon-confirmed facts.
- **Gateway restart does NOT drop the loaded model** (lifecycle lives outside the gateway).
- **Manager/caretaker restart → gateway recovers via `/ensure`** (Phase C/D recovery).
- Old direct lifecycle code is removed from the gateway; `engine/manager.py` (lifecycle
  core) moves under `caretaker/` (per F4 outcome) or is deleted once caretaker is live.

---

## Dependencies on the gateway repo (must land first)

1. **F4 — Untangle** (~509 registry/choice/discovery lines out of `manager.py` into the
   gateway layer). Pure relocation, no behaviour change. Undoes the current entanglement
   and establishes the clean boundary caretaker extracts to. **Without F4, the extraction
   is messy; with it, caretaker = the real lifecycle core only.**
2. **F2/F3** — provider registry recognizes `local` as a provider and local is a
   `managed` entry; `catalog_url: /v1/models` verified. Needed before `local` can be made
   passive in §7.
3. **Gateway-side contract changes** (F5): the ensure-before-forward + 503/`model_not_loaded`
   recovery paths, and the idle-unload **decision** feeding `/unload`.
4. **Per-caretaker key wiring (guardian, F5–F6):** the gateway stores one key per
   caretaker-host provider entry (`config/providers/<host>-local.settings.yaml`,
   `api_key: ${<HOST>_CARETAKER_KEY}`) and sends `Authorization: Bearer <key>` on every
   control call (`/status`, `/ensure`, `/unload`) — the same pattern as cloud providers.
4. **Provider naming** confirmed: `ai-kvm2-local`, `14700k-local` (F2 layout).

**When caretaker work can start:** the standalone-repo caretaker (Phases A–E) is largely
**independent** of F4 — its internals relocate the lifecycle core directly from `manager.py`.
Only §7 (wiring) and the clean removal of the ~509 registry lines depend on F4. So: build
caretaker standalone now (A–E can proceed in parallel with gateway F2–F4); block §7 on F4.
The in-gateway-first (monorepo) bootstrap from the master plan stays **OPTIONAL** — the
standalone repo is the destination; if stability pressure favors it, run caretaker from
inside the gateway repo temporarily, but treat that as a throwaway bootstrap, not a
permanent location.

---

## Open questions (cross-repo)

1. **Monorepo-first vs direct split.** The master plan recommends in-repo-first; the
   operator has created the standalone repo + dir. Plan treats standalone as primary and
   in-gateway bootstrap as optional — confirm this stance.
2. **`POST /pick` placement.** Recommendation: **gateway keeps the choice**, caretaker only
   executes (`/ensure`). Confirm no move of choice into caretaker.
3. **Windows IP / GGUF list / VRAM on the 14700K.** Which GGUFs, how much VRAM
   (`~limit_mb`), the exact LAN IP for `management_url`. Unknowns blocking E specifics.
4. **Key handling for `/ensure` + `/unload`.** — **RESOLVED (operator 2026-08-27):**
   **single per-caretaker key.** Each caretaker authenticates its control API with **one
   key of its own** (`Authorization: Bearer ${CARETAKER_KEY}`, read from env/secret, never
   committed). The gateway stores that key in the caretaker-host's provider entry (per-host
   key, just like cloud providers) and sends it on every control call. A LAN-IP allowlist is
   optional and never a replacement. Implementation lives primarily in this (caretaker) repo;
   gateway wiring is a dependency (see §7/Dependencies).
5. **Where does `scheduler/manager.py` (maintenance/services-stopper) go?** Stays in the
   gateway or moves to systemd/cron — operator choice (not caretaker scope).
6. **Watchdog ownership.** Does caretaker own crash-restart (systemd unit as pure exec), or
   keep the current unit's `Restart=on-failure`? Affects the `ServerProcess` abstraction.
7. **`context_hint`/client hint passthrough** — must `/ensure` accept a `context_hint` to
   preserve the existing client-context feature after split? (Drift folds it in today.)
