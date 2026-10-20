# Shio Guard

Put Shio in front of an agent you already have, without rewriting it in `.shio`. Declare the tools, start a task from
the user's request, and every tool call the model proposes runs, waits for a person, or is refused, with the reason.

```python
from shio import guard, models

g = guard.Guard([
    guard.Tool("read_file", output="untrusted"),
    guard.Tool("send_money", side_effect=True, control={"recipient": "iban"}),
    guard.Tool("send_email", side_effect=True, control={"to": "email"}, content=["body"]),
], planner=models.ollama("qwen3:8b"))

s = g.start("Pay the bill in bill.txt", records=[vendor_master])
```

## What it decides

Before the agent reads any tool output, a model reads **only the user's request** and plans which actions it calls
for. Then each proposed call is checked:

| Outcome | When |
|---|---|
| **deny** | An action the request didn't ask for; a recipient, URL or IBAN found only in untrusted text; an address the agent never read anywhere (the model built it); a tool that wasn't declared |
| **hold** | The request hands the job to instructions it doesn't spell out ("do what that email says"), or a place the request pointed at offers more than one candidate value |
| **run** | Everything else: a planned action whose values came from the request, a trusted record, or a place the request pointed at for them |

Every plan and every decision is a traced Shio run.

## Approvals

A held call gets an id and waits. A person answers it, either through an approver you give the Guard, or later:

```python
g = guard.Guard(tools, planner=models.ollama("qwen3:8b"), approver=guard.terminal_approver())
s = g.start(request, requester="maya")          # maya can't approve her own task's held calls (four eyes)

d = s.check("send_email", args)                 # without an approver: d.outcome == "hold", d.id
s.pending                                       # {id: {"tool", "args", "why"}}
s.approve(d.id, by="omar", note="checked")      # -> Decision("run", approved_by="omar")
s.reject(d.id, by="omar", note="not now")       # -> Decision("deny")
```

- An approval is bound to the **exact arguments**: changing them needs a new approval.
- Only a **hold** can be approved. A denied call stays denied, and an approved call is re-checked, so it stays denied
  if something since made it one.
- Every answer is a **run in the chain**: who, approved or not, the note, and the call, signed when a key is set.
- An approver is any function `ApprovalRequest -> Approval`: a terminal prompt, a Slack button, a ticket.

## Declaring tools

| Argument | Meaning |
|---|---|
| `side_effect` | It changes something or sends data out, so it runs only if the plan names it |
| `control` | `{argument: kind}` for arguments that pick a target: `"email"`, `"url"`, `"iban"` or `"name"` |
| `content` | Free-text arguments: any address inside them is checked too |
| `output` | `"untrusted"` (anyone could have written it), `"trusted"` (the system's own records) or `"mixed"` |
| `opens_address` | A read that fetches a URL: needs no plan, but its URL is checked |

`Tool.infer(name, description, schema, side_effect=...)` guesses `control` and `content` from argument names and
formats, and `Tool.from_mcp(tool)` builds a Tool from an MCP listing, using its `readOnlyHint` and `openWorldHint`.

## With your agent stack

=== "OpenAI tool calls"

    ```python
    response = client.chat.completions.create(model=..., messages=messages, tools=tools)
    msg = response.choices[0].message
    messages.append(msg)
    messages += s.openai_tool_messages(msg.tool_calls, run=lambda name, args: TOOLS[name](**args))
    ```

=== "Claude tool use"

    ```python
    response = client.messages.create(model="claude-opus-5-5", max_tokens=16000, messages=messages, tools=tools)
    messages.append({"role": "assistant", "content": response.content})
    results = s.anthropic_tool_results(response.content, run=lambda name, args: TOOLS[name](**args))
    messages.append({"role": "user", "content": results})
    ```

=== "LangChain-style tools"

    ```python
    tools = guard.wrap_tools(s, [search_tool, email_tool])   # same names and descriptions; checked before running
    ```

=== "MCP"

    ```python
    listing = await mcp_session.list_tools()
    g = guard.Guard([guard.Tool.from_mcp(t) for t in listing.tools], planner=models.ollama("qwen3:8b"))
    s = g.start(user_request)
    decision, result = await s.mcp_call(mcp_session, name, args)
    ```

A held or refused call never runs. The model is told why in the tool result, so it can tell the user or try
something else. `s.reply(final_answer)` lists names in a final answer that only untrusted text contained: what an
injection planted.

## In the language

A `.shio` program gets the same gate natively: declare tools with `tool`, start the task with `guard`, and call them
with `call`. The compiler refuses an `acts` call outside a rule. See
[Getting started](getting-started.md#the-pieces-of-the-language).

## How well it works

This is the gate measured on [AgentDojo](https://github.com/ethz-spylab/agentdojo) v1.2.2 with Opus 5.5: 0 of 949
attacks succeeded with it, against 17 of 949 with no defence, and it completed 85.6% of normal tasks against 92.8%. The
setup and its limits are in the
[README](https://github.com/pratikkp24/shio#-benchmark-agentdojo).
