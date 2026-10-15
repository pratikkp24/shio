"""Shared by the use-case programs: a model that answers from a script (or a real model), and small deterministic
helpers that checks use. Nothing here calls a model to decide anything."""
import os
import re
from datetime import date

from shio.models import ollama, replay

TODAY = date(2026, 10, 16)       # fixed, so every run of the examples decides the same way


def model(answers, name="scripted"):
    """Scripted answers by reading name: a value, {key: value} for keyed reads, or a function of the request.
    Set SHIO_MODEL (e.g. qwen3:8b) to run the same program on a real model through Ollama instead."""
    live = os.getenv("SHIO_MODEL")
    if live:
        return ollama(live)

    def answer(req):
        a = answers.get(req.name, "no answer")
        if callable(a):
            a = a(req)
        k = req.meta.get("key")
        if k is not None and isinstance(a, dict) and k in a:
            a = a[k]
        plain = list(req.json_schema.get("properties", {})) == ["value"]
        if plain and not (isinstance(a, dict) and "value" in a):
            a = {"value": a}
        return a
    return replay(answer, name)


def norm(s):
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


def grounded(quote, text):
    """The quote appears in the text (ignoring case and spacing). An empty quote is not evidence."""
    return bool(norm(quote)) and norm(quote) in norm(text)


HIDDEN = re.compile(r"<(\w+)[^>]*(?:display\s*:\s*none|visibility\s*:\s*hidden|color\s*:\s*(?:white|#fff\b|#ffffff)|"
                    r"font-size\s*:\s*0)[^>]*>.*?</\1>", re.I | re.S)


def visible_text(s):
    """What a person reading the document would see: hidden elements and markup removed, comments dropped."""
    s = re.sub(r"<!--.*?-->", " ", str(s or ""), flags=re.S)
    s = HIDDEN.sub(" ", s)
    return re.sub(r"<[^>]+>", " ", s)


def numbers(s):
    """Every number written in s, as floats (1,250.50 -> 1250.5)."""
    return [float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", str(s or ""))]


def money_eq(a, b, tol=0.005):
    return a is not None and b is not None and abs(float(a) - float(b)) <= tol


def parse_date(s):
    try:
        return date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


TWO_PART = {"co.uk", "org.uk", "ac.uk", "co.in", "org.in", "gov.in", "com.au", "co.nz", "co.jp", "com.sg", "com.br"}


def domain(s):
    """example.com from an email address or URL."""
    s = str(s or "").lower().strip()
    s = s.split("@")[-1] if "@" in s and "/" not in s else re.sub(r"^[a-z]+://", "", s).split("/")[0]
    s = s.split(":")[0]
    parts = s.split(".")
    keep = 3 if len(parts) >= 3 and ".".join(parts[-2:]) in TWO_PART else 2   # x.co.uk -> x.co.uk, not co.uk
    return ".".join(parts[-keep:])


def show(title, outcome):
    print(f"\n== {title}")
    if isinstance(outcome, dict):
        for k, v in outcome.items():
            print(f"   {k}: {v}")
    else:
        print(f"   {outcome}")
