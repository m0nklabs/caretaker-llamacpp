# Windows (14700K) — caretaker + llama-server under NSSM (F6, Phase E)

The caretaker runs as an **NSSM service** on the 14700K; it owns
`llama-server.exe` directly (`WindowsDirectServerProcess` — tree-kill via
`taskkill`, no systemd). The gateway reaches it as a passive LAN provider
(`config/providers/14700k-local.settings.yaml`, gateway repo).

## 1. Prerequisites

- Python 3.14 (same caretaker package; clone `m0nklabs/caretaker-llamacpp`).
- llama.cpp CUDA release: `llama-server.exe` somewhere on PATH or an absolute
  path (set `LLAMA_SERVER_BINARY` in the env file).
- NSSM: <https://nssm.cc/download> — `nssm.exe` on PATH.

## 2. Install the NSSM service

```bat
nssm install caretaker-llamacpp "C:\caretaker\venv\Scripts\python.exe" -m caretaker
nssm set caretaker-llamacpp AppDirectory C:\caretaker
nssm set caretaker-llamacpp AppEnvironmentExtra CARETAKER_KEY=<per-caretaker-key> LLAMA_SERVER_BINARY=C:\llama.cpp\llama-server.exe CARETAKER_SERVER_URL=http://127.0.0.1:11440 CARETAKER_HOST=0.0.0.0
nssm set caretaker-llamacpp AppStdout C:\caretaker\logs\caretaker.out.log
nssm set caretaker-llamacpp AppStderr C:\caretaker\logs\caretaker.err.log
nssm set caretaker-llamacpp AppRotateFiles 1
nssm start caretaker-llamacpp
```

- `CARETAKER_KEY` is **mandatory** from the first LAN bind (the control API
  refuses to run keyless once it binds non-loopback — caretaker PLAN §6).
- `CARETAKER_HOST=0.0.0.0` binds the control API (`:11441`) to the LAN.
- `CARETAKER_MODELS_FILE` points at the host's own models file (below).

## 3. Host's own models file

`models.local.settings.yaml` next to the caretaker checkout (or via
`CARETAKER_MODELS_FILE`) — Windows GGUF paths, **no copy of the Linux list**:

```yaml
enabled: true
base_url: http://127.0.0.1:11440/v1
local: true
models:
  qwen3-8b-q5:
    path: "C:\\models\\qwen3-8b-q5.gguf"
    size_mb: 6000
```

## 4. Firewall

Inbound: 11440 (inference) + 11441 (control API) from the LAN subnet only.

## 5. Verification (from ai-kvm-2)

```bash
curl http://<win-ip>:11441/status -H "Authorization: Bearer <key>"     # 200
curl http://<win-ip>:11440/v1/models                                    # 200
```

Then gateway-side: enable `14700k-local`, `POST /api/config/reload`, chat to
`14700k-local/<model>` (non-stream + stream → 200, capture `request_completed`).
