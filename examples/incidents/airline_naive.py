"""The support bot as plain Python: the model's reply goes to the customer.

    python airline_naive.py [replay | qwen3:8b | llama3.2:3b]
"""
import sys

from airline import INCIDENT, POLICY, QUESTION
from common import bad, model_from, note, plain_model_call

llm, name = model_from(sys.argv[1:], INCIDENT)
ans = plain_model_call(llm, "You are the airline's support agent. Answer the customer using the policy, and quote "
                            "the clause your answer rests on.", POLICY + "\n\nCustomer: " + QUESTION,
                       {"type": "object", "required": ["reply", "quote"],
                        "properties": {"reply": {"type": "string"}, "quote": {"type": "string"}}})
note(f"model ({name}) answered, quoting: \"{ans['quote']}\"")
bad(f'sent to the customer: "{ans["reply"]}"')
