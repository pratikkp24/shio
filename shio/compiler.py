"""The Shio compiler: .shio source -> Python AST, with line numbers mapped back to the .shio file.

Shio is Python's expression and statement language plus six forms:

  type   a structured value a model may propose (fields' trailing comments are their descriptions)
  model  a model declaration
  agent  a unit of work; the only place `read` may appear
  read   ask a model for a value: the result is a reading, a proposal
  check  a claim about readings that should hold; a boolean to the agent, a reward to `train`
  when   a condition over readings that only steers control flow (recorded, never a reward)
  rule   a block of code that settles an outcome from readings and checks
  train  fine-tune an open model on an agent's traces, with its checks as the reward
  main   the entry point for `shio run`

The boundary is checked here, before anything runs: inside an agent, a reading's name may appear only in a check,
a when, a rule, or a later read's `seeing` clause. Any other use (an `if`, an assignment, a call, an f-string, a return)
is a compile error, because that would let the model's proposal decide.
"""
import ast
import io
import re
import textwrap
import tokenize

PY_COMPOUND = re.compile(r"^(for|async\s+for|if|elif|else|while|with|async\s+with|try|except|finally|match|case)\b")
READ = re.compile(r"^read\s+(?P<name>[A-Za-z_]\w*)(?:\[(?P<key>.+?)\])?\s*:\s*(?P<type>.+?)"
                  r"(?:\s+from\s+(?P<src>.+?))?\s+by\s+(?P<model>.+?)(?P<blind>\s+blind)?"
                  r"(?:\s+seeing\s+(?P<seeing>[\w\s,]+?))?\s*:$")
CHECK = re.compile(r"^(?:check|when)\s+(?P<name>[A-Za-z_]\w*)(?:\[(?P<key>.+?)\])?\s*:\s*(?P<body>.+)$", re.S)
RULE = re.compile(r"^rule\s+(?P<name>[A-Za-z_]\w*)(?:\[(?P<key>.+?)\])?\s*:$")
AGENT = re.compile(r"^agent\s+(?P<name>[A-Za-z_]\w*)\s*\((?P<params>.*)\)\s*(?:->\s*(?P<ret>.+?))?\s*:$", re.S)
TYPE = re.compile(r"^type\s+(?P<name>[A-Za-z_]\w*)\s*:$")
MODEL = re.compile(r"^model\s+(?P<name>[A-Za-z_]\w*)\s*=\s*(?P<expr>.+)$", re.S)
TRAIN = re.compile(r"^train\s+(?P<agent>[A-Za-z_]\w*)\s+on\s+(?P<traces>.+?)\s+with\s+model\s+(?P<model>.+?)"
                   r"(?:\s+using\s+(?P<method>grpo|sft))?(?:\s+into\s+(?P<out>.+?))?\s*$")
FIELD = re.compile(r"^(?P<name>[A-Za-z_]\w*)\s*:\s*(?P<type>.+?)(?:\s*=\s*(?P<default>.+))?$")

PRELUDE = """import shio as __shio
from typing import Optional, Literal, List
from pydantic import BaseModel as _ShioType, Field as _ShioField
from shio.models import ollama, openai_compat, replay
from shio.core import Source
pdf = Source.from_pdf
__shio_train__ = []
"""


class ShioError(Exception):
    def __init__(self, msg, path, line, source_line=""):
        self.msg, self.path, self.line, self.source_line = msg, path, line, source_line
        super().__init__(self.format())

    def format(self):
        out = f"{self.path}:{self.line}: error: {self.msg}"
        if self.source_line.strip():
            out += f"\n    {self.line} | {self.source_line.rstrip()}"
        return out


def _strip_comment(text):
    """(code, comment) split at the first # outside a string."""
    q = None
    i = 0
    while i < len(text):
        c = text[i]
        if q:
            if c == "\\":
                i += 1
            elif text.startswith(q, i):
                i += len(q) - 1
                q = None
        elif c in "'\"":
            q = c * 3 if text.startswith(c * 3, i) else c
            i += len(q) - 1
        elif c == "#":
            return text[:i].rstrip(), text[i + 1:].strip()
        i += 1
    return text.rstrip(), ""


