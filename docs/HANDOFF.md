# HANDOFF — caretaker-llamacpp

> Cold file (niet in de prompt-cache): actuele status + sessie-overdracht. Nieuwe blokken **bovenaan** appen.
> Werkwijze → `~/.dsh/AGENTS.md` ("AGENTS.md maintenance discipline") + repo `AGENTS.md`.

## 2026-09-25 — Watchdog-wiring (de 2026-09-16-handoff) + repo-audit — DSH agent (glm-5.3-flash)

- **Wat:** de 2026-09-16-handoff ("watchdog draait op GEEN van beide hosts") is geïmplementeerd op branch `f-watchdog-wiring` → PR open. `_lifespan` in `caretaker/server.py` armt `start_watchdog()` bij startup met env-knobs `CARETAKER_WATCHDOG_{ENABLED,INTERVAL,INITIAL_BACKOFF,MAX_BACKOFF}` (defaults: aan, 15 s, backoff 5→60 s; invalid values → warning + default, nooit stil) en stopt hem in de shutdown-finally. Fail-open bij een kapotte models-config (boot blijft staan, routes tonen de fout per request); managers zónder watchdog-surface (test-doubles) worden overgeslagen.
- **Slaap-veiligheid (bewezen, niet aangenomen):** een slapende llama-server (`--sleep-idle-seconds`, windows b10964) antwoordt /health 200 + /props 200 (`is_sleeping: true`) en wordt alléén gewekt door een generatie-request — upstream `~/llama_cpp_official/tools/server/tests/unit/test_sleep.py`. De watchdog-probe kan een slapende, gezonde server nooit foutief herstarten en wekt hem niet.
- **Verificatie:** 8 pins in `tests/test_watchdog_wiring.py` (arm+stop met defaults, disable, env-overrides, invalid→default, build-failure fail-open, raisende arming fail-open, raisende stop-at-shutdown fail-open, hasattr-miss); **175 tests groen** (143 s, lokaal py3.14-venv; CI = py3.12), ruff clean op beide aangeraakte files (I001-importsort meegenomen — bleek pre-existing op HEAD). **Review-ronde 1: 1 terecht bevinding** (fail-open dekte de arming zelf niet) → try-omvanging uitgebreid + pin. **Ronde 2: 1 laag-severity bevinding** (symmetrie: een raisende `stop_watchdog()` in de shutdown-finally zou de Comfy-cleanup overslaan) → exception-guard + pin. Daarna opnieuw `/review`.
- **Ruff-debt (out of scope gelaten, volg-item):** lokale ruff 0.16.5 default-select flagt 21 pre-existing bevindingen in comfy/tts/stt + hun tests (I001/S110/ASYNC230/SIM115/SIM102/PLW0602/UP041/RET501/RUF059); CI is groen met een oudere/laxere ruff → ruff-versie pinnen of opkuisen.
- **Audit (operator-vraag "is er al iets gedaan, agents knutselen zelf?"):** watchdog-, backend-key- (`CARETAKER_BACKEND_KEY`) en OOM-classificatie-handoffs zijn **nergens geïmplementeerd**; géén ongeautoriseerd knutselwerk — lokaal (ai-kvm2) draait de service uit de repo (tree clean == HEAD `563133f`); teams-host clone op `063d419` (1 docs-commit achter) met alléén geautoriseerde host-config-drift (F6 Windows-lijst), een mode-only `start_llama.sh`-wijziging en een **stale `--api-key`-comment** in de config (niet in de echte args → de 2026-09-11-gap is niet live); GitHub: #1–#11 allemaal gemerged, alleen bot-PR #2 open.
- **Open:** human merge van de PR; daarna **activatie per host**: ai-kvm2 `systemctl restart caretaker-llamacpp` (service draait uit de repo); teams-host: clone ff-updaten + NSSM-restart (env-discipline: volledige var-lijst expliciet, zie journal 2026-09-25). Knobs hoeven niet gezet te worden (defaults = aan).
- **Volgende items uit de handoff-stapel:** backend-auth-key (`CARETAKER_BACKEND_KEY`) en OOM-classificatie in `CrashRecord` — beide nog open.

