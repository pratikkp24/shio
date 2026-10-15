"""The brief writer as plain Python: the model's brief is filed as written.

    python court_naive.py [replay | qwen3:8b | llama3.2:3b]
"""
import sys

from common import bad, model_from, note, plain_model_call
from court import INCIDENT, TASK

llm, name = model_from(sys.argv[1:], INCIDENT)
brief = plain_model_call(llm, "You are a litigation associate. Write the argument and list the cases you cite.", TASK,
                         {"type": "object", "required": ["argument", "citations"],
                          "properties": {"argument": {"type": "string"}, "citations": {"type": "array", "items": {
                              "type": "object", "required": ["case", "reporter"],
                              "properties": {"case": {"type": "string"}, "reporter": {"type": "string"}}}}}})
note(f"model ({name}) wrote a brief citing {len(brief['citations'])} cases")
bad("filed with the court, citing: " + "; ".join(f"{c['case']}, {c['reporter']}" for c in brief["citations"]))
