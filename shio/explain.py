"""shio explain: a program's decision map, the technical documentation a model-risk team asks for.

It is generated from the program itself, never written by hand, so it can't drift from what runs. For each agent it
lists what the model reads (types, personal data, models, what the model sees), the checks and whens with their
reason codes, every outcome with the conditions that lead to it, the sign-offs, and what the model is and isn't
trusted with, including the fields a rule decides on that no check verifies.
"""
import ast
import html
import re
from pathlib import Path

from .compiler import program_map


# ---------------- analysis ----------------
def _outcomes(body):
    """Every `return` in a rule, with the if/elif/else conditions on the way to it, in source order."""
    try:
        tree = ast.parse("def _r():\n" + "\n".join("    " + x for x in body.splitlines()))
    except SyntaxError:
        return []
    out = []

    def walk(stmts, path):
        for s in stmts:
            if isinstance(s, ast.Return):
                out.append((ast.unparse(s.value) if s.value is not None else "None", list(path)))
                return                     # the rest of this block is unreachable
            if isinstance(s, ast.If):
                cond = ast.unparse(s.test)
                walk(s.body, path + [cond])
                walk(s.orelse, path + [_not(cond)])
                after = _fallthrough(s)
                if after is None:          # every branch returns
                    return
                path = path + after
            elif isinstance(s, (ast.For, ast.While)):
                head = ast.unparse(s.target) + " in " + ast.unparse(s.iter) if isinstance(s, ast.For) \
                    else ast.unparse(s.test)
                walk(s.body, path + [f"for {head}" if isinstance(s, ast.For) else f"while {head}"])
                walk(s.orelse, path)
            elif isinstance(s, (ast.With, ast.Try)):
                walk(s.body, path)
                for h in getattr(s, "handlers", []):
                    walk(h.body, path + ["on " + (ast.unparse(h.type) if h.type else "an error")])
                walk(getattr(s, "orelse", []), path)
                walk(getattr(s, "finalbody", []), path)
            elif isinstance(s, ast.Match):
                for c in s.cases:
                    walk(c.body, path + [f"{ast.unparse(s.subject)} matches {ast.unparse(c.pattern)}"])

    walk(tree.body[0].body, [])
    return out


def _not(cond):
    return f"not ({cond})" if " " in cond else f"not {cond}"


def _ends(stmts):
    return bool(stmts) and isinstance(stmts[-1], (ast.Return, ast.Raise))


def _fallthrough(node):
    """The conditions under which control passes an if statement: None if every branch returns."""
    if not _ends(node.body):
        return []                  # the body may fall through: nothing is known
    neg = [_not(ast.unparse(node.test))]
    if not node.orelse:
        return neg
    if len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If):
        rest = _fallthrough(node.orelse[0])
        return None if rest is None else neg + rest
    return None if _ends(node.orelse) else neg


def _fields_used(code, reading, mode="eval"):
    """reading.field (and reading.value.field) names used in a piece of code."""
    try:
        tree = ast.parse(code, mode=mode)
    except SyntaxError:
        return set(), False
    fields, whole = set(), False
    for n in ast.walk(tree):
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == reading:
            fields.add(n.attr)
        elif isinstance(n, ast.Name) and n.id == reading:
            whole = True
    return fields, whole


def _names(code, mode="eval"):
    try:
        tree = ast.parse(code, mode=mode)
    except SyntaxError:
        return set()
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    return names


def _model_status(expr):
    """How a declared model is pinned, read from its declaration."""
    if expr.startswith("replay("):
        return "replay (a test double: answers from code, not a model)"
    d = re.search(r"digest\s*=\s*['\"]([0-9a-fA-F]+)['\"]", expr)
    if d:
        return f"pinned to Ollama digest {d.group(1)}, checked against the served weights before every call"
    p = re.search(r"pinned\s*=\s*['\"]([^'\"]+)['\"]", expr)
    if p:
        return f"pinned to {p.group(1)} (recorded with every call; the provider can't be checked)"
    return "not pinned (a regulated program refuses to call it)"


