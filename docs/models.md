# Models

A model in Shio only proposes: `propose(request) -> text`. Parsing, checking and deciding happen in Shio. Any object
with that method is a model.

```python
from shio import models

models.ollama("qwen3:8b", digest="sha256:...")            # a local model; refused unless the weights match the pin
models.openai_compat("gpt-x", "https://api.example.com/v1", key_env="EXAMPLE_API_KEY")   # vLLM, LM Studio, OpenAI...
models.anthropic("claude-opus-5-5", effort="medium")      # Claude, pip install 'shio-lang[anthropic]'
models.replay(lambda request: {"value": 42})              # saved answers, for tests and demos
```

- **Structured output.** Every backend asks for JSON matching the reading's schema. With `structured=False` the schema
  goes in the prompt instead, for a provider without structured outputs.
- **Failures are null readings.** A timeout, an error, a refusal or a reply that doesn't fit the schema gives a null
  reading, which fails its checks. A model failure never crashes an agent and never passes a check.
- **Retries and pacing.** Every backend retries timeouts, 429 and 5xx with backoff, honouring `Retry-After`
  (`retries=2`), and `per_minute=` paces calls across threads.
- **Keys** come from the environment and are never written to a trace.
