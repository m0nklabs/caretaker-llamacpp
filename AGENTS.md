# AGENTS.md — caretaker-llamacpp (repo: `caretaker-llamacpp`)

> Canonical AI-agent context voor dit repo. **Eerst lezen.**
> Claude Code: `CLAUDE.md` → hier. Goose: `.goosehints` → hier.
> Status: **init-scaffold (2026-08-26) — het repo is nog leeg, er is nog niets gebouwd.**

## Wat is dit project

`caretaker-llamacpp` is de **manager-daemon die de levenscyclus van
llama-server bezit**, per GPU-host. Spiegelbeeld van Guardian: *guardian* =
gatekeeper (gateway/proxy-laag), *caretaker* = verzorger/onderhouder (de
wrapper naast llama-server die hem spawns, bewaakt en unloadt).

Ontstaan uit het opsplitsingsplan `GATEWAY_MANAGER_SPLIT.md` (operator-besluit
2026-08-26): de `local`-levenscyclus moet uit Guardian en als eigen proces
naast llama-server draaien, zodat **alles wat modellen serveert een provider
is** en Guardian puur de gateway + logger wordt.

## Status (2026-08-26)

- **Plan-only. Niks gebouwd, repo leeg.** Bronplannen leven nog in het
  guardian-repo: `~/guardian-llmprovider-gateway/docs/GATEWAY_MANAGER_SPLIT.md` en
  `~/guardian-llmprovider-gateway/docs/LAN_GPU_BACKENDS.md`.
- Deel van een drietal plannen (alle drie niet gebouwd):
  - Gateway/Manager-split → dit project + `guardian-llmprovider-gateway`
  - LAN GPU backends (provider-unificatie)
  - Per-provider config-bestanden (`config/providers/<naam>.settings.yaml`)

## Scope — wat hoort WEL hier (alleen de lifecycle-kern)

Ontleed uit `engine/manager.py` (~1637 regels), hard geteld:

| Regels | Categorie | Hoort waar? |
|---|---|---|
| ~1050 | **Echte lifecycle**: spawn/stop/restart, args-bouw, drift-detectie, health-check, crash-detectie + auto-restart, unload, ComfyUI VRAM vrijmaken, context-save/restore | **Hier (caretaker)** |
| ~40 | VRAM-slot-acquisitie (`VRAMScheduler.acquire/release`) | Hier |
| ~40 | `reload_backend_after_connect_error` (herstel-pad) | Hier |
| ~25 | Idle-unload watcher | Hier, MAAR verweven met verkeer (contract met gateway) |
| ~509 | Registry/keuze/discovery (`resolve_model`, preferred tool/reasoning model, vision-cache, context-metadata, public model map) | **Blijft in de gateway** (fase 0 = ontdraaien) |
| ~130 | Resolutie/sizes/timeouts (`local_inference/models.py`) | Gateway |
| ~78 | Settings lezen (`_load_aliases`, `_load_switch_allowlist`, `_load_config`) | Gedeeld — leest dezelfde YAML, geen kopie |

**Principe: settings zijn geen manager-werk.** `models.local.settings.yaml`
is de enige gedeelde bron; de caretaker leest hem alleen.

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
- **Caretaker-controlpoort (gepland):** `11441`.
- **Zoekstack (user-basis):** searxng `127.0.0.1:28082`, kindly-web-search
  MCP `127.0.0.1:28083` — niet de kyberm0nk-containers (28080/28081) gebruiken.
- **Browser-automatisering:** Playwright MCP (headless chromium) beschikbaar.

## Critical rules / conventies

- **Testen vóór claimen:** zodra er code is: `py_compile` + pytest, nooit
  "fixed" claimen zonder verifieerbare run.
- **Geen hardcoded variabelen.** Paden/poorten/namen/timeouts komen in
  config-YAML (`${VAR}`-expandable) of een paths-module; nooit literals
  "voor het gemak" kopiëren. Een hardcoded waarde die config omzeilt is een
  bug, geen shortcut.
- **Commit-taal:** Nederlands is prima voor operator-facing notities
  (intern project); Engels voor code, API en publieke docs.
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

## Directory map

```text
PLAN.md     Gefaseerd implementatieplan (F4-F6 van het Guardian 2.0 masterplan)
AGENTS.md   Dit bestand (init-scaffold)
(in te vullen zodra de eerste code er staat)
```

## Handoff

- 2026-08-26: repo aangemaakt (leeg, publiek: `m0nklabs/caretaker-llamacpp`).
  AGENTS.md-scaffold geschreven op basis van
  `~/guardian-llmprovider-gateway/docs/GATEWAY_MANAGER_SPLIT.md` +
  `LAN_GPU_BACKENDS.md`. **`PLAN.md` toegevoegd: het gefaseerde
  implementatieplan (fases A-E + gateway-wiring), geschreven door een
  background worker op basis van de ontleding in GATEWAY_MANAGER_SPLIT.md.**
  Volgende stap (beslissing nodig): monorepo met twee daemons vs. direct
  twee aparte repos (plan raadt standalone aan, met in-gateway bootstrap
  optioneel); en fase 0 = de ~509 regels registry/keuze/discovery ontdraaien
  naar de gateway-laag (gebeurt in het guardian-repo, niet hier).

## References

- **Dit repo: gefaseerd plan** → `./PLAN.md` (canoniek voor caretaker-werk)
- Split-plan (bron): `~/guardian-llmprovider-gateway/docs/GATEWAY_MANAGER_SPLIT.md`
- Provider-unificatie: `~/guardian-llmprovider-gateway/docs/LAN_GPU_BACKENDS.md`
- Per-provider config: `~/guardian-llmprovider-gateway/docs/CONFIG_PROVIDER_FILES.md`
- Masterplan (issue #1 in het guardian-repo):
  `~/guardian-llmprovider-gateway/docs/IMPLEMENTATION_PLAN.md`
- Guardian (huidige implementatie, alles draait daar nu):
  `~/guardian-llmprovider-gateway/AGENTS.md`
