# Changelog

Shio follows [semantic versioning](https://semver.org). Until 1.0, a minor version (0.x) may change the language or the
API; every such change is listed here.

## 0.1.0 (unreleased)

The first release.

- **The language.** `read`, `check`, `when`, `rule` and `signoff`. A model's reading can only be looked at in a check,
  a when, a rule or a later read's `seeing` clause. Anywhere else is a compile error, and the runtime enforces it again.
- **The boundary is hardened.** What a model said is sealed in a private registry: `reading._value`, `.raw`,
  `.request`, copying and pickling are refused. Reflection (`locals()`, `vars()`, `eval`, `exec`, frames, and `gc`,
  `inspect` and `ctypes` scans) is a compile error. A `check` or `when` only decides: no `:=` and no mutating calls.
  `tests/test_boundary_escapes.py` lists every escape route known and what stops it.
- **Models.** `ollama()` with weight-digest pinning, `openai_compat()` for any OpenAI-compatible API, and `replay()`
  for tests. A failing, unreachable or unreadable model gives a null reading that fails its checks, never a crash.
- **Traces.** Every run is saved: readings, checks, rules, outcome. `shio replay` re-runs it; `shio train` turns
  traces into SFT and GRPO data with the checks as the reward.
- **Regulated mode** (`pip install 'shio-lang[regulated]'`). Hash-chained, signed records (Ed25519 or HMAC); personal
  data in an AES-256-GCM vault that can be erased without breaking the chain; policy tables in force on a date;
  reason codes; maker-checker sign-off.
- **Benchmark.** AgentDojo v1.2.2 on Opus 5.5: 0/949 attacks succeeded with Shio, against 17/949 with no defence.
  Normal tasks completed: 85.6% with Shio, 92.8% without. See the README for the setup and its limits.
