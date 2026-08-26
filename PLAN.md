# Caretaker-llamacpp — Phased Implementation Plan

> Status: **plan only (2026-08-26).** No code exists yet in this repo — this file is the plan.
> Canonical repo: `m0nklabs/caretaker-llamacpp`. Cross-links the Guardian 2.0 master
> plan (`docs/IMPLEMENTATION_PLAN.md`, phases **F4 / F5 / F6**) and the split analysis
> (`docs/GATEWAY_MANAGER_SPLIT.md`). Source-of-truth code today lives in
> `guardian-llmprovider-gateway/app/engine/manager.py` (~1637 lines) and
> `app/local_inference/models.py` (~277 lines); this plan cites those functions by name.

---

## 0. Context & goals

**What caretaker IS:** the per-GPU-host daemon that **owns the llama-server lifecycle.
It spawns, stops, reloads, and monitors the local `llama-server` behind a thin control
API. It reads its host's own `models.local.settings.yaml` and never duplicates a registry.
Deployment (operator 2026-08-26): one caretaker per GPU host — one on `ai-kvm-2` (Linux)
and one on the `14700K` (Windows). The gateway lives only on ai-kvm-2 and talks to both
via `management_url`.

**What caretaker does NOT do:** no LLM routing, no auth/proxy, no capture/WAL logging,
no queue, no discovery/choice logic. Those stay in the gateway. Choice (`/pick`, which
model serves a request) stays in the gateway; caretaker only executes.

**Control API contract (gateway ↔ caretaker), thin:**
```
GET  /status            → loaded model + gpu/vram status
POST /ensure {model}    → load/swap (VRAM slot); idempotent
POST /unload            → unload (only when gateway says queue is empty)
```
OpenAI inference stays direct to `llama-server` (`http://127.0.0.1:11440/v1`); the
control API on `:11441` is lifecycle only.

**Split boundary (hard counts from `GATEWAY_MANAGER_SPLIT.md`):** ~1050 lines true
lifecycle (spawn/stop/restart, args build, launch-signature drift, health/crash watchdog
+ auto-restart, unload, ComfyUI VRAM freeing, context save/restore) + ~40 VRAM-slot
(`VramScheduler.acquire/release`) + ~40 `reload_backend_after_connect_error` + ~25
idle-unload watcher move here. The ~509 registry/choice/discovery lines and ~130
resolution/sizes/timeouts stay in the gateway (untangled in F4).

---

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
  `http://192.168.1.x:11441`).
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
4. **Key handling for `/ensure` + `/unload`.** Once exposed on the LAN, these must be
   authenticated — recommend a **shared secret** (`Authorization: Bearer ${CARETAKER_KEY}`)
   or a **LAN-IP allowlist**; decide which and default the local loopback case.
5. **Where does `scheduler/manager.py` (maintenance/services-stopper) go?** Stays in the
   gateway or moves to systemd/cron — operator choice (not caretaker scope).
6. **Watchdog ownership.** Does caretaker own crash-restart (systemd unit as pure exec), or
   keep the current unit's `Restart=on-failure`? Affects the `ServerProcess` abstraction.
7. **`context_hint`/client hint passthrough** — must `/ensure` accept a `context_hint` to
   preserve the existing client-context feature after split? (Drift folds it in today.)

---

> **PLAN ONLY.** This repository currently contains no implementation beyond this plan and
> the scaffold `AGENTS.md`. Do not deploy, do not commit secrets, do not activate units. The
> next deliverable is Phase 1 (bootstrap skeleton) once `git init` + F4-critical path are
> agreed.