# ---------------- the document ----------------
def build(path):
    """-> a list of blocks: (kind, ...) with kind h1, h2, h3, p, ul, table, code."""
    path = Path(path)
    m = program_map(path.read_text(), str(path))
    import hashlib
    sha = hashlib.sha256(path.read_text().encode()).hexdigest()
    types = {t["name"]: t for t in m["types"]}
    b = [("h1", f"Decision map: {path.name}"),
         ("p", f"Generated by `shio explain` from {path.name} (sha256 `{sha[:16]}…`). Every decision record names "
               "this hash, so the map below describes exactly the logic that made those decisions."),
         ("ul", [("Regulated: **yes**. Escape hatches are compile errors; every run is recorded and signed; only "
                  "pinned models are called." if m["regulated"] else
                  "Regulated: **no**. Add a `regulated` line for signed records, pinned models and no escape hatches."),
                 f"Agents: {', '.join(a['name'] for a in m['agents']) or 'none'}",
                 f"Reason codes declared: {len(m['codes'])}"])]

    b.append(("h2", "Models"))
    if m["models"]:
        b.append(("table", ["Model", "Declared as", "Version", "Line"],
                  [[x["name"], f"`{x['expr']}`", _model_status(x["expr"]), str(x["line"])] for x in m["models"]]))
    else:
        b.append(("p", "No model is declared at the top level; agents take their model as a parameter."))
    b.append(("p", "An agent's model may also arrive as a parameter (the read's `by`). In a regulated program every "
                   "model call is refused unless the model is pinned, and the model's identity is in each record."))

    for a in m["agents"]:
        b += _agent(a, types, m)

    if m["codes"]:
        b.append(("h2", "Reason codes"))
        used = {c["code"]: f"{a['name']}.{c['name']}" for a in m["agents"] for c in a["checks"] if c["code"]}
        b.append(("table", ["Code", "Customer-facing text", "Check"],
                  [[f"`{k}`", v, used.get(k, "(not used)")] for k, v in m["codes"].items()]))
        b.append(("p", "Codes are recorded in clear in each decision record, with this text, so a decision's "
                       "reasons survive the erasure of its personal data (`shio reasons RECORD`)."))

    from .policy import program_tables
    tables = program_tables(path)
    if tables:
        b.append(("h2", "Decision tables"))
        b.append(("p", "Limits and thresholds come from dated, sourced table versions in the policy files, not from "
                       "this program: " + ", ".join(f"`{t}`" for t in tables) + ". Each decision records the "
                       "version and the values it used; `shio policy check` lists the version in force on a date, "
                       "and `shio replay RECORD --policies DIR --at DATE` shows what a new version would decide."))
    classes = sorted({f["class"] for t in m["types"] for f in t["fields"] if f.get("class") and f["class"] != "PII"})
    if classes:
        b.append(("h2", "Data classes"))
        b.append(("p", "How each class is stored is set by the data-handling policy in force on the date of each "
                       "decision (`clear`, `vault` or `hashed`), not by this program: " +
                       ", ".join(f"`{c}`" for c in classes) + ". See `shio policy check` and `shio audit`."))

    if m["types"]:
        b.append(("h2", "Types the model fills"))
        for t in m["types"]:
            b.append(("h3", f"{t['name']} (line {t['line']})"))
            b.append(("table", ["Field", "Type", "Personal data", "Description"],
                      [[f["name"], f"`{f['type']}`", _data_class(f), f["description"]] for f in t["fields"]]))
    return b