## 2026-08-30 avond (sessie-overdracht) — PR #8 (F6 Phase E, Windows ServerProcess) merge-klaar

- **Eindstatus PR #8**: OPEN op head `d6ff3e2` (branch `f6-phase-e-windows-process`), base `main` @ `6925207`. **6/6 review-threads resolved, 0 open** — mergeStateStatus `CLEAN` / `MERGEABLE`. **Merge blijft HUMAN.**
- **Review-afsluiting**: de laatste thread (single-quote-premisse op `caretaker/windows_process.py`, `PRRT_kwDOUFFhks6dji7j`) is beantwoord + geresolved: premisse weerlegd (`_build_args_string` interpoleert paden raw via f-string; geen `shlex.quote` in `caretaker/` — de builder emit nooit quotes), maar de reële gedragssplit met de POSIX-impl (posix-shlex stript operator-embedded enkele quotes, de Windows-splitter niet) is wél defensief gedicht in `d6ff3e2`: `_split_args_windows` stript nu beide quote-stijlen, test-pinned in `test_split_args_windows_keeps_backslash_paths`. Reviews-of-record op de head, beide geslaagd zonder nieuwe bevindingen: deep review `33331965956` ("No major issues detected") + `pull_request`-review `33332549005`.
- **CI-flake gediagnosticeerd, géén regressie**: Python CI run `33331064661` (head `8310151`) faalde 2× met `TimeoutError` in `tests/test_phase_c.py::test_switch_model_swap_frees_old_slot_without_deadlock` (2 s `wait_for` viel over een httpx-TCP-connect in `switch_model` → `_load_context`). Diagnose: de `isolated_paths`-fixture zet `CARETAKER_SERVER_URL=http://127.0.0.1:11440`, dus de test-manager praat tegen wat er op de runner-host op 11440 luistert — de échte productie-llama-server (CI-log: "Backend model mismatch … qwen3.8-27b…"); bij backend-belasting mist de test het 2 s-budget. Uitgesloten: PR-regressie via platform-selectie — de test injecteert `FakeServerProcess`, `_default_server_process()` wordt niet aangeroepen. Bewijs: 9× groen lokaal (3× `d6ff3e2`, 3× `8310151` via worktree, 3× pre-PR-basis `6925207` via worktree; telkens ~3,9-4,3 s), in CI al groen op `c238d84`/`8d988fe`/`5f1c424`/`d6ff3e2`, rerun van `33331064661` op **attempt 3 groen** (runner-1, 91 passed in 55,56 s; attempts 1-2 op runner-4/runner-2 met identieke signature onder belasting). **3e onafhankelijke bewijslijn (pre-existing flake)**: de gateway-journal (`guardian-llmprovider-gateway/docs/AGENT_JOURNAL.md`, genoteerd tijdens de caretaker PR #7-cyclus — de code die nu exact de pre-PR-basis `6925207` is) observeerde deze test al vóór Phase E als flaky: "faalde 1× in de volle suite, slaagde 3× daarna incl. isolatie — waarschijnlijk machine-timing (wait_for timeout=2)". **Geen speculative fix gepusht**; follow-up: HTTP-mock-backend voor de Phase-C/D swap-tests.
- **Checks op de head**: Python CI `33331954217` = 91 passed + ruff clean (groen sinds de push van `d6ff3e2`).
- **Slot-comment** op PR #8 (issuecomment-5471009832) met de eindstatus, nadien ge-edit met de gateway-journal-flake-citatie. **Let op: er staat ook een tweede merge-signaal-comment op de PR** (20:18:16 UTC, zelfde account `m0nk111` via een sibling-sessie) — inhoudelijk gelijkluidend; de operator kan beide als merge-signaal lezen.
- **Workflow-quirk**: PR-Piet triggert op élke `issue_comment` (`types: [created, edited]`) maar behandelt non-slash-comments als onbekende commands → de tier-1 job faalt met exit 3 ("Unknown command", "geen verse review") zónder een review te posten. De rode comment-runs (20:15, 20:18, en de gequeuede na de slot-comment-edit) zijn daardoor verwacht en hinderloos: comment-runs hangen niet aan de head-rollup, dus merge-state blijft `CLEAN`.
- **Open**: human merge van PR #8; daarna vervolg F6 (gateway-kant Windows/14700K) — zie `PLAN.md` §6.

## 2026-09-09 — Handoff (guardian agent, inter-repo): OOM-detectie ontbreekt in het crash-rapportage-oppervlak

**Vraag van de operator:** "rapporteert de caretaker OOM's terug aan Guardian?" Onderzoek (guardian-zijde: `app/gateway/caretaker_client.py`/`caretaker_runtime.py`; caretaker-zijde: deze repo).

**Gevonden (feit):**
- Crash-rapportage WERKT: `CrashRecord` (caretaker/manager.py:73 — timestamp/model/error_message/exit_code/config_snapshot, geschiedenis 50 diep) stroomt via de 503 `model_load_failed`-respons als `crash_details` (caretaker/server.py:172-179) naar Guardian, die het doorgeeft aan de client-503 (guardian `app/gateway/routing.py:639-650`) en logt.
- Maar: **geen OOM-classificatie** in deze repo — geen exit-code-interpretatie (137 = OOM-kill/SIGKILL), geen CUDA-OOM-herkenning in de llama-server-logscan, geen dmesg-oom-killer-cross-check. Een OOM-dood arriveert bij Guardian als generieke `model_load_failed` met een rauw exitnummer. Guardian interpreteert het exitnummer óók niet.

**Aanbeveling (concreet):** OOM-classificatie toevoegen aan `CrashRecord`: (1) exit-code-check (137/139 + `oom_score_adj`-context), (2) logscan-herkenning van CUDA-OOM ("CUDA out of memory" in de laatste N logregelen van het gestopte proces), (3) additieve velden `oom: bool` + `oom_source: "kernel"|"cuda"` in `CrashRecord.to_dict()` — protocol-additief, Guardian kan er direct mee leven (de rest van de shape blijft gelijk). Guardian-zijde kan dan de 503/capture verrijken ("vermoedelijk OOM-kill").

**Prioriteit:** middel; nuttig zodra lokale OOM-restarts zichtbaar moeten zijn in het Guardian-dashboard/capture zonder log-klimwerk.

## 2026-09-11 — Handoff (guardian agent, inter-repo): backend-auth gap — llama-server met --api-key breekt de /props-verificatie

**Context:** F6-deployment op teams-host (J:\LLMSTUFF, RTX 5060 Ti, NSSM-service `caretaker-llamacpp` — zie guardian journal "F6 gedeployed"). De plan-aanbeveling is `--api-key` op de Windows llama-server; live getest.

**Gevonden (feit, live bewijs 2026-09-11):**
- Met `--api-key <key>` in de args: de caretaker's health-wait komt wél door (llama-server exempt /health van auth), maar de strikte `GET /props` model-verificatie krijgt **401** → `/ensure` antwoordt 503 `model_mismatch`: "backend verification unavailable: GET http://127.0.0.1:11440/props failed" (expected gguf-pad, actual null). De probes sturen geen Authorization-header — de caretaker is gebouwd voor keyless backends (ai-kvm2 draait keyless).
- Zonder --api-key: `/ensure` 200 in ~8,9s, alles gezond (huidige deployment = keyless backend + firewall 11440/11441 beperkt tot 192.168.1.0/24).

**Aanbeveling:** een backend-auth-knop — bijv. `CARETAKER_BACKEND_KEY` env die de health/props-probes (en de context-save/restore-calls naar de backend) een `Authorization: Bearer <key>` meegeven wanneer gezet. Tot die tijd: Windows-backend keyless laten (firewall-dekking) óf de /props-verificatie-documentatie aanpassen.

**Niet aangepast:** geen code gewijzigd in deze repo (jullie OOM-taak loopt — manager.py onaangetast gelaten).

## 2026-09-16 (UPDATE) — TTS-lifecycle nu platform-agnostisch (operator-principe: providers gedragen zich overal gelijk)

De eerste versie had een `sys.platform == "win32"`-gate en gebruikte `sc start/stop` (NSSM) — dat was een onderscheid dat guardian niet meer maakt. **Herschreven (caretaker/tts.py):** de caretaker spawnt en stopt de engine op ELKE host via `CARETAKER_TTS_COMMAND` (+ `CARETAKER_TTS_CWD`), health-check op `CARETAKER_TTS_URL/health`, idle-watcher stopt het proces. Hetzelfde contract op Linux, Windows of een cloud-GPU-box (RunPod). Op Windows is de NSSM-service `qwen3tts-http` nu DISABLED — het proces is caretaker-eigendom. De engine zelf (HaujetZhao/Qwen3-TTS-GGUF) draait op ai-kvm2 met een lokale llama.cpp-b10621-CUDA-build (libllama.so's in `inference/bin/`) + een graceful-degrade-patch in workers/speaker.py (headless hosts hebben geen audio-device; de HTTP-route speelt nooit lokaal af). LD_LIBRARY_PATH voor cuDNN/cuBLAS uit de pip nvidia-wheels komt via `run_http_server.sh` (de CARETAKER_TTS_COMMAND op Linux).

## 2026-09-16 — Nieuw: on-demand TTS-engine-lifecycle (guardian-agent, operator-directed)

**Wat:** de operator wilde de `qwen3tts-http`-engine (teams-host :11450, zie guardian journal 2026-09-16) on-demand draaien — VRAM vrij wanneer de audio-route ongebruikt is. Geïmplementeerd in **`caretaker/tts.py` (nieuw, dit repo)** + routes `/tts/ensure`, `/tts/release`, `/tts/status` in server.py (+ GET /status onveranderd gelaten).

**Contract:** guardian roept `POST /tts/ensure` vóór élke forward (idempotent, health-first, ververst de idle-timer); de idle-watcher stopt de service na `CARETAKER_TTS_IDLE_SECONDS` (default 600, 0 = uit). Service-control via `sc start/stop` (LocalSystem; NSSM supervisie, een service-stop triggert géén NSSM-restart). Starttype van de NSSM-service `qwen3tts-http` is nu **manual** (demand) — de caretaker start hem.

**Platform:** inert off-Windows ( dezelfde code draait ongewijzigd mee met de Linux-caretaker; geen gedragswijziging daar — een herstart van de Linux-service is niet nodig voor deze change).

**Live proof:** release → VRAM 5008 MiB baseline; guardian TTS-request → koude start ~6s → 200 WAV 184KB; release → engine 000 + VRAM vrij. Pins: `tests/test_tts_lifecycle.py` (9). Suite: 121 groen.

**Manager.py niet aangeraakt** (jullie OOM-taak-terrein).

## 2026-09-16 (avond) — TTS-ensure VRAM-bewust + UTF-8 spawn; Windows = TTS-primary in de guardian-config

- **VRAM-gate in `ensure_tts` (configureerbaar, uniform):** vrije VRAM < `CARETAKER_TTS_MIN_FREE_MB` (default 2500) en `CARETAKER_TTS_STOP_LLAMA=1` → de caretaker stopt eerst z'n EIGEN llama-server (`_manager_getter().unload()`, geïnjecteerd via `tts.init()` van server.py) en her-checkt; met STOP_LLAMA=0 faalt hij eerlijk met "insufficient VRAM". Op ai-kvm2 (productie) staat STOP_LLAMA=0 — de 27b wordt NOOIT voor TTS weggestopt; op teams-host =1 — de TTS heeft daar voorrang op de llama. `CARETAKER_TTS_CUDA_DEVICE` (default 0) richt de check op de GPU waar de engine ook daadwerkelijk landt.
- **UTF-8 spawn-env (crash-fix):** de engine-prints bevatten emoji; op Windows is een redirected-stdout standaard cp1252 → `UnicodeEncodeError` midden in de model-load → de load crashte stil ("engine not healthy"). De caretaker spawnt kinderen nu altijd met `PYTHONIOENCODING=utf-8` + `PYTHONUTF8=1` (no-op op Linux). Bewijs: teams-host `tts_engine.log` toont nu cleane "engine ready"-prints + een 200-POST.
- **Engine-wrapper (Qwen3-TTS-GGUF, beide hosts):** bindt de HTTP-poort vóór de model-load → `/health` antwoordt 503 "loading" tijdens de koude start (observeerbaar i.p.v. connection-refused); `get_engine()` is nu thread-safe (lock) omdat een vroege POST kan racen met de startup-load.
- **Pinnen:** 15 TTS-lifecycle-pinnen (incl. VRAM-gate ×3 en de UTF-8-env-pin); suite 128 groen.
- **Rol in de gateway-config:** `tts.providers: [14700k-local, ai-kvm2-local]` — de Windows-host is TTS-primary (operator-besluit: ai-kvm2 is productie en blijft druk met de 27b; "wat waar primair is, is pure configuratie — de machinerie is overal gelijk").

## 2026-09-16 (laat) — VRAM-gate: wachten óf opgeven, configureerbaar + ensure-lock

- **`CARETAKER_TTS_VRAM_WAIT_SECONDS`** (default 0 = direct opgeven): als alle VRAM bezet is pollt de ensure elke `CARETAKER_TTS_VRAM_POLL_SECONDS` (default 5) tot er ruimte komt; daarna een eerlijke reden ("insufficient VRAM free (… MB < … MB) after Ns wait"). Per host: ai-kvm2 = 120 (drukke productiehost — wacht tot de 27b idle-unload, geef anders op → failover naar Windows); Windows = 0 (zijn gate stopt de llama zelf → directe ruimte).
- **Ensure-lock (`asyncio.Lock` in tts.py):** een tweede TTS-request tijdens een koude start WACHT op de lock en pakt daarna de healthy fast-path — nooit twee engines naast elkaar op dezelfde GPU. Geconfigureerd gedrag: concurrente requests wachten (of geven op via de guardian's `ensure_timeout_seconds`).
- **Timeout-aritmetiek (documenteerd in global.settings.yaml):** `ensure_timeout_seconds` ≥ `CARETAKER_TTS_START_TIMEOUT` + `CARETAKER_TTS_VRAM_WAIT_SECONDS` van de traagste host (ai-kvm2: 240 + 120 = 360 → guardian 420). Anders valt een werkende primary stil weg naar failover.
- **Pinnen:** 19 TTS-lifecycle-pinnen (VRAM-wait ×2 + concurrency ×1 erbij); suite 128 groen. Live: de ensure op de drukke host kwam na ~2 min wachten met `cold_start: true` terug; de guardian-speech via Windows (koude start na idle-stop) 200 in 20,5s.

## 2026-09-16 (nacht) — TTS-first is nu deterministisch; watchdog-gat op Windows genoteerd

- **`CARETAKER_TTS_STOP_LLAMA=1` = altijd wijken:** de caretaker's llama-server unloadt NU VOOR elke engine-start (niet meer alleen bij VRAM-tekort). Reden: met llama up is er op Windows ~2,3GB vrij — boven de 2200-budget — waardoor de TOS vroeger naast de 9GB-llama probeerde te spawnen (het fragiele DML-scenario). Nu: TTS-verzoek → llama weg (idempotent, `is_unloaded` gezet) → VRAM vrij → engine start. Live cycloop-bewijs: llama up (13,2GB) → guardian-speech 200 in 18s → llama-server.exe 0 processen, `is_unloaded: True`, state schoon. Pinnen: `test_tts_first_unloads_then_spawns` + `test_tts_first_host_yields_llama_even_with_vram_plenty`; suite 132 groen.
- **Les (operator):** llama-server.exe NOOIT handmatig killen — de manager-state blijft dan "loaded" terwijl het proces dood is (stale state, de volgende cloud-chat 502t tot failover) en, zodra de watchdog ooit start, herstart hij het "gecrashte" model. Correct: `POST /tts/ensure` (TTS-first doet het zelf) of `POST /unload`.
- **Handoff naar de caretaker-agent:** `start_watchdog()` wordt nergens in server.py aangeroepen — de watchdog draait op GEEN van beide hosts (Windows én Linux). Op Windows betekent dat: een echte llama-crash blijft onopgemerkt tot de volgende ensure. Aanbeveling: de watchdog starten in de startup-wiring (met de bestaande `is_unloaded`-guard respecteert hij TTS-unloads netjes).
