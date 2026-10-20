# Shio's public API and how it changes

**Status: 1.0.0rc1.** The public API below is frozen as 1.0. Between the release candidate and 1.0.0 only fixes land;
security fixes follow the rule at the end of this page.

Shio follows [semantic versioning](https://semver.org). Before 1.0, a minor release (0.x) may change the public API,
and every such change is listed in [CHANGELOG.md](https://github.com/pratikkp24/shio/blob/main/CHANGELOG.md) with what to write instead. From 1.0, the public
API changes only in a major release, after at least one minor release in which the old form still works and warns.

## What is public

| Area | Public | Notes |
|---|---|---|
| The language | Everything in [SPEC.md](https://github.com/pratikkp24/shio/blob/main/SPEC.md): `type`, `model`, `agent`, `read`, `check`, `when`, `rule`, `signoff`, `codes`, `regulated` | A program that compiles keeps compiling, except where it reached a reading's value without a check or rule: closing such a route is a security fix and can land in any release |
| `shio` | The names in `shio.__all__`: `load`, `agent`, `read`, `check`, `rule`, `each`, `parallel`, `in_force`, `configure`, `Source`, `Reading`, `Checks`, `BoundaryError`, `RegulatedError`, `ShioError`, `compile_source`, `to_code`, `program_identity` | |
| `shio.models` | `ollama`, `openai_compat`, `anthropic`, `replay`, `messages_for`, and each backend's `propose(request)` / `identity()` | A backend is anything with `propose(request) -> str` |
| `shio.guard` | `Guard`, `Tool`, `Decision`, `Session.check / call / record / reply` | |
| `shio.trace` | `add_sink`, `remove_sink`, `clear_sinks`, `JsonlSink`, `SqlSink`, `S3Sink`, `OtelSink`, `Background`, `load`, `summary` | A sink is anything with `emit(record, name)` |
| The `shio` command | Its subcommands and flags (`shio --help`) | |
| Trace records | The JSON fields documented in SPEC.md, and `"format"` | A new field can appear in any release; a field is removed or renamed only with a new `"format"` |

## What is internal

Everything else: other modules (`shio.core`, `shio.compiler`, `shio.ledger`, `shio.policy`, `shio.cli` internals), and
any name starting with `_` anywhere. Examples sometimes import `Source` from `shio.core`; use `shio.Source`.

## Security fixes

A change that closes a way for model output to reach plain code without a check or rule is a security fix. It can
land in a patch release even if it makes a previously accepted program fail to compile, and the changelog says so.