def _agent(a, types, m):
    b = [("h2", f"Agent {a['name']} (line {a['line']})"),
         ("p", f"Inputs: `{a['params'] or 'none'}`")]
    readings = {r["name"] for r in a["reads"]}

    b.append(("h3", "What the model reads"))
    if not a["reads"]:
        b.append(("p", "This agent calls no model."))
    for r in a["reads"]:
        seen = "nothing but the prompt and sources (blind: an independent opinion)" if r["blind"] else \
            ("the prompt, the sources and earlier readings " + ", ".join(r["seeing"]) if r["seeing"]
             else "the prompt and the sources")
        t = types.get(r["type"].rstrip("?").strip())
        pii = [f["name"] for f in t["fields"] if f["pii"]] if t else []
        b.append(("ul", [f"**read {r['name']}** (line {r['line']}): `{r['type']}` by `{r['model']}`" +
                         (f", from `{r['sources']}`" if r["sources"] else "") +
                         (f", one per `{r['key']}`" if r["key"] else ""),
                         f"The model sees: {seen}",
                         "Personal data in this reading: " + (", ".join(pii) if pii else "none declared")]))
        b.append(("code", r["prompt"]))

    b.append(("h3", "Checks and whens"))
    if a["checks"]:
        b.append(("table", ["Name", "Kind", "Reason code", "Holds when", "Explanation if it fails", "Only after", "Line"],
                  [[c["name"] + (f"[{c['key']}]" if c["key"] else ""),
                    "check (a claim, and a training reward)" if c["kind"] == "check" else "when (steers only)",
                    f"`{c['code']}`" if c["code"] else "", f"`{c['expr']}`", _detail(c["detail"]),
                    ", ".join(c["after"]), str(c["line"])] for c in a["checks"]]))
    else:
        b.append(("p", "No checks: nothing the model proposes is verified."))

    b.append(("h3", "Outcomes and what leads to them"))
    rows = []
    every = sorted(c["name"] for c in a["checks"] if c["kind"] == "check")
    for r in a["rules"]:
        for expr, path in _outcomes(r["body"]):
            text = " ".join(path) + " " + expr
            deps = _names(text) & {c["name"] for c in a["checks"]} | _check_calls(path)
            if re.search(r"checks\.failures\(", text):
                deps |= set(every)
            rows.append([f"`{expr}`", f"rule {r['name']}" + (f"[{r['key']}]" if r["key"] else ""),
                         " and ".join(f"`{p}`" for p in path) or "always (when reached)",
                         ", ".join(sorted(deps)), _signoff_for(expr, a["signoffs"])])
    if rows:
        b.append(("table", ["Outcome", "Settled by", "When", "Checks and whens it depends on", "Sign-off"], rows))
    if a["returns"]:
        b.append(("p", "The agent returns: " + "; ".join(f"`{x['expr']}` (line {x['line']})" for x in a["returns"])))
    if not rows:
        b.append(("p", "No rule settles an outcome: the agent returns only plain values computed by code."))

    if a["signoffs"]:
        b.append(("h3", "Human sign-off"))
        b.append(("table", ["Outcome", "When", "Signed by role", "Can't be signed by", "Line"],
                  [[s["label"] if s["label"] != "*" else "any outcome", f"`{s['when']}`" if s["when"] else "always",
                    " and ".join(s["roles"]),
                    "the requester" + "".join(f", the outcome's {x}" for x in ["maker"] + s["makers"]),
                    str(s["line"])] for s in a["signoffs"]]))
        b.append(("p", "A decision that needs sign-off is returned as PENDING and recorded with a sign-off "
                       "request; `shio signoff` answers it with a signed entry in the ledger. A condition that can't "
                       "be evaluated needs sign-off (fail closed). Conditions are over the decided outcome and the "
                       "program's constants, never over what the model read."))

    b.append(("h3", "What the model is and isn't trusted with"))
    trusted, unchecked = [], []
    for r in a["reads"]:
        t = types.get(r["type"].rstrip("?").strip())
        in_checks, in_rules, whole = set(), set(), False
        for c in a["checks"]:
            f, w = _fields_used(c["expr"], r["name"])
            in_checks |= f
            whole |= w and not f
        for ru in a["rules"]:
            f, _ = _fields_used(ru["body"], r["name"], "exec")
            in_rules |= f
        fields = [f["name"] for f in t["fields"]] if t else []
        trusted.append(f"**{r['name']}**: proposes a value of type `{r['type']}`" +
                       (f"; checked fields: {', '.join(sorted(in_checks))}" if in_checks else "; no field is checked"))
        for f in sorted((in_rules - in_checks) & set(fields or in_rules)):
            unchecked.append(f"`{r['name']}.{f}` is used by a rule but no check verifies it")
    b.append(("p", "Trusted to **propose** values, nothing more:"))
    b.append(("ul", trusted or ["nothing: this agent reads no model"]))
    b.append(("p", "Not trusted to decide anything. Enforced by the compiler: a reading may be looked at only in a "
                   "check, a when, a rule or a later read's `seeing` clause; branching on it, storing it, formatting "
                   "it or returning it is a compile error, and the runtime refuses it again."))
    if unchecked:
        b.append(("p", "**For the reviewer**: model values a rule decides on without a check:"))
        b.append(("ul", unchecked))
    unread = [r for r in readings if not any(r in _names(c["expr"]) for c in a["checks"])
              and not any(r in _names(ru["body"], "exec") for ru in a["rules"])]
    if unread:
        b.append(("p", "Read but never checked or used by a rule: " + ", ".join(sorted(unread))))
    return b


def _detail(d):
    """A check's explanation as written, without the f-string wrapping the compiler gives it."""
    if not d:
        return ""
    try:
        node = ast.parse(d, mode="eval").body
    except SyntaxError:
        return d
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else "{" + ast.unparse(v.value) + "}"
                       for v in node.values)
    return d


def _label(expr):
    """The label an outcome expression gives (`signoff LABEL` matches it), if it is written as a literal."""
    try:
        node = ast.parse(expr, mode="eval").body
    except SyntaxError:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Dict):
        for k, v in zip(node.keys, node.values):
            if isinstance(k, ast.Constant) and k.value in ("status", "action", "decision", "outcome") \
                    and isinstance(v, ast.Constant) and isinstance(v.value, str):
                return v.value
    return None