def _depth(text, state=None):
    """Bracket depth and open string quote at the end of `text`, continuing from `state`."""
    depth, q = state or (0, None)
    i = 0
    while i < len(text):
        c = text[i]
        if q:
            if c == "\\":
                i += 1
            elif text.startswith(q, i):
                i += len(q) - 1
                q = None
            elif len(q) == 1 and c == "\n":
                q = None
        elif c == "#":
            while i < len(text) and text[i] != "\n":
                i += 1
            continue
        elif c in "'\"":
            q = c * 3 if text.startswith(c * 3, i) else c
            i += len(q) - 1
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        i += 1
    return depth, q


def _split_top(text, sep):
    """Split at `sep` where it is outside brackets and strings."""
    parts, buf, state = [], "", (0, None)
    for c in text:
        if c == sep and state == (0, None):
            parts.append(buf)
            buf = ""
            continue
        buf += c
        state = _depth(c, state)
    return parts + [buf]


def _free_names(code, mode="exec"):
    """Names a fragment reads but does not bind itself."""
    tree = ast.parse(code, mode=mode)
    loads, stores = [], set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Name):
            (loads.append(n.id) if isinstance(n.ctx, ast.Load) else stores.add(n.id))
        elif isinstance(n, ast.arg):
            stores.add(n.arg)
        elif isinstance(n, (ast.FunctionDef, ast.ClassDef)):
            stores.add(n.name)
    return [x for x in dict.fromkeys(loads) if x not in stores]


class _Locals(ast.NodeVisitor):
    """Names bound in a function body, not counting comprehensions, lambdas and nested definitions."""

    def __init__(self):
        self.names = set()

    def visit_Name(self, n):
        if not isinstance(n.ctx, ast.Load):
            self.names.add(n.id)

    def visit_FunctionDef(self, n):
        self.names.add(n.name)

    visit_AsyncFunctionDef = visit_ClassDef = visit_FunctionDef

    def visit_Lambda(self, n):
        pass

    visit_ListComp = visit_SetComp = visit_DictComp = visit_GeneratorExp = visit_Lambda

    def visit_ExceptHandler(self, n):
        if n.name:
            self.names.add(n.name)
        self.generic_visit(n)

    def visit_alias(self, n):
        self.names.add((n.asname or n.name).split(".")[0])


