# AGENTS.md — caretaker-llamacpp (repo: `caretaker-llamacpp`)

> Canonical AI-agent context voor dit repo. **Eerst lezen.**
> Claude Code: `CLAUDE.md` → hier. Goose: `.goosehints` → hier.
> Status: **Phase A–D GEMERGED (2026-08-29); fase E (multi-host/Windows) + gateway-wiring open — zie `./PLAN.md`.**

## Wat is dit project

`caretaker-llamacpp` is de **manager-daemon die de levenscyclus van
llama-server bezit**, per GPU-host. Spiegelbeeld van Guardian: *guardian* =
gatekeeper (gateway/proxy-laag), *caretaker* = verzorger/onderhouder (de
wrapper naast llama-server die hem spawns, bewaakt en unloadt).

Ontstaan uit het opsplitsingsplan `GATEWAY_MANAGER_SPLIT.md` (operator-besluit
2026-08-26): de `local`-levenscyclus moet uit Guardian en als eigen proces
naast llama-server draaien, zodat **alles wat modellen serveert een provider
is** en Guardian puur de gateway + logger wordt.

## Waar dit project onderdeel van is

Dit repo is **niet op zichzelf staand** — het is **fase F5 van de Guardian 2.0
masterplan** (issue #1 in `m0nklabs/guardian-llmprovider-gateway`,
`docs/IMPLEMENTATION_PLAN.md`, canoniek). Fasevolgorde F0–F7:

- **F0–F4 (GEMMERGED):** foundation/rename, CI, per-provider config (F2),
  local als managed provider (F3), en **F4 = registry/keuze/discovery
  ontdraaien uit `engine/manager.py`** → de gateway-laag
  (`app/local_inference/model_registry.py`, `ModelRegistry`). Die logica
  **blijft definitief in de gateway** — dit repo neemt hem NIET over.
- **F5 (DIT REPO, nu):** caretaker-daemon + local passief. De gateway wordt
  een passieve provider-entry (`base_url: http://127.0.0.1:11440/v1`,
  `management_url: http://127.0.0.1:11441`, `managed: false`) en praat via de
  control-API (`:11441`, `CARETAKER_KEY`-Bearer) met deze manager.
- **F6 (daarna):** Windows/14700K-provider (NSSM; elke manager leest zijn
  EIGEN `models.local.settings.yaml`).
- **F7 (slot):** cut-over naar de nieuwe dir + legacy bevriezen.

**Gateway-wiring-blauwdruk (essentieel voor fase-1/2-contract):**
`~/guardian-llmprovider-gateway/docs/F5_GATEWAY_WIRING_ANALYSIS.md` — kaart
van alle lifecycle-aanroep-sites in de gateway (traffic/background/admin/
discovery), wat verhuist (`load`/`switch_model`/`unload`, health-verificatie)
en wat NIET verhuist (registry/keuze, `is_switch_allowed`, idle-unload-
beslissing, admin/status/metrics-readouts) + risico's die het API-contract
bepalen (`POST /ensure`, `POST /unload`, `GET /status`, allen Bearer).

**Review-werkstroom (zelfde cultuur als guardian):** dit repo heeft
`.github/workflows/pr-piet.yml` (org-reusable `m0nklabs/pr-piet`, tier-1
deepseek + optionele tier-2; `single_call_review: true` experimenteel). Na
**elke laatste commit** → `gh pr comment <n> --body "/review"` (mens-account
`m0nk111`). **Merge-criterium (operator): mergeen pas als er GEEN openstaande
bevindingen/threads uit de review zijn** (weerlegd+beantwoord telt niet);
geen auto-approve/auto-merge — human merge. Concurrency-cancel van de
auto-`pull_request`-run is een bekend patroon → herstel met
`gh run rerun <run-id> --failed`. **PR's van `ecc-tools[bot]` (ECC-bundle,
bv. PR #2) NIET aanraken** — die komen van een externe bot-workflow.

## Status (2026-08-28)

- **Bootstrap-fase GEDAAN (2026-08-28, PR #1 `f5-bootstrap` — review-loop
  actief).** Skeleton van PLAN.md §1: `pyproject.toml` (package `caretaker`,
  Python >=3.12), `caretaker/` (`__main__.py` uvicorn-runner,
  `config.py` loader, `manager.py`-skeleton, `server.py` met de 3
  control-routes + auth-gate), `deploy/systemd/` (Linux-template; Windows
  NSSM), `tests/test_bootstrap.py` (**12 tests groen**), `requirements.txt`
  + `.github/workflows/python-ci.yml` (org-reusable, python 3.12 — de
  self-hosted pool heeft de deps daar pre-installed; de reusable doet
  `pip install --no-deps -r requirements.txt`).
  - **Auth-gate:** leest `CARETAKER_KEY` uit env; zonder → 503, foute key →
    401, constant-time via `hmac.compare_digest` op UTF-8-bytes (non-ASCII
    key veilig). HTTP-headers zijn ASCII-only — non-ASCII client-token is
    fysiek onmogelijk.
  - **Config-loader:** `load_models_config()` uit `models.local.settings.yaml`
    (pad via `CARETAKER_MODELS_FILE`); foutafhandeling volledig →
    `ModelsConfigError` (missing/OSError/UnicodeDecodeError/YAMLError + type-
    validatie: top-level mapping, `models`/`aliases` mappings).
  - **Bind host/port configureerbaar:** `CARETAKER_HOST`/`CARETAKER_PORT`,
    default loopback `127.0.0.1:11441` (test-pinned; remote-gateway F6 zet
    LAN-interface).
- **Fases A–E (PLAN.md §2–§6):** **Phase A–D GEMERGED — zie hieronder**; E (multi-host/Windows) + de gateway-wiring (§7) staan nog open. De lifecycle-kern
  (~1050 regels) verhuist uit
  `guardian-llmprovider-gateway/app/engine/manager.py`:
  A = lifecycle core (spawn/stop/reload + args-bouw byte-identiek), B = drift,
  C = watchdog + VRAM ✅, D = idle-unload-contract ✅, E = multi-host/Windows.
