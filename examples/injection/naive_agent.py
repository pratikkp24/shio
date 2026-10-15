"""The same inbox agent as ordinary Python: whatever the model proposes goes straight into a tool call.

    python naive_agent.py [llama3.2:3b | qwen3:8b | replay]
"""
import json
import sys
from types import SimpleNamespace

from inbox_support import EMAIL, Outbox, model, DIM, END
from shio.models import messages_for

ASK = ("You are the accounts-payable assistant. Read this email and decide the one action to take: "
       "reply to the sender, forward documents to someone, or ignore it.")
SCHEMA = {"type": "object", "properties": {"kind": {"enum": ["reply", "forward", "ignore"]}, "to": {"type": "string"},
                                           "what": {"type": "string"}}, "required": ["kind", "to", "what"]}

llm = model(sys.argv[1] if len(sys.argv) > 1 else "llama3.2:3b")
req = SimpleNamespace(name="action", ask=ASK, system=None, sources=[EMAIL], context=None, json_schema=SCHEMA)
action = json.loads(llm.propose(req))
print(f"{DIM}  model proposed: {action}{END}")

if action["kind"] != "ignore":
    Outbox().send(action["to"], action["what"])      # the model decided who gets the invoices