class Compiler:
    def __init__(self, source, path="<shio>"):
        self.path = path
        self.src = source.expandtabs(4).splitlines()
        self.out = []          # [text, shio line]
        self.tail = []         # module-level helpers emitted after everything else (checks, rules)
        self.agents = set()
        self.trains = []

    # ---------------- helpers ----------------
    def error(self, line, msg):
        raise ShioError(msg, self.path, line, self.src[line - 1] if 0 < line <= len(self.src) else "")

    def emit(self, level, text, line, into=None):
        lines = text.split("\n")
        target = self.out if into is None else into
        target.append(["    " * level + lines[0], line])
        for extra in lines[1:]:          # continuation lines of a bracketed statement keep their own layout
            target.append([extra, line])
        return len(target) - len(lines)

    @staticmethod
    def indent(s):
        return len(s) - len(s.lstrip(" "))

    def blank(self, i):
        s = self.src[i].strip()
        return not s or s.startswith("#")

    def logical(self, i):
        """The logical line starting at physical line i (bracketed continuations joined). -> (text, next i)."""
        text = self.src[i].strip()
        state = _depth(self.src[i])
        j = i + 1
        while (state[0] > 0 or state[1] or text.endswith("\\")) and j < len(self.src):
            text += "\n" + self.src[j]
            state = _depth("\n" + self.src[j], state)
            j += 1
        if state[0] > 0 or state[1]:
            self.error(i + 1, "unclosed bracket or string")
        return text, j

    def block(self, i, parent_indent):
        """Physical lines after i that belong to a block indented deeper than parent_indent. -> (lines, next i)."""
        j = i
        while j < len(self.src) and (not self.src[j].strip() or self.indent(self.src[j]) > parent_indent):
            j += 1
        while j > i and not self.src[j - 1].strip():
            j -= 1
        return self.src[i:j], j

    def scan(self, text, line, readings, where):
        """The boundary: a reading's name may not appear in plain code."""
        if not readings:
            return
        try:
            toks = list(tokenize.generate_tokens(io.StringIO(text).readline))
        except (tokenize.TokenError, SyntaxError, IndentationError):
            toks = []
        for k, t in enumerate(toks):
            if t.type != tokenize.NAME or t.string not in readings:
                continue
            prev = toks[k - 1].string if k else ""
            nxt = toks[k + 1].string if k + 1 < len(toks) else ""
            if prev == "." or (nxt == "=" and prev in ("(", ",")):
                continue
            self.error(line, f"'{t.string}' is a model reading and cannot be used in {where}. "
                             f"A reading can only be looked at in a check, a when, a rule, or a later read's 'seeing' clause; "
                             f"write a check or a rule that decides from it.")

    def py(self, code, line, mode="exec"):
        try:
            ast.parse(code, mode=mode)
        except SyntaxError as e:
            self.error(line + (e.lineno or 1) - 1, f"invalid syntax: {e.msg}")

    # ---------------- top level ----------------
    def compile(self):
        i = 0
        while i < len(self.src):
            if self.blank(i):
                i += 1
                continue
            line = i + 1
            if self.indent(self.src[i]):
                self.error(line, "unexpected indent")
            text, j = self.logical(i)
            word = text.split(None, 1)[0].rstrip(":")
            if word == "type":
                i = self.type_decl(i, text)
            elif word == "model":
                m = MODEL.match(text) or self.error(line, "expected: model NAME = EXPRESSION")
                self.py(m["expr"], line, "eval")
                self.emit(0, f"{m['name']} = {m['expr']}", line)
                i = j
            elif word == "agent":
                i = self.agent_decl(i, j, text)
            elif text == "main:":
                self.emit(0, "def __shio_main__():", line)
                body, i = self.block(j, 0)
                if not any(s.strip() and not s.strip().startswith("#") for s in body):
                    self.error(line, "main: needs a body")
                self.body(j, 0, 1, {"kind": "main", "readings": set(), "agent": None})
            elif word == "train":
                m = TRAIN.match(text) or self.error(
                    line, 'expected: train AGENT on "traces/*.json" with model "Qwen/Qwen3-8B" [using grpo|sft] [into "dir"]')
                self.trains.append((m["agent"], line))
                self.emit(0, f"__shio_train__.append(dict(agent={m['agent']!r}, traces={m['traces']}, model={m['model']}, "
                             f"method={(m['method'] or 'grpo')!r}, out={m['out'] or 'None'}, line={line}))", line)
                i = j
            elif word in ("read", "check", "rule", "when"):
                self.error(line, f"'{word}' can only be used inside an agent")
            else:
                # plain Python (import, def, class, constants): passed through as written
                self.scan_block_for_shio(i, j)
                body, k = self.block(j, 0) if text.endswith(":") else ([], j)
                for n in range(i, k):
                    self.out.append([self.src[n], n + 1])
                i = k
        for name, line in self.trains:
            if name not in self.agents:
                self.error(line, f"train names agent '{name}', which is not defined in this file")
        self.out.extend(self.tail)
        return self.out

    def scan_block_for_shio(self, i, j):
        _, k = self.block(j, 0)
        for n in range(j, k):
            s = self.src[n].strip()
            if re.match(r"^(read|check|rule|when)\s+\w+.*:", s):
                self.error(n + 1, f"'{s.split()[0]}' can only be used inside an agent")

    def type_decl(self, i, header):
        line = i + 1
        m = TYPE.match(header) or self.error(line, "expected: type NAME:")
        self.emit(0, f"class {m['name']}(_ShioType):", line)
        body, k = self.block(i + 1, 0)
        fields = 0
        for n, raw in enumerate(body, i + 2):
            s = raw.strip()
            if not s or s.startswith("#"):
                continue
            if s[0] in "'\"" and not fields:
                self.emit(1, s, n)       # docstring: the type's description
                continue
            code, desc = _strip_comment(s)
            f = FIELD.match(code) or self.error(n, "expected a field: name: type [= default]  # description")
            t = self.type_expr(f["type"].strip(), n)
            args = ([f["default"]] if f["default"] else []) + ([f"description={desc!r}"] if desc else [])
            self.emit(1, f"{f['name']}: {t}" + (f" = _ShioField({', '.join(args)})" if args else ""), n)
            fields += 1
        if not fields:
            self.error(line, f"type {m['name']} has no fields")
        return k

    def type_expr(self, t, line):
        if t.endswith("?"):
            return f"Optional[{self.type_expr(t[:-1].strip(), line)}]"
        parts = [p.strip() for p in _split_top(t, "|")]
        if len(parts) > 1 and all(p[:1] in "'\"" for p in parts):
            return f"Literal[{', '.join(parts)}]"
        self.py(t, line, "eval")
        return t

    # ---------------- agents ----------------
    def agent_decl(self, i, j, header):
        line = i + 1
        m = AGENT.match(header) or self.error(line, "expected: agent NAME(PARAMS) [-> TYPE]:")
        name = m["name"]
        self.agents.add(name)
        body, k = self.block(j, 0)
        readings = self.reads_in(body, start=j)
        self.emit(0, f"@__shio.agent(name={name!r})", line)
        start = self.emit(0, f"def {name}({m['params']}):", line)
        if readings:
            self.emit(1, " = ".join(sorted(readings)) + " = None", line)
        ctx = {"kind": "agent", "readings": readings, "agent": name, "pending": []}
        self.body(j, 0, 1, ctx)
        # now that the whole body is known, give each check and rule the agent locals it uses
        fn_src = "\n".join(t for t, _ in self.out[start:])
        try:
            tree = ast.parse(fn_src)
        except SyntaxError as e:
            self.error(self.out[start + (e.lineno or 1) - 1][1], f"invalid syntax: {e.msg}")
        v = _Locals()
        fn = tree.body[0]
        for a in fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs + [fn.args.vararg, fn.args.kwarg]:
            if a:
                v.names.add(a.arg)
        for stmt in fn.body:
            v.visit(stmt)
        scope = v.names | readings
        for p in ctx["pending"]:
            params = [x for x in p["free"] if x in scope]
            call_args = ", ".join(params + ([f"key={p['key']}"] if p["key"] else []))
            row = self.out[p["slot"]]
            row[0] = row[0].replace(p["placeholder"], f"{p['fn']}({call_args})")
            sig = ", ".join((["checks"] if p["kind"] == "rule" else []) + params)
            self.emit(0, "", p["line"], self.tail)
            self.emit(0, p["decorator"], p["line"], self.tail)
            self.emit(0, f"def {p['fn']}({sig}):", p["line"], self.tail)
            for text, ln in p["body"]:
                self.emit(1, text, ln, self.tail)
        return k

    def reads_in(self, lines, start=0):
        names = set()
        for raw in lines:
            r = re.match(r"^\s*read\s+([A-Za-z_]\w*)", raw)
            if r and READ.match(raw.strip()):
                names.add(r.group(1))
        return names

    def body(self, i, parent_indent, level, ctx):
        """Compile the statements of a block (agent, main, or a compound statement inside them)."""
        start = i
        own = None
        while i < len(self.src):
            if self.blank(i):
                i += 1
                continue
            ind = self.indent(self.src[i])
            if ind <= parent_indent:
                break
            if own is None:
                own = ind
            elif ind != own:
                self.error(i + 1, "unexpected indent" if ind > own else "unindent does not match any outer level")
            line = i + 1
            text, j = self.logical(i)
            word = text.split(None, 1)[0].rstrip(":")
            readings, kind = ctx["readings"], ctx["kind"]
            if word in ("read", "check", "rule", "when") and READ.match(text) or CHECK.match(text) and \
                    word in ("check", "when") or RULE.match(text) and word == "rule":
                if kind != "agent":
                    self.error(line, f"'{word}' can only be used inside an agent")
                if ctx.get("in_rule"):
                    self.error(line, f"'{word}' cannot be used inside a rule")
                i = getattr(self, f"stmt_{'check' if word == 'when' else word}")(i, j, text, ind, level, ctx)
                continue
            if word == "read":
                self.error(line, "expected: read NAME[KEY]: TYPE from SOURCES by MODEL [blind] [seeing A, B]:  "
                                 "followed by the prompt, indented")
            if word in ("type", "agent", "model", "train") and re.match(rf"^{word}\s+\w", text):
                self.error(line, f"'{word}' is only allowed at the top level")
            where = "a return value" if word == "return" else "plain code"
            if PY_COMPOUND.match(text) and text.endswith(":"):
                self.scan(text.rstrip(":") if word in ("if", "elif", "while", "for") else text, line, readings, where)
                self.emit(level, text, line)
                if word in ("for", "while", "async") and kind == "agent":
                    # readings belong to one iteration: one not taken this time is null, never last time's
                    inner = sorted(self.reads_in(*self.block(j, ind)[0:1], start=j))
                    if inner:
                        self.emit(level + 1, " = ".join(inner) + " = None", line)
                i = self.body(j, ind, level + 1, ctx)
                continue
            if re.match(r"^(def|class|async\s+def)\b", text):
                blk, k = self.block(j, ind)
                self.scan("\n".join([text] + blk), line, readings, "a nested function")
                self.emit(level, text, line)
                for n, raw in enumerate(blk, j + 1):
                    self.emit(level, raw[ind:] if raw.strip() else "", n)
                i = k
                continue
            self.scan(text, line, readings, where)
            self.emit(level, text, line)
            i = j
        if own is None:
            self.error(start, "expected an indented block")
        return i

    def prompt_expr(self, lines, line, readings):
        """Indented prompt text -> a Python expression. {expr} interpolates plain values; {{ and }} are braces."""
        text = textwrap.dedent("\n".join(lines)).strip("\n")
        if not text.strip():
            self.error(line, "this read has no prompt; write it indented under the read")
        parts, buf, k = [], "", 0
        while k < len(text):
            c = text[k]
            if text.startswith("{{", k) or text.startswith("}}", k):
                buf += c
                k += 2
                continue
            if c == "{":
                depth, e = 1, k + 1
                while e < len(text) and depth:
                    depth += {"{": 1, "}": -1}.get(text[e], 0)
                    e += 1
                if depth:
                    self.error(line, "unclosed { in prompt")
                expr = text[k + 1:e - 1].strip()
                spec = ""
                pieces = _split_top(expr, ":")
                if len(pieces) > 1 and not expr.startswith("lambda"):
                    expr, spec = ":".join(pieces[:-1]).strip(), pieces[-1]
                self.py(expr, line, "eval")
                self.scan(expr, line, readings, "a prompt (use 'seeing' to show the model another reading)")
                if buf:
                    parts.append(repr(buf))
                parts.append(f"format({expr}, {spec!r})")
                buf = ""
                k = e
                continue
            buf += c
            k += 1
        if buf:
            parts.append(repr(buf))
        return parts[0] if len(parts) == 1 and parts[0][0] in "'\"" else f"''.join([{', '.join(parts)}])"

    def stmt_read(self, i, j, text, ind, level, ctx):
        line = i + 1
        m = READ.match(text)
        lines, k = self.block(j, ind)
        prompt = self.prompt_expr(lines, line, ctx["readings"])
        t = self.type_expr(m["type"].strip(), line)
        for part in ("src", "model", "key"):
            if m[part]:
                self.py(m[part], line, "eval")
                self.scan(m[part], line, ctx["readings"], "a read's sources, model or key")
        seeing = [s.strip() for s in (m["seeing"] or "").split(",") if s.strip()]
        for s in seeing:
            if s not in ctx["readings"]:
                self.error(line, f"'seeing {s}': {s} is not a reading in this agent")
        if m["blind"] and seeing:
            self.error(line, "a blind read cannot also be 'seeing' other readings")
        args = [t, prompt, f"by={m['model']}", f"name={m['name']!r}"]
        if m["src"]:
            args.append(f"sources=({m['src']},)")
        if m["key"]:
            args.append(f"key={m['key']}")
        if m["blind"]:
            args.append("blind=True")
        if seeing:
            args.append("context={" + ", ".join(f"{s!r}: {s}" for s in seeing) + "}")
        self.emit(level, f"{m['name']} = __shio.read({', '.join(args)})", line)
        return k

    def _pending(self, ctx, kind, name, key, line, free, body, level, reward=True):
        n = len(ctx["pending"]) + 1
        fn = f"__shio_{kind}_{ctx['agent']}_{name}_{n}"
        ph = f"__SHIO_PENDING_{n}__"
        slot = self.emit(level, f"{name} = {ph}", line)
        deco = f"@__shio.{kind}(name={name!r}" + ("" if reward else ", reward=False") + ")"
        ctx["pending"].append({"kind": kind, "fn": fn, "placeholder": ph, "slot": slot, "key": key, "line": line,
                               "free": free, "body": body, "decorator": deco})

    def stmt_check(self, i, j, text, ind, level, ctx):
        line = i + 1
        m = CHECK.match(text)
        body = m["body"].strip()
        pieces = _split_top(body, ",")
        detail = None
        if len(pieces) > 1 and re.match(r"^\s*[rRfF]?(['\"])", pieces[-1]):
            body, detail = ",".join(pieces[:-1]).strip(), pieces[-1].strip()
            if not detail.lower().startswith(("f", "rf", "fr")):
                detail = "f" + detail
        self.py(body, line, "eval")
        if m["key"]:
            self.py(m["key"], line, "eval")
            self.scan(m["key"], line, ctx["readings"], "a check's key")
        free = _free_names(body, "eval") + (_free_names(detail, "eval") if detail else [])
        ret = f"return bool({body})" + (f", (lambda: {detail})" if detail else "")
        self._pending(ctx, "check", m["name"], m["key"], line, list(dict.fromkeys(free)), [(ret, line)], level,
                      reward=text.startswith("check"))
        return j

    def stmt_rule(self, i, j, text, ind, level, ctx):
        line = i + 1
        m = RULE.match(text)
        lines, k = self.block(j, ind)
        if not any(s.strip() for s in lines):
            self.error(line, "a rule needs a body that returns the outcome")
        code = textwrap.dedent("\n".join(lines))
        for n, raw in enumerate(lines, j + 1):
            s = raw.strip()
            if re.match(r"^(read|check|rule|when)\s+\w+.*:", s):
                self.error(n, f"'{s.split()[0]}' cannot be used inside a rule")
        try:
            tree = ast.parse(f"def _r():\n{textwrap.indent(code, '    ')}")
        except SyntaxError as e:
            self.error(j + (e.lineno or 2) - 1, f"invalid syntax: {e.msg}")
        if not any(isinstance(n, ast.Return) and n.value is not None for n in ast.walk(tree)):
            self.error(line, f"rule {m['name']} must return the outcome")
        if m["key"]:
            self.py(m["key"], line, "eval")
            self.scan(m["key"], line, ctx["readings"], "a rule's key")
        free = [x for x in _free_names(code) if x != "checks"]
        base = min(self.indent(s) for s in lines if s.strip())
        body = [(raw[base:] if raw.strip() else "", n) for n, raw in enumerate(lines, j + 1)]
        self._pending(ctx, "rule", m["name"], m["key"], line, free, body, level)
        return k


def compile_source(source, path="<shio>"):
    """-> (python source text, list mapping each python line to its .shio line)."""
    c = Compiler(source, path)
    rows = c.compile()
    py = PRELUDE + "\n".join(t for t, _ in rows) + "\n"
    lines = [0] * PRELUDE.count("\n") + [ln for _, ln in rows]
    return py, lines


def to_code(source, path="<shio>"):
    py, lines = compile_source(source, path)
    try:
        tree = ast.parse(py)
    except SyntaxError as e:
        ln = lines[e.lineno - 1] if e.lineno and e.lineno <= len(lines) else 1
        raise ShioError(f"invalid syntax: {e.msg}", path, ln, source.splitlines()[ln - 1] if ln else "")
    for node in ast.walk(tree):
        if hasattr(node, "lineno"):
            node.lineno = lines[node.lineno - 1] or 1
            node.end_lineno = lines[(node.end_lineno or node.lineno) - 1] or node.lineno
            if node.end_lineno < node.lineno:
                node.end_lineno = node.lineno
            if node.end_lineno == node.lineno and getattr(node, "end_col_offset", None) is not None \
                    and node.end_col_offset < node.col_offset:
                node.end_col_offset = node.col_offset
    return compile(tree, path, "exec")