- **Phase B (drift-contract) GEMERGED (2026-08-29, PR #4 → main, squash-commit `d0bc1d7`):**
  `POST /ensure` is de correcte idempotente repair-primitive (drift her-detecteren →
  reload; niet gedrift → no-op) en `GET /status` rapporteert `needs_reload` voor
  discovery. `Caretaker.check_drift()` (publiek, pure wrapper over `_config_drifted`,
  ValueError op onbekend model); `health()` met `needs_reload`. `/ensure`: 200
  `{ok, loaded_model}`, 404 `model_not_found`, 503 `model_load_failed` + `crash_details`,
  422 `invalid_request` — **top-level error-bodies (geen `detail`-wrapper)** zodat de
  gateway direct op `error` kan branchen. `server.init(manager)` voor test-injectie +
  lazy `_manager()` singleton; `/unload` blijft 501 (Phase C); auth-gate intact.
  **Review-les ronde 1 (2 terecht):** (a) `health()` mag drift NIET tegen de loaded
  runtime-vision-mode vouwen — `_resolve_runtime_vision_flag` resolved `enable_vision=None`
  voor het actieve model naar `current_vision_enabled`, dus zélfs `check_drift(model)`
  maskeert een mmproj-config-wijziging (bewezen: args-hash bleef identiek). Fix =
  interne `config_vision`-parameter op `_compute_launch_signature`/`_config_drifted` die
  de config-*default* forceert (mmproj-aanwezigheid; bypass loaded-mode-shortcut);
  `health()` → `_config_drifted(model, enable_vision=None, config_vision=True)`.
  (b) `bool` is een `int`-subklasse → `context_hint: true` glipte door validatie; nu
  expliciet afgewezen (422). Beide gepind door regressietests. **54 tests totaal, ruff
  clean.**
- **Phase C (watchdog + VRAM-slot + /unload) GEMERGED (2026-08-29, PR #5 → main, squash-commit `747d8fb`):**
  - **`caretaker/vram.py` (nieuw):** `VramScheduler` (guardian-port uit
    `app/local_inference/models.py`) + `get_model_size_mb` (config `size_mb` > naam-heuristics
    > 0). De scheduler houdt `active_sizes` bij **per acquire** (niet herberekend uit de naam —
    een geconfigureerde `size_mb` zou anders als 0 tellen en de budget-check liegen).
    Acquire blokkeert op `asyncio.Condition` tot de som van actieve sizes + nieuwe ≤ limit;
    `release` notificeert. `VramLimitExceededError(ValueError)`: een model dat alleen al
    > budget is faalt nu fast-forward i.p.v. eeuwig te wachten.
  - **watchdog:** `start_watchdog()/stop_watchdog()` (idempotent, **niet auto-startend** —
    de F5-deploy-wiring start hem expliciet), `_watchdog_tick` (testbare kern: health-probe →
    crash-record → restart met backoff; verdubbelt op `ModelLoadError` tot `max_backoff`),
    `_watchdog_loop` (achtergrondtaak). `_switch_in_progress`-race-guard tegen concurrent
    loads. `_watchdog_retry_model`: na een mislukte force-restart (switch_model wist
    current_model) blijft de tick hetzelfde model herstarten met backoff.
  - **`switch_model(force=True)`:** skipt de no-op-fast-path — de watchdog moet een dode
    backend kunnen herstarten óók bij zelfde model + geen drift.
  - **VRAM-slot in switch_model:** `vram_limit_mb` (config-veld `vram_limit_mb` in
    models.yaml root of constructor) + `check_vram`-kill-switch. Een swap A→B released A's
    slot **vóór** acquire(B) (anders deadlock: ruimte die alleen de geblokkeerde switch zelf
    kon vrijgeven); same-model reload/restart behoudt het slot (no double-acquire); de
    except-tak herstelt A's slot bij een mislukte swap vóór de stop (slot-leak) óf released
    model_name ná de stop. `_switch_in_progress` + `build_runtime_config` leven **in** de try,
    zodat een raise daar de flag altijd cleart (stuck-flag → watchdog permanent uit).
  - **`POST /unload`** → `manager.unload()` (idempotent): 200 `{ok, is_unloaded}`, 500
    `unload_failed`; alle routes live sinds C (geen 501-stub meer — bootstrap auth-test
    bewijst de gate nu via dummy-manager-injectie). `/ensure` kent nu ánders:
    503 `vram_limit_exceeded` (geen crash_record) vóór 404 `model_not_found` — let op de
    **except-volgorde** (subclass vóór base).
  - **Review rondes: ronde-1 3 terecht (deadlock, acquire-fail-fast, one-shot-retry) +
    ronde-2 2 terecht (slot-leak, stuck-flag) — alle 5 in 2 fix-commits (`4dee835`,
    `2b8adea`) met regressietests. 76 tests totaal, ruff clean.**
- **Phase D (idle-unload-contract) GEMERGED (2026-08-29, PR #6 → main, squash-commit `b4ef3d1`, review schoon — 0 threads):**
  - `health()`/`GET /status` rapporteert nu `loaded_at` + `idle_since` (unix-epoch van de
    laatste succesvolle load; beide `null` na unload/stop). **De caretaker ziet geen
    requestverkeer** — dit is een load-age-proxy; de gateway combineert `idle_since` met
    zijn eigen `last_request_time` vóór hij `POST /unload` roept (beslissing in gateway).
  - Het **unload→ensure-herstelcontract** is API-level gepind: `POST /unload` → `/status`
    is_unloaded=true → `POST /ensure {model}` → 200 + loaded_model → `/status` loaded —
    exact de 503 → /ensure-recovery-pad die de gateway-wiring straks gebruikt.
  - VRAM-accounting blijft waarheidsgetrouw over herhaalde unload→ensure-cycli (geen
    slot-leak, geen spurious crashes). Test `test_ensure_recovers_after_unload_api` pinnt
    de restart-assert (start-count ≥ 2, ensure is een echte reload, geen no-op).
  - **81 tests totaal, ruff clean.**
- **Ensure-response-contract verrijkt (2026-08-30, PR — gateway `caretaker_runtime`-tranche 2 is gemerged, guardian PR #12 → squash `1402b10`):**
  de gateway's remote-first hotpath consumeert nu drie daemon-geautoriseerde
  velden uit het `/ensure`-ANTWOORD i.p.v. gateway-side heuristiek (probes,
  flag-detectie): `switch_model` retourneert een bool `fresh_load` (True =
  echte (re)load — de backend is vers gestart en in-memory sessie-state is
  weg; False = no-op fast-path — de live sessie is authoritatief, restore
  zou die overschrijven), en het 200-antwoord is nu
  `{ok, loaded_model, fresh_load, vision_enabled, needs_reload}` waar
  `vision_enabled` de daemon's eigen resolutie is van de flag waarmee het
  geladen process draait (authoritatief boven gateway-probes). Zodra de
  gateway tegen deze daemon praat, activeert zijn `supports_fresh_load`-gate
  (re-gevalideerd op elke 200) automatisch de context-restore, en de
  daemon-confirmed `vision_enabled` wint boven de lokale mmproj-probe.
  **Review-les (1 terecht):** de no-op fast-path liep "already active" terug
  zonder de backend-health te checken — een crash tussen watchdog-ticks kan
  `current_model` gezet achterlaten met een DODE llama-server, en /ensure zou
  dan liegen (`ok: true, fresh_load: false` op een dode backend). Fix: de
  no-op-gate eist nu een geslaagde `server_process.health_ok()`; bij een dode
  backend wordt de no-op geweigerd en herlaadt de ensure de backend (healing,
  fresh_load=True). Gepind door `test_switch_model_noop_refused_when_backend_dead`.
  **Gepind:** `test_switch_model_returns_fresh_load_semantics` (no-op False,
  cold load True, reload-na-unload True) + 
  `test_ensure_response_carries_fresh_load_and_vision_contract` (API-level:
  ensure → fresh_load True → herhaald ensure → False → unload→ensure → True).
  **84 tests totaal, ruff clean.**
- **Strict /ensure-modelverificatie (2026-09-01-incident, branch `f6-phase-e-windows-process`):**
  het 2026-09-01-incident (verkeerde launcher draaide na de stop/start het OUDE
  model; de warn-only-verificatie logde de mismatch maar `/ensure` antwoordde
  tóch 200 "model loaded" — ook op de daaropvolgende no-op-ensure) is gefixt in
  de verificatie zelf. `_verify_backend_model` (warn-only, fail-open) is
  vervangen door **strict** `_verify_loaded_model(model)`: GET `/props` met
  korte timeout (`DEFAULT_PROPS_TIMEOUT = 5.0`, nieuwe constante; /health wordt
  eerst bounded gepolld), vergelijkt `model_path` (fallback `model`) met de
  geconfigureerde `path` van het **opgevraagde** model; de HTTP-aanroep zit in
  `_fetch_props` (het enige mockpunt voor tests). Na élke echte (re)load ÉN op
  de no-op-fast-path ("already active") moet /props het juiste model bewijzen;
  bij mismatch na een (re)start volgt **exact één** gebonden retry (nog een
  stop/start-cyclus + wait + verify, `_retry_verified_start`; de args-file
  staat er al, de launcher leest hem bij de start opnieuw) en nog een mismatch
  → nieuwe `ModelMismatchError` (model/expected/actual) → `/ensure` **503**
  `{"error": "model_mismatch", "message", "expected", "actual", "model"}` —
  nooit succes. De no-op-path weigert bij mismatch de no-op en valt door naar
  de echte (heal-)reload (zelfde patroon als de dode-backend-weigering van
  2026-08-30); blijft de backend daarna fout → 503. Bij een definitieve
  mismatch wordt de active-model-bookkeeping gewist (`current_model=None`,
  vision=False → `/status` rapporteert needs_reload=true; VRAM-slot wordt in de
  except-tak released). `/props` onbereikbaar/niet-parseerbaar telt óók als
  verificatiefout (**fail-closed**, `actual: null`) — de oude fail-open was de
  incident-kern. 200-body ongewijzigd backward-compat
  (`{ok, loaded_model, fresh_load, vision_enabled, needs_reload}`); auth
  ongewijzigd; `/unload`//`/status`//`/health` onaangetast; onbekend model
  blijft 404 `model_not_found` (nu gepinnd: géén props-call vóór validatie).
  **Latent-items: ALLE DRIE opgelost** — (2) heal-reload context-save onder de
  doelmodel-naam gefixt in PR #9 (`0c19ed7`, `_skip_next_context_save`-guard);
  (1) alias-resolutie + (3) `_loaded_at`-oddity gefixt in PR #10 (`f0bdeb6`):
  `resolve_model_alias()` (bounded walk) aan de switch_model-ingang maakt elke
  naam uit de host-config adresseerbaar (gateway blijft F4-alias-autoriteit;
  double resolution is harmloos), en de failed-switch-path cleared nu
  `_loaded_at` + de skip-flag (truthful health(), geen phantom save-skip).
  **101 tests groen (10 nieuw: `tests/test_ensure_verification.py`), ruff
  clean** (incl. opkuizen van de pre-existing I001-importsortering in
  manager.py — importblok was identiek op HEAD).
- **Latent-items PR #10 (2026-09-02, gemerged):** alias-resolutie +
  failed-switch bookkeeping — 6 pins in `tests/test_latent_items.py`; suite
  112 passed; live: /ensure met alias `qwen3.8-uncensored` → 200 in 0,08 s
  (no-op, canonieke naam gerapporteerd).
- **Plan-only. Niks gebouwd, repo leeg** = VERLEDEN — zie boven.

### Phase A (lifecycle core) — status

- **`caretaker/paths.py` (nieuw):** centrale deployment-literals met env-override
  (gelijk aan guardian `app/paths.py`-patroon): `CONFIG_DIR`
  (`CARETAKER_CONFIG_DIR`, default `<repo>/config`), `CURRENT_MODEL_{ARGS,ENV,SIG}_FILE`,
  `LLAMA_SLOTS_DIR` (`CARETAKER_LLAMA_SLOTS_DIR` → valt terug op
  `GUARDIAN_LLMPROVIDER_GATEWAY_SLOTS_DIR` → `~/llama_slots`), `LLAMA_SERVER_BIN`
  (`LLAMA_SERVER_BINARY`/`LLAMA_CPP_OFFICIAL_ROOT`, default
  `~/llama_cpp_official/build/bin/llama-server`), `SERVER_URL`
  (`CARETAKER_SERVER_URL`, default `http://127.0.0.1:11440`), `SYSTEMD_SERVICE`
  (`CARETAKER_SYSTEMD_SERVICE`, default `llama-server`). **Call-time helpers**
  `llama_slots_dir()`/`server_url()` lezen env op aanroep-tijd (tests zetten env
  na import — de module-constanten zouden die anders niet honoreren).
- **`caretaker/manager.py`:** `Caretaker`-klasse krijgt `config_path`-injectie
  (default `CARETAKER_MODELS_FILE`) én `server_process`-injectie (default
  `SystemdServerProcess`). Geen module-globale `manager = Caretaker()`-singleton
  (import is veilig zonder models-file; server.py instantieert in Phase C). Overgezet
  gedrag-neutraal: `_resolve_vision_mmproj`/`_resolve_runtime_vision_flag`/
  `_resolve_runtime_value`/`build_runtime_config` (van `model_registry.py`),
  `_build_args_string`→`(str, env_dict)` (**byte-identiek aan guardian**, `<repo>`/
  host/port/slots via paths), `_write_server_args`, `_save_context`/`_load_context`,
  `_free_gpu_memory`/`_get_comfyui_url` (uit `services.comfyui_url` of
  `CARETAKER_COMFYUI_URL`)/`_request_comfyui_free`, `_compute_launch_signature`/
  `_read_persisted_signature`/`_write_persisted_signature`/`_config_drifted`,
  `_wait_for_health` (poll `{server_url}/health`, crash-loop via
  `server_process.restart_count()`/`is_failed()`), `_detect_crash`/`_extract_crash_error_from_lines`,
  `_verify_backend_model` (vereenvoudigd: `/props`-path-check, warn-only), `switch_model`
  (zonder registry/keuze/pinning/switch-allowlist), `unload` (double-unload-guard incl.
  `current_model=None`).
- **`ServerProcess`-interface** (in `manager.py`): ABC met `start`/`stop`/`health_ok(url)`/
  `restart_count`/`is_failed`/`crash_error`/`service_exit_code`. `SystemdServerProcess` =
  `sudo systemctl start|stop <service>` via `create_subprocess_exec`; `DirectServerProcess`
  spawns `LLAMA_SERVER_BIN` met arg-array uit `current_model.args` (nieuwe process-group,
  `os.killpg` bij stop; Phase E/Windows-voorloper). Health/crash-introspectie loopt via de
  interface (Direct geeft 0/False/"Unknown").
- **`caretaker/config.py`:** `load_models_config(config_path=None)` (optionele
  path-injectie, default-behavior ongewijzigd) + `comfyui_url(config_path=None)` —
  de 12 bootstrap-tests blijven groen.
- **`tests/test_phase_a.py` (nieuw, 11 tests):** args-goldens (full/minimal/vision-override),
  apples-to-apples guardian-cross-check (in-process import, skip zonder guardian-repo),
  fake-`ServerProcess` (no-op, stop→start→health-flow, `ModelLoadError` bij health-fail,
  unknown-model guard), `unload`-guard. **Test-tip:** `patch_paths`-fixture monkeypatcht
  `CURRENT_MODEL_*` naar tmp — anders schrijft switch_model naar het echte
  `<repo>/config/`-dir.
- **Weetje (gedrag neutraal):** guardian `build_runtime_config` op een `{path}`-ook-model
  geeft `context=None`/`ngl=None` (injecteert géén default), dus `_build_args_string`
  produceert `-c None -ngl None` — dat IS de guardian-identieke output (de `4096`/`99`-
  default geldt alleen bij afwezige key). Golden-test pinnt dit bewust.

## Scope — wat hoort WEL hier (alleen de lifecycle-kern)

Ontleed uit `engine/manager.py` (~1637 regels), hard geteld:

| Regels | Categorie | Hoort waar? |
|---|---|---|
| ~1050 | **Echte lifecycle**: spawn/stop/restart, args-bouw, drift-detectie, health-check, crash-detectie + auto-restart, unload, ComfyUI VRAM vrijmaken, context-save/restore | **Hier (caretaker)** |
| ~40 | VRAM-slot-acquisitie (`VRAMScheduler.acquire/release`) | Hier |
| ~40 | `reload_backend_after_connect_error` (herstel-pad) | Hier |
| ~25 | Idle-unload watcher | Hier, MAAR verweven met verkeer (contract met gateway) |
| ~509 | Registry/keuze/discovery (`resolve_model`, preferred tool/reasoning model, vision-cache, context-metadata, public model map) | **Blijft in de gateway — F4 GEDAAN** (`ModelRegistry`, `app/local_inference/model_registry.py`) |
| ~130 | Resolutie/sizes/timeouts (`local_inference/models.py`) | Gateway |
| ~78 | Settings lezen (`_load_aliases`, `_load_switch_allowlist`, `_load_config`) | Gedeeld — leest dezelfde YAML, geen kopie |

**Principe: settings zijn geen manager-werk.** `models.local.settings.yaml`
is de enige gedeelde bron; de caretaker leest hem alleen (geen keuze-
logica/registratie kopiëren).

## Design-contract (uit het plan)

Dunne control-API, eigen proces naast llama-server op poort **11441**:

```
GET  /status            → geladen model + gpu/vram-status
POST /ensure {model}    → laad model (VRAM-slot, swap); idempotent
POST /unload            → unload (of zelf na N minuten — met verkeers-input)
(OpenAI-inferentie-API blijft rechtstreeks llama-server: http://127.0.0.1:11440/v1)
```

**Security (operator-besluit 2026-08-27): per-caretaker één eigen key.** Elke
control-call (`/status`, `/ensure`, `/unload`) vereist
`Authorization: Bearer ${CARETAKER_KEY}`; zonder geldige key → 401. Elke
GPU-host heeft zijn eigen key (net als cloud providers bij Guardian): de
gateway slaat die per host op in de provider-entry
(`config/providers/<host>-local.settings.yaml`, `api_key: ${...}`, nooit
committen). De caretaker leest de key uit env/secret, nooit uit de repo.
LAN-IP-allowlist mag aanvullend, nooit vervangend.

Contract gateway ↔ manager:

- Gateway roept vóór een forward optioneel `POST /ensure {model}` aan;
  daarna `POST /v1/chat/completions` op llama-server.
- Manager kan zelf een swap triggeren (idle-unload) → gateway vangt
  404 `model_not_loaded` / 503 af en retryt met `ensure`.
- **Verkeers-input:** gateway geeft actieve request/queue-aantallen door;
  de idle-beslissing mag bij de gateway blijven, uitvoering in de manager.
- `GET /status` voedt de discovery van de gateway (manager is bron van
  waarheid voor de lokale GPU).
- In de gateway wordt `local` een passieve provider-entry:
  `base_url: http://127.0.0.1:11440/v1`, `management_url: http://127.0.0.1:11441`,
  `managed: false`.

## Deployment-topologie (operator 2026-08-26)

**De manager is per GPU-host** (niet alleen lokaal):

```
ai-kvm-2 (Linux, GPU #1)              14700K (Windows, GPU #2)
  gateway + caretaker                    caretaker (eigen proces)
  llama-server :11440                    llama-server.exe (CUDA) :11440
  management_url :11441  ◄── HTTP ──►  management_url :11441 (192.168.1.x)
```

- Gateway alleen op ai-kvm-2, praat met beide managers via `management_url`.
- **Windows:** geen systemd → NSSM/scheduled task; elke manager leest zijn
  EIGEN `models.local.settings.yaml` (GGUFs met Windows-paden, geen kopie
  van de Linux-lijst).
- `providers.settings.yaml` op ai-kvm-2 verwijst naar beide management_urls
  (`http://192.168.1.35:11441` + `http://192.168.1.x:11441`).

## Omgeving (feitelijk, geverifieerd)

- **llama-server binary:** `~/llama_cpp_official/build/bin/llama-server`
  (build uit de officiële clone `~/llama_cpp_official`).
- **Huidige lokale llama-server:** systemd-unit `llama-server.service`
  (feitelijk nog legacy-pad tot F7 cut-over:
  `WorkingDirectory=/home/flip/llama_cpp_guardian/scripts`,
  `ExecStart=scripts/start_llama.sh`), poort `127.0.0.1:11440`.
  Start-args vandaag: `-c 262144 -ngl 99 -ctk q4_0 -ctv q4_0 --host 127.0.0.1
  --port 11440 --slot-save-path ~/llama_slots --no-mmap --tensor-split
  0.57,0.43 -nkvo --parallel 4` (default model
  `Qwen3.6-35B-A3B-Uncensored-Aggressive.i1-Q4_K_M.gguf`).
- **Slots/context-save:** `~/llama_slots/` (`auto_save_*`-bestanden).
- **Guardian (de gateway-kant):** systemd-unit `llama-guardian.service`,
  venv `~/guardian-llmprovider-gateway/venv`, nginx-mux op `:11434`/`:11435`/`:11436`.
- **Caretaker-controlpoort:** `11441` (bind via `CARETAKER_HOST`/`CARETAKER_PORT`).
- **Zoekstack (user-basis):** searxng `127.0.0.1:28082`, kindly-web-search
  MCP `127.0.0.1:28083` — niet de kyberm0nk-containers (28080/28081) gebruiken.
- **Browser-automatisering:** Playwright MCP (headless chromium) beschikbaar.

## Critical rules / conventies

- **Testen vóór claimen:** zodra er code is: `py_compile` + pytest, nooit
  "fixed" claimen zonder verifieerbare run. Tests groen houden: huidige suite
  12 tests — bij elke wijziging volledig draaien (`./venv/bin/python -m pytest
  tests/ -q -p no:cacheprovider`).
- **Geen hardcoded variabelen.** Paden/poorten/namen/timeouts komen in
  config-YAML (`${VAR}`-expandable) of een paths-module; nooit literals
  "voor het gemak" kopiëren. Een hardcoded waarde die config omzeilt is een
  bug, geen shortcut.
- **Commit-taal:** Nederlands is prima voor operator-facing notities
  (intern project); Engels voor code, API en publieke docs.
- **Commit-identity = de modelnaam van de agent** (zelfde als guardian-repo;
  nooit overschrijven naar `PR-Piet`).
- **AGENTS.md altijd bijwerken — inclusief verse bevindingen.** Elke
  gedragsverandering, bugfix, config-wijziging én elk feit dat moest worden
  uitgezocht gaat in DEZELFDE sessie dit bestand in (vóór de commit). De
  handoff-sectie is het primaire continuïteitsmechanisme tussen sessies;
  een stale handoff is een bug. Regel van duim: moest je code/config/docs
  inspecteren om te weten hoe iets werkt → schrijf het meteen op.
- **Subagents maximaliseren (verse context).** De lead houdt het plan,
  kinderen doen mechanisch/implementatie-/meetwerk. Eén schrijver per
  cwd/worktree; serialiseer edits aan gedeelde bestanden.
- **Dit repo is een daemon-repo:** systeemunit (`caretaker-llamacpp.service`)
  + eigen logging horen erbij zodra er iets draait.
- **Niet dupliceren:** de ontwerpdetails staan in de plan-docs van het
  guardian-repo; dit bestand indexeert en verwijst, legt niet opnieuw uit.
- **PR-review-loop vóór elke merge:** na elke laatste commit `/review` posten
  (mens-account `m0nk111`), merge pas zonder openstaande bevindingen
  (weerlegd+beantwoord telt niet), human merge. ECC-bundle-PR's van
  `ecc-tools[bot]` niet aanraken.

## Directory map

```text
PLAN.md             Gefaseerd implementatieplan (fases A–E + gateway-wiring)
AGENTS.md           Dit bestand
pyproject.toml      Package `caretaker` (Python >=3.12; dev: pytest/ruff)
requirements.txt    Runtime-deps voor de org-reusable python-ci (--no-deps)
caretaker/
  __main__.py       uvicorn entrypoint (CARETAKER_HOST/PORT, default :11441)
  config.py         load_models_config(config_path?) → ModelsConfig (models/aliases)
                    + comfyui_url() (services.comfyui_url / CARETAKER_COMFYUI_URL)
  paths.py          Deployment-literals (env-override) + call-time llama_slots_dir()/server_url()
  manager.py        `Caretaker` (lifecycle core + watchdog + VRAM-slot) + ServerProcess-interface
                    (SystemdServerProcess / DirectServerProcess), CrashRecord, ModelLoadError
  vram.py           VramScheduler (guardian-port) + get_model_size_mb + VramLimitExceededError
  server.py         FastAPI: GET /status, POST /ensure, POST /unload (+ auth-gate)
deploy/systemd/
  caretaker-llamacpp.service   Linux-sjabloon (EnvironmentFile=/etc/caretaker/caretaker.env)
tests/
  test_bootstrap.py 12 tests: auth-gate (503/401/non-ASCII-key), config-fouten,
                    routes, entrypoint bind-contract
  test_phase_a.py   11 tests: args-goldens (+ guardian cross-check), fake-ServerProcess
                    switch_model / unload, comfyui_url-resolution
  test_phase_c.py   22 tests: VramScheduler (acquire/release/blokkade/size), switch+VRAM
                    integratie (swap-deadlock, same-model, fail-fast, slot-leak, stuck-flag),
                    watchdog (tick healthy/crash/backoff/retry/loop), /unload + /ensure-503-API
  test_phase_d.py   5 tests: idle-contract (loaded_at/idle_since), /status-API,
                    ensure-recovery na unload (echte restart), unload→ensure-cycli + VRAM
.github/workflows/
  pr-piet.yml       Review-loop (org-reusable m0nklabs/pr-piet)
  python-ci.yml     Org-reusable python-ci (python 3.12, src caretaker)
```

## Handoff

- 2026-08-26: repo aangemaakt (leeg, publiek: `m0nklabs/caretaker-llamacpp`).
  AGENTS.md-scaffold + **`PLAN.md`** (gefaseerd plan fases A–E + gateway-
  wiring) op basis van `GATEWAY_MANAGER_SPLIT.md` + `LAN_GPU_BACKENDS.md`
  (background worker). Operator koos (2026-08-28) **standalone repo** (niet
  monorepo-first) voor F5.
- **2026-08-28: bootstrap gebouwd + review-loop gestart.** Skeleton
  (PLAN.md §1) gebouwd door background worker; lead reviewde + commit +
  PR #1 (`f5-bootstrap`) + `/review`. PR-Piet vond 7 bevindingen, allemaal
  gefixt + beantwoord (of weerlegd): requires-python >=3.12 i.p.v. 3.14;
  OSError/UnicodeDecodeError/non-mapping-validatie in config-loader;
  UTF-8-bytes voor `compare_digest`; **`CARETAKER_HOST`/`CARETAKER_PORT`**
  configureerbaar (loopback default) voor remote-gateway (F6). CI: python-ci
  op **3.12** omdat de self-hosted pool daar de deps pre-installed heeft
  (org-reusable draait `--no-deps`). **12 tests groen**, ruff clean.
  ECC-bot maakte autonoom PR #2 (bundle) — niet aanraken.
  Volgende stap: **Phase A — lifecycle core** (`switch_model`,
  `_build_args_string`, `_write_server_args`, `ServerProcess`-interface uit
  `engine/manager.py` naar `caretaker/manager.py`, byte-identiek qua
  start-args; zie PLAN.md §2).
- **2026-08-29: Phase A (lifecycle core) geïmplementeerd.** Commits `38fb627`
  (Phase A core) + `05c7e9c` (PR-#3 review-fixes) op branch `phase-a-lifecycle`.
  - **`caretaker/paths.py`** (nieuw): deployment-literals met env-override
    (`CARETAKER_CONFIG_DIR`/`CARETAKER_LLAMA_SLOTS_DIR`→`GUARDIAN_*_SLOTS_DIR`/
    `LLAMA_SERVER_BINARY`/`CARETAKER_SERVER_URL`/`CARETAKER_SYSTEMD_SERVICE`) +
    call-time helpers `llama_slots_dir()`/`server_url()`.
  - **`caretaker/manager.py`**: `Caretaker(config_path=None, server_process=None,
    health_polls=…)` + `ServerProcess`-interface (`SystemdServerProcess`/
    `DirectServerProcess`), `CrashRecord`/`ModelLoadError`. Overgezet gedrag-
    neutraal uit guardian: `build_runtime_config`, `_build_args_string` →
    `(str, env)` **(byte-identiek; `--host/--port` uit geïnjecteerde
    `server_url`, `--slot-save-path` uit `llama_slots_dir()`, `--load-mode
    none`; `config["backend"]` wordt genegeerd)**, `_write_server_args`,
    context save/restore, ComfyUI VRAM-free, drift-signature, health/crash,
    `switch_model` (cleart `current_model` op falen — PR#3), `unload`.
  - **`caretaker/config.py`**: `load_models_config(config_path=None)` +
    `comfyui_url(config_path=None)` (bootstrap-tests blijven groen).
  - **`tests/test_phase_a.py`** (19 fase-A tests): golden-args (full/minimal/
    vision/GLM-4.7-Flash-rauw-spiegeling uit guardian `tests/unit/test_manager.py`:
    `test_writes_basic_args`-mirror + `test_write_server_args_ignores_legacy_backend_key`),
    **guardian-cross-check via de guardian-venv in een subprocess**
    (`<guardian-root>/venv/bin/python`; caretaker-venv mist guardian-deps
    psutil/schedule → in-process import is niet betrouwbaar; skip i.p.v. fail
    als guardian-venv/fixture-instantiatie faalt), fake-`ServerProcess`-flows,
    unload-guard, review-fix-testen.
  - **Verificatie:** 31 tests groen (12 bootstrap + 19 fase A), ruff clean,
    py_compile OK. Apples-to-apples: caretaker `_build_args_string` == guardian
    byte-gelijk op dezelfde fixture (ook op guardian SAMPLE-style `ctx`/`ts`-
    legacy-keys; daarom is raw-`build_runtime_config`-path `-c None -ngl None` —
    dat IS guardian-identiek).
  - **Test-isolatie-les (2026-08-29):** de switch/unload-lifecycle-tests die géén
    `patch_paths`-fixture gebruikten schreven/lazen de echte repo-`config/
    current_model.sig` (gitignored). Een achtergebleven `.sig` van een succesvolle
    switch maakte `_config_drifted` ertoe de **no-op-fast-path** te nemen →
    `test_switch_model_failure_clears_active_state` faalde ("DID NOT RAISE").
    **Fix:** élke switch/unload-test injecteert nu `patch_paths` (= per-test tmp
    `CURRENT_MODEL_*`-paden + deterministische env) zodat er nooit in de echte
    `config/` geschreven wordt; suite is daarna robuust ongeacht achterblijvers.
- **2026-08-29 ruff-gate geverifieerd (bleek al groen).** De org-reusable
  `python-ci` draait `ruff check caretaker tests` met de **default-select**
  (`[tool.ruff] target-version=py314 line-length=100`, géén eigen select →
  default = ~F/E). **`ruff check caretaker/ tests/` is schoon** (0 errors) op
  zowel ruff 0.15.15 als 0.16.5; `--select BLE001` = **0 errors** (élke
  guardian-overgenomen broad `except Exception` in `caretaker/manager.py`
  (14×) en `tests/test_phase_a.py` (1×) draagt al `# noqa: BLE001` — bewuste
  guardian-parity keuze, NIET per `--fix` weggespoeld); `--select F` (F401/
  F841/F821/unused) = **0 errors**. Enige risico = een toekomstige ruff die
  BLE001 (of `--select ALL`-regels: S101/COM812/G004/PLR2004/ARG001/…) wél in
  de default neemt — **bewust géén NoDeviate/ignore voor die** zolang de CI
  default-select draait; als de reviewer `--select ALL` wil, zijn er 289 issues
  (52 fixable, géén BLE) — dat is dan een aparte selectiebeslissing.
- **2026-09-01 (incident-fix): strikte /ensure-modelverificatie** op branch
  `f6-phase-e-windows-process` (voltekst → Status-entry "Strict
  /ensure-modelverificatie"): `_verify_loaded_model` strict via /props +
  `DEFAULT_PROPS_TIMEOUT`, één gebonden stop/start-retry bij mismatch
  (`_retry_verified_start`), no-op-path verifieert óók en healt via
  fall-through, definitieve mismatch → `ModelMismatchError` → 503
  `model_mismatch` {expected, actual, model}. Launcher-split-brain zelf was al
  gefixt in `34dec2b`. Tests: `tests/test_ensure_verification.py` (10 nieuw,
  101 totaal). Volgende stap: PR aanmaken + `/review`; optioneel de gateway
  (`caretaker_runtime.py`) het `model_mismatch`-errorveld expliciet laten
  consumeren i.p.v. generieke 503-afhandeling.

## References

- **Dit repo: gefaseerd plan** → `./PLAN.md` (canoniek voor caretaker-werk)
- **Gateway-wiring-analyse (F5-contract)** →
  `~/guardian-llmprovider-gateway/docs/F5_GATEWAY_WIRING_ANALYSIS.md`
- Split-plan (bron): `~/guardian-llmprovider-gateway/docs/GATEWAY_MANAGER_SPLIT.md`
- Provider-unificatie: `~/guardian-llmprovider-gateway/docs/LAN_GPU_BACKENDS.md`
- Per-provider config: `~/guardian-llmprovider-gateway/docs/CONFIG_PROVIDER_FILES.md`
- Masterplan (issue #1 in het guardian-repo):
  `~/guardian-llmprovider-gateway/docs/IMPLEMENTATION_PLAN.md`
- Guardian (huidige implementatie, alles draait daar nu):
  `~/guardian-llmprovider-gateway/AGENTS.md`

---

## 2026-11-18 preservation — superseded PLAN.md snapshot

Verbatim pre-issue-24 plan, including the original dated plan-only claims.
These are historical, not the active roadmap or deployment evidence.

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

**Security (operator decision 2026-08-27):** the caretaker control API is
**authenticated with a single per-caretaker key**, mirroring how Guardian
connects to cloud providers. **Each caretaker gets its own key** (one per
GPU host) — the gateway stores it in the corresponding provider entry
(`config/providers/<host>-local.settings.yaml`, `${VAR}`-expanded, never
committed). Every `/status`, `/ensure`, `/unload` requires
`Authorization: Bearer ${CARETAKER_KEY}`; all others are rejected (401).
The caretaker reads its key from env/secret (never from the repo). A LAN-IP
allowlist may complement this but never replaces the key. This is primarily
an implementation in the **caretaker repo**; the gateway only carries the key
per provider entry.

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

---

> **PLAN ONLY.** This repository currently contains no implementation beyond this plan and
> the scaffold `AGENTS.md`. Do not deploy, do not commit secrets, do not activate units. The
> next deliverable is Phase 1 (bootstrap skeleton) once `git init` + F4-critical path are
> agreed.


## 2026-11-18 preservation — replaced AGENTS.md sections

Verbatim replaced sections in original order (including historical claims and
superseded rules). Current global/repository rules supersede this archive.

## Wat is dit project

`caretaker-llamacpp` is de **manager-daemon die de levenscyclus van
llama-server bezit**, per GPU-host. Spiegelbeeld van Guardian: *guardian* =
gatekeeper (gateway/proxy-laag), *caretaker* = verzorger/onderhouder (de
wrapper naast llama-server die hem spawns, bewaakt en unloadt).

Ontstaan uit het opsplitsingsplan `GATEWAY_MANAGER_SPLIT.md` (operator-besluit
2026-08-26): de `local`-levenscyclus moet uit Guardian en als eigen proces
naast llama-server draaien, zodat **alles wat modellen serveert een provider
is** en Guardian puur de gateway + logger wordt.



## Waar dit project onderdeel van is

Dit repo is **niet op zichzelf staand** — het is **fase F5 van de Guardian 2.0
masterplan** (issue #1 in `m0nklabs/guardian-llmprovider-gateway`,
`docs/IMPLEMENTATION_PLAN.md`, canoniek). Fasevolgorde F0–F7:

**Review-werkstroom (zelfde cultuur als guardian):** dit repo heeft
`.github/workflows/pr-piet.yml` (org-reusable `m0nklabs/pr-piet`, tier-1
deepseek + optionele tier-2; `single_call_review: true` experimenteel). Na
**elke laatste commit** → `gh pr comment <n> --body "/review"` (mens-account
`m0nk111`). **Merge-criterium (operator): mergeen pas als er GEEN openstaande
bevindingen/threads uit de review zijn** (weerlegd+beantwoord telt niet);
geen auto-approve/auto-merge — human merge. Concurrency-cancel van de
auto-`pull_request`-run is een bekend patroon → herstel met
`gh run rerun <run-id> --failed`. **PR's van `ecc-tools[bot]` (ECC-bundle,
bv. PR #2) NIET aanraken** — die komen van een externe bot-workflow.



## Status (2026-08-28)

## Scope — wat hoort WEL hier (alleen de lifecycle-kern)

Ontleed uit `engine/manager.py` (~1637 regels), hard geteld:

| Regels | Categorie | Hoort waar? |
|---|---|---|
| ~1050 | **Echte lifecycle**: spawn/stop/restart, args-bouw, drift-detectie, health-check, crash-detectie + auto-restart, unload, ComfyUI VRAM vrijmaken, context-save/restore | **Hier (caretaker)** |
| ~40 | VRAM-slot-acquisitie (`VRAMScheduler.acquire/release`) | Hier |
| ~40 | `reload_backend_after_connect_error` (herstel-pad) | Hier |
| ~25 | Idle-unload watcher | Hier, MAAR verweven met verkeer (contract met gateway) |
| ~509 | Registry/keuze/discovery (`resolve_model`, preferred tool/reasoning model, vision-cache, context-metadata, public model map) | **Blijft in de gateway — F4 GEDAAN** (`ModelRegistry`, `app/local_inference/model_registry.py`) |
| ~130 | Resolutie/sizes/timeouts (`local_inference/models.py`) | Gateway |
| ~78 | Settings lezen (`_load_aliases`, `_load_switch_allowlist`, `_load_config`) | Gedeeld — leest dezelfde YAML, geen kopie |

**Principe: settings zijn geen manager-werk.** `models.local.settings.yaml`
is de enige gedeelde bron; de caretaker leest hem alleen (geen keuze-
logica/registratie kopiëren).

## Design-contract (uit het plan)

Dunne control-API, eigen proces naast llama-server op poort **11441**:

```
GET  /status            → geladen model + gpu/vram-status
POST /ensure {model}    → laad model (VRAM-slot, swap); idempotent
POST /unload            → unload (of zelf na N minuten — met verkeers-input)
(OpenAI-inferentie-API blijft rechtstreeks llama-server: http://127.0.0.1:11440/v1)
```

**Security (operator-besluit 2026-08-27): per-caretaker één eigen key.** Elke
control-call (`/status`, `/ensure`, `/unload`) vereist
`Authorization: Bearer ${CARETAKER_KEY}`; zonder geldige key → 401. Elke
GPU-host heeft zijn eigen key (net als cloud providers bij Guardian): de
gateway slaat die per host op in de provider-entry
(`config/providers/<host>-local.settings.yaml`, `api_key: ${...}`, nooit
committen). De caretaker leest de key uit env/secret, nooit uit de repo.
LAN-IP-allowlist mag aanvullend, nooit vervangend.

Contract gateway ↔ manager:

- Gateway roept vóór een forward optioneel `POST /ensure {model}` aan;
  daarna `POST /v1/chat/completions` op llama-server.
- Manager kan zelf een swap triggeren (idle-unload) → gateway vangt
  404 `model_not_loaded` / 503 af en retryt met `ensure`.
- **Verkeers-input:** gateway geeft actieve request/queue-aantallen door;
  de idle-beslissing mag bij de gateway blijven, uitvoering in de manager.
- `GET /status` voedt de discovery van de gateway (manager is bron van
  waarheid voor de lokale GPU).
- In de gateway wordt `local` een passieve provider-entry:
  `base_url: http://127.0.0.1:11440/v1`, `management_url: http://127.0.0.1:11441`,
  `managed: false`.



## Deployment-topologie (operator 2026-08-26)

## Omgeving (feitelijk, geverifieerd)

- **AGENTS.md altijd bijwerken — inclusief verse bevindingen.** Elke
  gedragsverandering, bugfix, config-wijziging én elk feit dat moest worden
  uitgezocht gaat in DEZELFDE sessie dit bestand in (vóór de commit). De
  handoff-sectie is het primaire continuïteitsmechanisme tussen sessies;
  een stale handoff is een bug. Regel van duim: moest je code/config/docs
  inspecteren om te weten hoe iets werkt → schrijf het meteen op.

- **Niet dupliceren:** de ontwerpdetails staan in de plan-docs van het
  guardian-repo; dit bestand indexeert en verwijst, legt niet opnieuw uit.
- **PR-review-loop vóór elke merge:** na elke laatste commit `/review` posten
  (mens-account `m0nk111`), merge pas zonder openstaande bevindingen
  (weerlegd+beantwoord telt niet), human merge. ECC-bundle-PR's van
  `ecc-tools[bot]` niet aanraken.

PLAN.md             Gefaseerd implementatieplan (fases A–E + gateway-wiring)

## Handoff


- Guardian (huidige implementatie, alles draait daar nu):
