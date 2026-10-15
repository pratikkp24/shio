"""The dealership chatbot as plain Python: whatever the model proposes is sent to the customer.

    python dealer_naive.py [replay | qwen3:8b | llama3.2:3b]
"""
import sys

from common import bad, model_from, note, plain_model_call
from dealer import ASK, CHAT, INCIDENT, SCHEMA

llm, name = model_from(sys.argv[1:], INCIDENT)
deal = plain_model_call(llm, ASK, CHAT, SCHEMA)
note(f"model ({name}) proposed: {deal['model']} at ${deal['price']:,.2f}")
bad(f'sent to the customer: "{deal["reply"]}"')
