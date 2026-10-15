"""What a reading is allowed to be: a pydantic model or a plain type (float, int, str, bool), and how it round-trips
through JSON so a trace can be replayed and a check re-run on a new completion."""
import dataclasses
import importlib
import json
import re
import sys
import typing

from pydantic import BaseModel, TypeAdapter

PLAIN = {"float": float, "int": int, "str": str, "bool": bool}


def _is_model(schema):
    return isinstance(schema, type) and issubclass(schema, BaseModel)


def _inline(node, defs, seen=()):
    """Replace {"$ref": "#/$defs/X"} with X itself, so local models' structured output (which often ignores or
    chokes on $ref) sees plain nested objects. A type that refers to itself keeps its $ref."""
    if isinstance(node, dict):
        ref = node.get("$ref", "")
        if ref.startswith("#/$defs/"):
            name = ref.rsplit("/", 1)[1]
            if name in defs and name not in seen:
                rest = {k: v for k, v in node.items() if k != "$ref"}
                return {**_inline(defs[name], defs, seen + (name,)), **rest}
            return node
        return {k: _inline(v, defs, seen) for k, v in node.items() if k != "$defs"}
    if isinstance(node, list):
        return [_inline(v, defs, seen) for v in node]
    return node


def _flatten(js):
    defs = js.get("$defs", {})
    out = _inline(js, defs)
    if defs and any(isinstance(n, dict) and "$ref" in n for n in _walk(out)):
        out["$defs"] = defs                      # a recursive type: keep the definitions it still points to
    return out


def _walk(node):
    yield node
    if isinstance(node, dict):
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def to_json_schema(schema):
    if _is_model(schema):
        return _flatten(schema.model_json_schema())
    # plain types are wrapped so every reading is a JSON object
    inner = TypeAdapter(schema).json_schema()
    defs = inner.pop("$defs", {})
    return _flatten({"type": "object", "properties": {"value": inner}, "required": ["value"], "$defs": defs})


def schema_ref(schema):
    if _is_model(schema):
        return f"{schema.__module__}:{schema.__qualname__}"
    if isinstance(schema, type):
        return schema.__name__
    return repr(schema)          # list[float], typing.Optional[shio__prog.Amount], ...


def load_schema(ref):
    if ref in PLAIN:
        return PLAIN[ref]
    if re.fullmatch(r"[\w.]+:[\w.]+", ref):
        mod, _, qual = ref.partition(":")
        obj = importlib.import_module(mod)
        for part in qual.split("."):
            obj = getattr(obj, part)
        return obj
    if not re.fullmatch(r"[\w.\[\], |]+", ref):   # trace files are data: only type expressions are evaluated
        raise ValueError(f"not a type expression: {ref!r}")
    names = {"__builtins__": {"list": list, "dict": dict, "tuple": tuple, "set": set, **PLAIN, "NoneType": type(None)},
             "typing": typing, **vars(typing), **{m: mod for m, mod in sys.modules.items() if m.startswith("shio__")}}
    return eval(ref, names)


def _json_text(raw):
    """The JSON object in a completion (models sometimes wrap it in a code fence or think out loud first)."""
    raw = re.sub(r"<think>.*?</think>", "", raw or "", flags=re.S)
    m = re.search(r"```(?:json)?\s*(.*?)```", raw, re.S)
    if m:
        raw = m.group(1)
    start = raw.find("{")
    return raw[start:raw.rfind("}") + 1] if start >= 0 else raw


def parse(schema, raw):
    """(value, error). A completion that does not fit the schema is a failed reading, never an exception."""
    try:
        if _is_model(schema):
            return schema.model_validate_json(_json_text(raw)), None
        return TypeAdapter(schema).validate_python(json.loads(_json_text(raw))["value"]), None
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:300]}"


def from_jsonable(schema, data):
    if data is None:
        return None
    return schema.model_validate(data) if _is_model(schema) else TypeAdapter(schema).validate_python(data)


def to_jsonable(x):
    if isinstance(x, BaseModel):
        return x.model_dump(mode="json")
    if dataclasses.is_dataclass(x) and not isinstance(x, type):
        return to_jsonable(dataclasses.asdict(x))
    if isinstance(x, dict):
        return {str(k): to_jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple, set)):
        return [to_jsonable(v) for v in x]
    if x is None or isinstance(x, (str, int, float, bool)):
        return x
    return repr(x)
