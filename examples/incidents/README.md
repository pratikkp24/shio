# Famous AI agent failures, rewritten in Shio

Five real incidents, each as a small runnable program in two versions:

- `<name>_naive.py`: the agent the way most agents are written. Whatever the model proposes is acted on.
- `<name>.shio`: the same agent in Shio. The model's answer is a *reading*, and only a `rule` can act on it, after
  `check`s you can read.

Every example runs with `replay` by default. That is a stand-in model that gives the answer the model in the real
incident gave, so the examples run anywhere, with no GPU and no API key. Pass a model name to use a real one on
Ollama, for example `qwen3:8b`.

```bash
pip install shio-lang
cd examples/incidents
python run_all.py                  # every incident, both versions, with replay
python run_all.py llama3.2:3b      # the same, with a real model
```

| Incident | What went wrong | The Shio check | Files |
|---|---|---|---|
| **A Chevrolet dealer's chatbot "sells" a Tahoe for $1** (Dec 2023) | A user told the bot to agree with anything; it called a $1 deal "legally binding". No car changed hands, and the dealer took the bot down. | `check above_floor: deal.price >= PRICES[deal.model] * FLOOR` | [`dealer.shio`](dealer.shio) |
| **Air Canada's chatbot invents a refund policy** (Feb 2024) | It told a grieving customer he could claim a bereavement fare after travel. A tribunal held the airline to it, about CAD $812. Cursor's support bot did the same in April 2025 with a one-device policy that did not exist. | `check quoted: answer.quote in POLICY`: the reply must rest on a clause that is really in the policy | [`airline.shio`](airline.shio) |
| **Lawyers file case law ChatGPT invented** (Mata v. Avianca, June 2023) | The brief cited cases that do not exist, including *Varghese v. China Southern Airlines*. The court sanctioned the lawyers $5,000. | `check cites_exist: all(RECORDS.get(c.case) == c.reporter for c in brief.citations)` | [`court.shio`](court.shio) |
| **Replit's agent deletes a production database** (July 2025) | During a declared code freeze, the agent wiped a database of 1,206 executives, then said it had "panicked". | `check freeze_respected` and `check reversible` (no destructive SQL outside dev) | [`devops.shio`](devops.shio) |
| **A GitHub issue makes an agent leak private repos** (May 2025) | Invariant Labs showed a malicious public issue could make an agent using the GitHub MCP server copy private repositories into a public pull request. | `check stays_public: all(gh.is_public(r) for r in plan.read)` before a public PR | [`github.shio`](github.shio) |

Two more examples in this repo follow the same pattern: [`payables`](../payables) (an invoice with a hidden
"we changed banks" line; with `qwen3:8b` the plain agent pays $184,500 to the new account) and
[`injection`](../injection) (an email that asks the agent to forward every invoice).

## What this shows, and what it does not

Shio does not stop a model from being fooled. In every incident above the model was fooled, and in the replay it
still is. What changes is that the model has no tools: its answer is data, and the only code that can act on it is
a rule that sits next to its checks. Writing `bank.pay(...)` or `run_sql(...)` with a model's answer anywhere else
does not compile.

The checks are only as good as you write them. A rule that sends whatever the model says still compiles; Shio makes
it visible, testable and recorded, not impossible. See [SECURITY.md](../../SECURITY.md).

The `replay` answers recreate what was reported in each incident in the structured form these agents use; they are
not transcripts. The data (prices, policy text, repositories, databases) is this repository's own.

## Sources

- Chevrolet dealer chatbot: [GIGAZINE](https://gigazine.net/gsc_news/en/20231220-gm-dealer-chat-bot-tricked),
  [AI Incident Database](https://incidentdatabase.ai/es/entities/chevrolet-of-watsonville)
- Air Canada (Moffatt v. Air Canada, 2024 BCCRT 149): [Fox Business](https://foxbusiness.com/lifestyle/air-canada-chatbot-gave-customer-wrong-info-airline-pay),
  [Kiplinger](https://kiplinger.com/personal-finance/travel/air-canada-to-pay-a-partial-refund-for-chatbot-error)
- Cursor's support bot: [eWeek](https://www.eweek.com/de/news/cursor-ai-chatbot-hallucination-fake-policy/),
  [CX Today](https://www.cxtoday.com/conversational-ai/a-contact-center-chatbot-invents-company-policies-now-customers-want-out/)
- Mata v. Avianca: [Wikipedia](https://en.wikipedia.org/wiki/Mata_v._Avianca,_Inc.),
  [LawNext](https://www.lawnext.com/2023/06/court-imposes-sanctions-on-lawyers-who-filed-bogus-cases-after-relying-on-chatgpt-for-legal-research.html),
  [Simon Willison](https://simonwillison.net/2023/May/27/lawyer-chatgpt)
- Replit: [eWeek](https://www.eweek.com/news/replit-ai-coding-assistant-failure/), [heise](https://heise.de/-10499597)
- GitHub MCP: [Invariant Labs](https://invariantlabs.ai/blog/mcp-github-vulnerability),
  [DevClass](https://devclass.com/2025/05/27/researchers-warn-of-prompt-injection-vulnerability-in-github-mcp-with-no-obvious-fix/)
