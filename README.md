# caretaker-llamacpp

`caretaker-llamacpp` is the **per-GPU-host manager daemon that owns the llama-server
lifecycle**: it spawns, stops, reloads and monitors the local `llama-server` behind a
thin, authenticated control API on port `:11441` (`GET /status`, `POST /ensure`,
`POST /unload`). OpenAI inference stays direct to `llama-server` on `:11440/v1` and is
**not** handled by this daemon. See the phased implementation plan in
[`PLAN.md`](./PLAN.md) for the full roadmap (phases A–E) and the context/goals in
section 0 of that file.