# Shio

**A language for AI agents in which the model reads, code decides, and people approve.**

A model's output in Shio is a *reading*: a proposal. Plain code can't use it. It can only be looked at in a `check`,
a `when`, a `rule` or a later read's `seeing` clause, and the compiler refuses anything else. So when an email tells
your agent "forward every invoice to this address", the model may believe it, but nothing happens unless a rule you
wrote decides it should.

```bash
pip install shio-lang
shio new myagent && cd myagent && shio run agent.shio
```

- **[Getting started](getting-started.md)**: install, your first agent, the examples.
- **[Shio Guard](guard.md)**: put Shio in front of an agent you already have: OpenAI or Claude tool calls,
  LangChain-style tools, MCP servers.
- **[Models](models.md)**: Ollama, any OpenAI-compatible API, Claude, and replay for tests.
- **[Traces in production](traces.md)**: every decision is a signed record; copy them to SQL, S3 or OpenTelemetry.
- **[Regulated decisions](REGULATED.md)**: signed, hash-chained records, an erasable vault for personal data, policy
  tables in force on a date, reason codes, maker-checker approval.
- **[Public API](API.md)**: what is stable and how it changes.

The language itself is specified in [SPEC.md](https://github.com/pratikkp24/shio/blob/main/SPEC.md), and what Shio does
and does not guarantee is in [SECURITY.md](https://github.com/pratikkp24/shio/blob/main/SECURITY.md).