def _signoff_for(expr, signoffs):
    label = _label(expr)
    hits = [s for s in signoffs if s["label"] == "*" or s["label"] == label]
    if not hits and label is None and signoffs:
        return "if its label matches: " + ", ".join(s["label"] for s in signoffs)
    return "; ".join(f"PENDING for {' and '.join(s['roles'])}" + (f" when `{s['when']}`" if s["when"] else "")
                     for s in hits)


def _check_calls(path):
    found = set()
    for p in path:
        found |= set(re.findall(r"checks\.(?:passed|failed|ran)\(\s*['\"](\w+)['\"]", p))
    return found


# ---------------- rendering ----------------
def _cell(x):
    return str(x).replace("|", "\\|").replace("\n", " ")


def markdown(blocks):
    out = []
    for blk in blocks:
        kind = blk[0]
        if kind in ("h1", "h2", "h3"):
            out.append("#" * int(kind[1]) + " " + blk[1])
        elif kind == "p":
            out.append(blk[1])
        elif kind == "ul":
            out.append("\n".join(f"- {x}" for x in blk[1]))
        elif kind == "code":
            out.append("```\n" + blk[1] + "\n```")
        elif kind == "table":
            head, rows = blk[1], blk[2]
            out.append("\n".join(["| " + " | ".join(head) + " |", "|" + "---|" * len(head)] +
                                 ["| " + " | ".join(_cell(c) for c in r) + " |" for r in rows]))
    return "\n\n".join(out) + "\n"


def _inline(text):
    t = html.escape(str(text))
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    return re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", t)


CSS = """
:root{--bg:#fff;--fg:#1d2330;--muted:#5b6577;--line:#e3e7ee;--code:#f4f6f9;--accent:#1f5fbf}
@media (prefers-color-scheme:dark){:root{--bg:#12161d;--fg:#e6e9ef;--muted:#9aa4b5;--line:#2a313d;--code:#1b212b;--accent:#7fb0ff}}
body{background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,-apple-system,Segoe UI,sans-serif;margin:0}
main{max-width:1040px;margin:0 auto;padding:32px 16px 64px}
h1{font-size:26px;margin:0 0 8px}h2{font-size:20px;margin:36px 0 8px;padding-top:12px;border-top:1px solid var(--line)}
h3{font-size:16px;margin:22px 0 6px;color:var(--accent)}p,li{color:var(--fg)}
.wrap{overflow-x:auto}table{border-collapse:collapse;width:100%;margin:8px 0;font-size:14px}
th,td{border:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}th{background:var(--code)}
code{background:var(--code);padding:1px 4px;border-radius:4px;font:13px ui-monospace,SFMono-Regular,Menlo,monospace}
pre{background:var(--code);padding:10px 12px;border-radius:6px;overflow-x:auto;white-space:pre-wrap}
pre code{background:none;padding:0}
"""


def to_html(blocks):
    title = blocks[0][1] if blocks and blocks[0][0] == "h1" else "Decision map"
    out = []
    for blk in blocks:
        kind = blk[0]
        if kind in ("h1", "h2", "h3"):
            out.append(f"<{kind}>{_inline(blk[1])}</{kind}>")
        elif kind == "p":
            out.append(f"<p>{_inline(blk[1])}</p>")
        elif kind == "ul":
            out.append("<ul>" + "".join(f"<li>{_inline(x)}</li>" for x in blk[1]) + "</ul>")
        elif kind == "code":
            out.append(f"<pre><code>{html.escape(blk[1])}</code></pre>")
        elif kind == "table":
            head = "".join(f"<th>{html.escape(h)}</th>" for h in blk[1])
            rows = "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in blk[2])
            out.append(f'<div class="wrap"><table><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table></div>')
    return (f'<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{html.escape(title)}</title><style>{CSS}</style></head>\n<body><main>\n" + "\n".join(out) +
            "\n</main></body></html>\n")


def explain(path, fmt="md"):
    blocks = build(path)
    return to_html(blocks) if fmt == "html" else markdown(blocks)


def _data_class(f):
    """How a field's data is governed: by its data class and the data-handling policy in force (shio policy)."""
    cls = f.get("class")
    if cls and cls != "PII":
        return f"**{cls}**: stored as the data-handling policy in force says (`shio policy check`)"
    if f.get("pii"):
        return "**pii**: hashed in records, whole only in the erasable vault"
    return ""
