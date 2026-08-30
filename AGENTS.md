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