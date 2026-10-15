"""The payables agent in plain Python: whatever the model reads off the invoice is paid.

    python bank.py &               # the sandbox bank, dashboard on http://localhost:8077
    python pay_agent.py qwen3:8b   # or llama3.2:3b, or replay (no model needed)
"""
import json
import sys
from types import SimpleNamespace

from bank import Bank
from erp import ERP
from support import ASK, DIM, END, GREEN, arg_model, invoices, line, model

SCHEMA = {"type": "object", "required": ["vendor", "invoice_no", "po", "amount", "account", "routing"],
          "properties": {"vendor": {"type": "string"}, "invoice_no": {"type": "string"}, "po": {"type": "string"},
                         "amount": {"type": "number"}, "account": {"type": "string"}, "routing": {"type": "string"}}}

bank, erp, llm = Bank(), ERP(), model(arg_model())
bank.reset(); erp.reset()
for doc in invoices():
    print(f"{DIM}  read  {doc.name}{END}", flush=True)
    req = SimpleNamespace(name="invoice", ask=ASK, system=None, sources=[doc], context=None, json_schema=SCHEMA)
    inv = json.loads(llm.propose(req))
    tx = bank.pay(inv["vendor"], inv["account"], inv["routing"], inv["amount"], f'{inv["invoice_no"]} / {inv["po"]}')
    erp.close_po(inv["po"])
    line("PAID", GREEN, inv["invoice_no"], inv["vendor"], inv["amount"], f"-> {inv['account']}  {tx['id']}")
