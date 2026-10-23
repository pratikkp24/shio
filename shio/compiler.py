"""The Shio compiler: .shio source -> Python AST, with line numbers mapped back to the .shio file.

Shio is Python's expression and statement language plus six forms:

  type   a structured value a model may propose (fields' trailing comments are their descriptions)
  model  a model declaration
  agent  a unit of work; the only place `read` may appear
  read   ask a model for a value: the result is a reading, a proposal
  check  a claim about readings that should hold; a boolean to the agent, a reward to `train`.
         `check b after a:` is skipped (not failed) when check a failed, so one unknown value does not
         set off a cascade of misleading failures
  when   a condition over readings that only steers control flow (recorded, never a reward)
  rule   a block of code that settles an outcome from readings and checks
  train  fine-tune an open model on an agent's traces, with its checks as the reward
  main   the entry point for `shio run`

Regulated programs add three more (docs/REGULATED.md):

  check NAME as CODE:   a stable reason code, recorded in clear so the reason survives erasure
  codes                 the program's table of reason codes and their customer-facing text
  signoff               an outcome that needs a person: `signoff PAY when amount > 10000 by role "controller"`

The boundary is checked here, before anything runs: inside an agent, a reading's name may appear only in a check,
a when, a rule, or a later read's `seeing` clause. Any other use (an `if`, an assignment, a call, an f-string, a return)
is a compile error, because that would let the model's proposal decide.
"""
import ast
import builtins
import io
import re
import textwrap
import tokenize
import unicodedata

from .core import MUTATING_METHODS

PY_COMPOUND = re.compile(r"^(for|async\s+for|if|elif|else|while|with|async\s+with|try|except|finally|match|case)\b")
READ = re.compile(r"^read\s+(?P<name>[A-Za-z_]\w*)(?:\[(?P<key>.+?)\])?\s*:\s*(?P<type>.+?)"
                  r"(?:\s+from\s+(?P<src>.+?))?\s+by\s+(?P<model>.+?)(?P<blind>\s+blind)?"
                  r"(?:\s+seeing\s+(?P<seeing>[\w\s,]+?))?\s*:$")
CHECK = re.compile(r"^(?:check|when)\s+(?P<name>[A-Za-z_]\w*)(?:\[(?P<key>.+?)\])?(?:\s+as\s+(?P<code>[A-Za-z_]\w*))?"
                   r"(?:\s+after\s+(?P<after>[A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*))?\s*:\s*(?P<body>.+)$", re.S)
# for item in reading.path:   /   for i, item in enumerate(reading.path):
EACH = re.compile(r"^for\s+(?:(?P<idx>[A-Za-z_]\w*)\s*,\s*)?(?P<var>[A-Za-z_]\w*)\s+in\s+(?P<enum>enumerate\(\s*)?"
                  r"(?P<root>[A-Za-z_]\w*)(?P<path>(?:\.[A-Za-z_]\w*)*)\s*(?(enum)\))\s*:$")
RULE = re.compile(r"^rule\s+(?P<name>[A-Za-z_]\w*)(?:\[(?P<key>.+?)\])?\s*:$")
AGENT = re.compile(r"^(?P<async>async\s+)?agent\s+(?P<name>[A-Za-z_]\w*)\s*\((?P<params>.*)\)\s*(?:->\s*(?P<ret>.+?))?\s*:$", re.S)
TYPE = re.compile(r"^type\s+(?P<name>[A-Za-z_]\w*)\s*:$")
MODEL = re.compile(r"^model\s+(?P<name>[A-Za-z_]\w*)\s*=\s*(?P<expr>.+)$", re.S)
TRAIN = re.compile(r"^train\s+(?P<agent>[A-Za-z_]\w*)\s+on\s+(?P<traces>.+?)\s+with\s+model\s+(?P<model>.+?)"
                   r"(?:\s+using\s+(?P<method>grpo|sft))?(?:\s+into\s+(?P<out>.+?))?\s*$")
# signoff PAY [when CONDITION] by role "a" [and "b"] [maker FIELD, FIELD]
SIGNOFF = re.compile(r"^signoff\s+(?P<label>[A-Za-z_]\w*|\*)(?:\s+when\s+(?P<cond>.+?))?\s+by\s+roles?\s+"
                     r"(?P<roles>(?:\"[^\"]+\"|'[^']+')(?:\s*(?:,|and)\s*(?:\"[^\"]+\"|'[^']+'))*)"
                     r"(?:\s+maker\s+(?P<makers>[A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*))?\s*$", re.S)
TOOL = re.compile(r"^tool\s+(?P<name>[A-Za-z_]\w*)\s*\((?P<args>[^)]*)\)\s+(?P<mode>reads|acts)"
                  r"(?:\s+(?P<output>trusted|untrusted|mixed))?(?P<opens>\s+opens)?\s*=\s*(?P<impl>.+)$")
TOOL_ARG = re.compile(r"^(?P<name>[A-Za-z_]\w*)\s*:\s*(?P<kind>[A-Za-z_]\w*)$")
TOOL_KINDS = {"email", "url", "iban", "name", "text", "str", "int", "float", "bool", "dict", "list"}
GUARD = re.compile(r"^guard\s+(?P<req>.+?)\s+by\s+(?P<model>.+?)(?:\s+approved\s+by\s+(?P<approver>.+?))?\s*$")
CALL = re.compile(r"\bcall\s+(?P<name>[A-Za-z_]\w*)\s*\(")
CODE_ROW = re.compile(r"^(?P<code>[A-Za-z_]\w*)\s*:\s*(?P<text>.+)$")
FIELD = re.compile(r"^(?P<name>[A-Za-z_]\w*)\s*:\s*(?P<type>.+?)(?:\s*=\s*(?P<default>.+))?$")

PRELUDE = """import shio as __shio
from typing import Optional, Literal, List, ClassVar
from pydantic import BaseModel as _ShioType, Field as _ShioField
from shio.models import ollama, openai_compat, replay
from shio.core import Source, in_force
from shio.guard import GateHeld, GateDenied
from shio.core import checked_getattr as getattr
pdf = Source.from_pdf
image = Source.image
__shio_train__ = []
__shio_tools__ = {}
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


def _comprehension_names(code):
    """Names bound by comprehensions and lambdas inside a fragment of code."""
    for attempt in (code, code.rstrip().rstrip(":") + ": pass", code.rstrip().rstrip(":")):
        try:
            tree = ast.parse(attempt)
            break
        except SyntaxError:
            tree = None
    names = set()
    for n in ast.walk(tree) if tree else []:
        if isinstance(n, ast.comprehension):
            names |= {x.id for x in ast.walk(n.target) if isinstance(x, ast.Name)}
        elif isinstance(n, ast.Lambda):
            names |= {a.arg for a in n.args.args}
    return names


def _bound_names(tree):
    """Every name a module binds anywhere (lenient: a name bound in any scope counts)."""
    names = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and not isinstance(n.ctx, ast.Load):
            names.add(n.id)
        elif isinstance(n, ast.arg):
            names.add(n.arg)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(n.name)
        elif isinstance(n, ast.alias):
            names.add((n.asname or n.name).split(".")[0])
        elif isinstance(n, ast.ExceptHandler) and n.name:
            names.add(n.name)
        elif isinstance(n, ast.ImportFrom) and any(a.name == "*" for a in n.names):
            return None          # a star import: anything may be defined
    return names


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
        # known before anything is compiled, so rules that depend on it hold wherever the pragma sits in the file
        self.regulated = any(line.split("#", 1)[0].strip() == "regulated" and not line[:1].isspace()
                             for line in self.src)
        self.uses = []         # (names, line, where) used in prompts and checks, verified once the module is known
        self.pure_uses = []    # (expression tree, line, where) of every check and when, for _check_helpers
        self.codes = None      # the program's reason-code table, {CODE: text}, if it declares one
        self.code_uses = []    # (code, line) from `check NAME as CODE`
        # what `shio explain` documents: every declaration, read, check, rule and sign-off, with its .shio line
        self.meta = {"types": [], "models": [], "agents": [], "regulated": False, "codes": {}, "tools": []}
        # declared tools, known before anything is compiled, so an agent may call a tool declared further down
        self.tools = {}
        for line in self.src:
            m = TOOL.match(line.split("#", 1)[0].strip()) if line[:1] == "t" else None
            if m:
                self.tools[m["name"]] = m["mode"]

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

    def block(self, i, parent_indent, raw=False):
        """Lines after i that belong to a block indented deeper than parent_indent. -> (lines, next i).
        A statement's continuation lines (inside brackets or a triple-quoted string) belong to it whatever their
        indentation; raw=True (prompt text, type bodies) goes by indentation alone."""
        j = i
        while j < len(self.src):
            if not self.src[j].strip():
                j += 1
                continue
            if self.indent(self.src[j]) <= parent_indent:
                break
            if raw:
                j += 1
                continue
            try:
                j = self.logical(j)[1]
            except ShioError:
                j += 1
        while j > i and not self.src[j - 1].strip():
            j -= 1
        return self.src[i:j], j

    @staticmethod
    def code(text):
        """A one-line statement without its trailing comment (multi-line statements are left as they are)."""
        return _strip_comment(text)[0] if "\n" not in text else text

    def scan(self, text, line, readings, where):
        """The boundary: a reading's name may not appear in plain code."""
        if not readings:
            return
        try:
            toks = list(tokenize.generate_tokens(io.StringIO(text).readline))
        except (tokenize.TokenError, SyntaxError, IndentationError):
            toks = []
        local = _comprehension_names(text)
        for k, t in enumerate(toks):
            if t.type != tokenize.NAME or t.string not in readings or t.string in local:
                continue
            prev = toks[k - 1].string if k else ""
            nxt = toks[k + 1].string if k + 1 < len(toks) else ""
            if prev == "." or (nxt == "=" and prev in ("(", ",")):
                continue
            self.error(line, f"'{t.string}' is a model reading and cannot be used in {where}. "
                             f"A reading can only be looked at in a check, a when, a rule, or a later read's 'seeing' clause; "
                             f"write a check or a rule that decides from it.")

    MUTATORS = MUTATING_METHODS

    def pure(self, code, line, where):
        """A check or when only decides: it may not assign (:=), or take a method that changes something (called or
        not) or any dunder method, which could carry a reading's value out into plain code. Functions this file
        defines that the check uses are checked too, once the whole module is known (_check_helpers)."""
        try:
            tree = ast.parse(code, mode="eval")
        except SyntaxError:
            return               # reported by py()
        for n in ast.walk(tree):
            bad = _impure_node(n)
            if isinstance(n, ast.NamedExpr):
                bad = "':=' assigns"
            elif isinstance(n, ast.Attribute) and n.attr.startswith("__"):
                bad = f"'.{n.attr}' is a dunder method, which can change its object"
            elif isinstance(n, ast.Attribute) and n.attr in self.MUTATORS:
                bad = f".{n.attr}() changes something"
            if bad:
                self.error(line, f"{where} only decides, so it cannot change anything: {bad}. "
                                 f"Move it to a rule, after the check.")
        self.pure_uses.append((tree, line, where))

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
            text = self.code(text)
            word = text.split(None, 1)[0].rstrip(":")
            if word == "type":
                i = self.type_decl(i, text)
            elif word == "model":
                m = MODEL.match(text) or self.error(line, "expected: model NAME = EXPRESSION")
                self.py(m["expr"], line, "eval")
                self.emit(0, f"{m['name']} = {m['expr']}", line)
                self.meta["models"].append({"name": m["name"], "expr": " ".join(m["expr"].split()), "line": line})
                i = j
            elif word == "agent" or word == "async" and re.match(r"^async\s+agent\s", text):
                i = self.agent_decl(i, j, text)
            elif word == "tool" and re.match(r"^tool\s+[A-Za-z_]\w*\s*\(", text):
                i = self.tool_decl(line, text, j)
            elif text == "main:":
                self.emit(0, "def __shio_main__():", line)
                body, _ = self.block(j, 0)
                if not any(s.strip() and not s.strip().startswith("#") for s in body):
                    self.error(line, "main: needs a body")
                i = self.body(j, 0, 1, {"kind": "main", "readings": set(), "agent": None})
            elif text == "regulated":
                # the program declares itself regulated: the escape hatches are compile errors, and at run time it
                # needs a signing key and pinned models (see shio.ledger and docs/REGULATED.md)
                self.regulated = self.meta["regulated"] = True
                self.emit(0, "pass  # regulated", line)
                i = j
            elif word == "train":
                m = TRAIN.match(text) or self.error(
                    line, 'expected: train AGENT on "traces/*.json" with model "Qwen/Qwen3-8B" [using grpo|sft] [into "dir"]')
                self.trains.append((m["agent"], line))
                self.emit(0, f"__shio_train__.append(dict(agent={m['agent']!r}, traces={m['traces']}, model={m['model']}, "
                             f"method={(m['method'] or 'grpo')!r}, out={m['out'] or 'None'}, line={line}))", line)
                i = j
            elif text == "codes:":
                i = self.codes_decl(i, j)
            elif word in ("read", "check", "rule", "when", "signoff") or word == "guard" and GUARD.match(text):
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
        if self.codes is not None:
            for code, line in self.code_uses:
                if code not in self.codes:
                    self.error(line, f"reason code {code} is not in the program's codes: table")
        elif self.regulated and self.code_uses:
            self.error(self.code_uses[0][1], "a regulated program that uses reason codes must declare them in a "
                                             "codes: table, with the text a customer is given for each")
        self.out.extend(self.tail)
        return self.out

    def tool_decl(self, line, text, j):
        m = TOOL.match(text) or self.error(
            line, "expected: tool NAME(ARG: KIND, ...) reads [trusted|untrusted|mixed] [opens] | acts = python.callable")
        args = []
        for a in [x.strip() for x in m["args"].split(",") if x.strip()]:
            am = TOOL_ARG.match(a) or self.error(line, f"expected an argument as NAME: KIND, got {a!r}")
            if am["kind"] not in TOOL_KINDS:
                self.error(line, f"argument {am['name']}: kind must be one of {', '.join(sorted(TOOL_KINDS))}")
            args.append((am["name"], am["kind"]))
        if m["mode"] == "acts" and (m["output"] or m["opens"]):
            self.error(line, "an acts tool takes no trusted/untrusted/mixed or opens: those describe reading tools")
        if m["opens"] and not any(k == "url" for _, k in args):
            self.error(line, "an opens tool needs a url argument: it reads the page at that address")
        self.py(m["impl"], line, "eval")
        self.uses.append((_free_names(m["impl"], "eval"), line, f"tool {m['name']}"))
        decl = (f"__shio_tools__[{m['name']!r}] = (__shio.guard.declare({m['name']!r}, {args!r}, {m['mode']!r}, "
                f"{(m['output'] or 'untrusted')!r}, {bool(m['opens'])}), lambda: {m['impl']})")
        self.emit(0, decl, line)
        self.meta["tools"].append({"name": m["name"], "args": args, "mode": m["mode"], "output": m["output"] or
                                   "untrusted", "opens": bool(m["opens"]), "impl": m["impl"].strip(), "line": line})
        return j

    def calls(self, text, line, in_rule):
        """`call NAME(` -> the declared tool's runner. An acts tool is called only inside a rule."""
        def repl(m):
            name = m["name"]
            if name not in self.tools:
                self.error(line, f"'{name}' is not a declared tool: declare it with `tool {name}(...) reads|acts = ...`")
            if self.tools[name] == "acts" and not in_rule:
                self.error(line, f"{name} acts on the world, so it can only be called inside a rule, "
                                 f"where the decision to act is written down")
            return f"__shio.tool_call(__shio_tools__, {name!r})("
        return CALL.sub(repl, text) if "call" in text else text

    def codes_decl(self, i, j):
        """codes:  /  INCOME_NOT_VERIFIED: "We could not verify your income."  -> __shio_codes__ = {...}"""
        line = i + 1
        if self.codes is not None:
            self.error(line, "a program has one codes: table")
        body, k = self.block(j, 0)
        codes = {}
        for n, raw in enumerate(body, j + 1):
            s = _strip_comment(raw.strip())[0]
            if not s:
                continue
            m = CODE_ROW.match(s) or self.error(n, 'expected a reason code: CODE: "the text a customer is given"')
            try:
                text = ast.literal_eval(m["text"])
            except (ValueError, SyntaxError):
                text = None
            if not isinstance(text, str):
                # a plain string, never an f-string: the text is the same for everyone, so it holds no personal data
                self.error(n, f"the text for {m['code']} must be a plain string")
            if m["code"] in codes:
                self.error(n, f"reason code {m['code']} is declared twice")
            codes[m["code"]] = text
        if not codes:
            self.error(line, "codes: needs at least one CODE: \"text\" line")
        self.codes = self.meta["codes"] = codes
        self.emit(0, f"__shio_codes__ = {codes!r}", line)
        return k

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
        body, k = self.block(i + 1, 0, raw=True)
        fields = 0
        pii = []
        classes = {}
        tmeta = {"name": m["name"], "line": line, "fields": []}
        self.meta["types"].append(tmeta)
        for n, raw in enumerate(body, i + 2):
            s = raw.strip()
            if not s or s.startswith("#"):
                continue
            if s[0] in "'\"" and not fields:
                self.emit(1, s, n)       # docstring: the type's description
                continue
            code, desc = _strip_comment(s)
            f = FIELD.match(code) or self.error(n, "expected a field: name: type [as Class | pii] [= default]  # description")
            ftype = f["type"].strip()
            cm = re.search(r"\s+as\s+([A-Z][A-Za-z0-9_]*)$", ftype)
            if cm:                               # what the data is; a policy in force says how it is stored
                classes[f["name"]] = cm.group(1)
                ftype = ftype[:cm.start()].strip()
            elif re.search(r"\spii$", ftype):     # personal data of no particular class: hashed, kept in the vault
                ftype = ftype[:-3].strip()
                pii.append(f["name"])
            elif self.regulated and PERSONAL.search(f["name"]):
                self.error(n, f"regulated: '{f['name']}' looks like personal data; say what it is with `as Class` "
                              "(e.g. `as Email`, `as PhoneNumber`) so a data-handling policy can govern it, or "
                              "`as Public` if it is not personal")
            t = self.type_expr(ftype, n)
            tmeta["fields"].append({"name": f["name"], "type": ftype, "pii": f["name"] in pii or f["name"] in classes,
                                    "class": classes.get(f["name"], "PII" if f["name"] in pii else None),
                                    "description": desc})
            args = ([f["default"]] if f["default"] else []) + ([f"description={desc!r}"] if desc else [])
            self.emit(1, f"{f['name']}: {t}" + (f" = _ShioField({', '.join(args)})" if args else ""), n)
            fields += 1
        if not fields:
            self.error(line, f"type {m['name']} has no fields")
        if pii:
            self.emit(1, f"__shio_pii__: ClassVar[frozenset] = frozenset({sorted(pii)!r})", line)
        if classes:
            self.emit(1, f"__shio_classes__: ClassVar[dict] = {dict(sorted(classes.items()))!r}", line)
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
        m = AGENT.match(header) or self.error(line, "expected: [async] agent NAME(PARAMS) [-> TYPE]:")
        name = m["name"]
        self.agents.add(name)
        body, k = self.block(j, 0)
        readings = self.reads_in(body, start=j)
        deco = self.emit(0, f"@__shio.agent(name={name!r})", line)
        start = self.emit(0, f"{'async def' if m['async'] else 'def'} {name}({m['params']}):", line)
        if readings:
            self.emit(1, " = ".join(sorted(readings)) + " = None", line)
        ameta = {"name": name, "line": line, "params": " ".join(m["params"].split()), "reads": [], "checks": [],
                 "rules": [], "signoffs": [], "returns": []}
        self.meta["agents"].append(ameta)
        ctx = {"kind": "agent", "readings": readings, "agent": name, "pending": [], "signoffs": [], "meta": ameta,
               "async": bool(m["async"])}
        ameta["async"] = bool(m["async"])
        k = self.body(j, 0, 1, ctx)
        if ctx["signoffs"]:
            self.out[deco][0] = f"@__shio.agent(name={name!r}, signoffs={ctx['signoffs']!r})"
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
            for text, ln, *raw in p["body"]:
                if raw and raw[0]:
                    self.tail.append([text, ln])        # a string's own line: exactly as written
                else:
                    self.emit(1, text, ln, self.tail)
        return k

    def reads_in(self, lines, start=0):
        names = set()
        for raw in lines:
            r = re.match(r"^\s*read\s+([A-Za-z_]\w*)", raw)
            if r and READ.match(_strip_comment(raw.strip())[0]):
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
            text = self.calls(self.code(text), line, in_rule=False)
            word = text.split(None, 1)[0].rstrip(":")
            readings, kind = ctx["readings"], ctx["kind"]
            if word == "guard" and GUARD.match(text):
                if kind != "agent":
                    self.error(line, "'guard' can only be used inside an agent")
                g = GUARD.match(text)
                for part in (g["req"], g["model"], g["approver"]):
                    if part:
                        self.py(part, line, "eval")
                        self.scan(part, line, readings, "a guard statement")
                self.emit(level, f"__shio.guard_task(__shio_tools__, {g['req']}, {g['model']}, {g['approver'] or 'None'})",
                          line)
                i = j
                continue
            each = EACH.match(text) if kind == "agent" else None
            if each and each["root"] in readings:
                # iterating over what the model returned: each item is still a reading, for checks and rules only
                if each["var"] == each["root"] or each["idx"] in readings:
                    self.error(line, "the loop variable must be a new name")
                items = f"__shio.each({each['root']}, {each['path'].lstrip('.')!r})"
                head = f"for {each['idx']}, {each['var']} in enumerate({items}):" if each["idx"] else \
                    f"for {each['var']} in {items}:"
                readings.add(each["var"])
                self.emit(level, head, line)
                inner = sorted(self.reads_in(self.block(j, ind)[0]))
                if inner:
                    self.emit(level + 1, " = ".join(inner) + " = None", line)
                i = self.body(j, ind, level + 1, ctx)
                continue
            if word == "signoff":
                if kind != "agent":
                    self.error(line, "'signoff' can only be used inside an agent")
                if level != 1:
                    self.error(line, "'signoff' belongs at the top of an agent's body, not inside a block")
                self.stmt_signoff(line, text, level, ctx)
                i = j
                continue
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
            if word == "return" and kind == "agent":
                ctx["meta"]["returns"].append({"expr": text[6:].strip(), "line": line})
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
                text = self.src[i].strip() if "\n" not in text else text
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
                self.uses.append((_free_names(expr, "eval"), line, f"the prompt's {{{expr}}}"))
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
        lines, k = self.block(j, ind, raw=True)
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
        call = "await __shio.aread" if ctx.get("async") else "__shio.read"     # an async agent doesn't block on the model
        self.emit(level, f"{m['name']} = {call}({', '.join(args)})", line)
        ctx["meta"]["reads"].append({"name": m["name"], "type": m["type"].strip(), "sources": m["src"],
                                     "model": m["model"].strip(), "blind": bool(m["blind"]), "seeing": seeing,
                                     "key": m["key"], "line": line,
                                     "prompt": textwrap.dedent("\n".join(lines)).strip()})
        return k

    def _pending(self, ctx, kind, name, key, line, free, body, level, reward=True, after=(), code=None):
        n = len(ctx["pending"]) + 1
        fn = f"__shio_{kind}_{ctx['agent']}_{name}_{n}"
        ph = f"__SHIO_PENDING_{n}__"
        slot = self.emit(level, f"{name} = {ph}", line)
        deco = f"@__shio.{kind}(name={name!r}" + ("" if reward else ", reward=False") + \
            (f", after={after!r}" if after else "") + (f", code={code!r}" if code else "") + ")"
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
        self.pure(body, line, "a check" if text.startswith("check") else "a when")
        if detail:
            self.pure(detail, line, "a check's message")
        if m["key"]:
            self.py(m["key"], line, "eval")
            self.scan(m["key"], line, ctx["readings"], "a check's key")
        free = _free_names(body, "eval") + (_free_names(detail, "eval") if detail else [])
        self.uses.append((free, line, f"check {m['name']}"))
        after = tuple(a.strip() for a in (m["after"] or "").split(",") if a.strip())
        for a in after:
            if a not in ctx.setdefault("checks", set()):
                self.error(line, f"'after {a}': there is no check or when named {a} before this line")
        ctx.setdefault("checks", set()).add(m["name"])
        is_check = text.startswith("check")
        if m["code"]:
            if not is_check:
                self.error(line, f"a when only steers control flow, so it has no reason code; "
                                 f"make it a check to give it the code {m['code']}")
            if not re.fullmatch(r"[A-Z][A-Z0-9_]*", m["code"]):
                self.error(line, f"reason code {m['code']} must be in capitals, like INCOME_NOT_VERIFIED")
            self.code_uses.append((m["code"], line))
        ctx["meta"]["checks"].append({"name": m["name"], "kind": "check" if is_check else "when", "code": m["code"],
                                      "key": m["key"], "after": list(after), "expr": " ".join(body.split()),
                                      "detail": detail, "line": line, "nested": level > 1})
        ret = f"return bool({body})" + (f", (lambda: {detail})" if detail else "")
        self._pending(ctx, "check", m["name"], m["key"], line, list(dict.fromkeys(free)), [(ret, line)], level,
                      reward=is_check, after=after, code=m["code"])
        return j

    def stmt_signoff(self, line, text, level, ctx):
        """An outcome that needs a person. The condition is over the outcome the rules decided (its fields by name)
        and the program's constants, never over a reading: the model cannot talk its way past a sign-off."""
        m = SIGNOFF.match(text) or self.error(
            line, 'expected: signoff OUTCOME [when CONDITION] by role "ROLE" [and "ROLE"] [maker FIELD, FIELD]')
        roles = re.findall(r"(['\"])([^'\"]+)\1", m["roles"])
        roles = list(dict.fromkeys(r for _, r in roles))
        makers = [x.strip() for x in (m["makers"] or "").split(",") if x.strip()]
        cond = " ".join(m["cond"].split()) if m["cond"] else None
        fn = None
        if cond:
            self.py(cond, line, "eval")
            free = [x for x in _free_names(cond, "eval") if x not in dir(builtins)]
            bad = [x for x in free if x in ctx["readings"]]
            if bad:
                self.error(line, f"'{bad[0]}' is a model reading; a sign-off condition is over the decided outcome "
                                 "(its fields by name) and the program's constants")
            fn = f"__shio_signoff_{ctx['agent']}_{len(ctx['signoffs']) + 1}"
            self.emit(0, "", line, self.tail)
            self.emit(0, f"def {fn}({', '.join(free)}):", line, self.tail)
            self.emit(1, f"return bool({cond})", line, self.tail)
        ctx["signoffs"].append({"label": m["label"], "when": cond, "fn": fn, "roles": roles, "makers": makers,
                                "line": line})
        ctx["meta"]["signoffs"].append(dict(ctx["signoffs"][-1]))
        self.emit(level, "pass  # signoff", line)

    def stmt_rule(self, i, j, text, ind, level, ctx):
        line = i + 1
        m = RULE.match(text)
        lines, k = self.block(j, ind)
        lines = [self.calls(x, j + 1 + n, in_rule=True) for n, x in enumerate(lines)]
        if not any(s.strip() for s in lines):
            self.error(line, "a rule needs a body that returns the outcome")
        base = next(self.indent(x) for x in lines if x.strip())
        own = [x[base:] if not x[:base].strip() else x for x in lines]   # a string's column-0 lines stay as they are
        code = "\n".join(own)
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
        body = [(x if x.strip() else "", n, bool(x[:base].strip())) for n, x in zip(range(j + 1, j + 1 + len(lines)), lines)]
        body = [(own[k] if not raw else lines[k], n, raw) if lines[k].strip() else ("", n, False)
                for k, (_, n, raw) in enumerate(body)]
        self._pending(ctx, "rule", m["name"], m["key"], line, free, body, level)
        ctx["meta"]["rules"].append({"name": m["name"], "key": m["key"], "body": code, "line": line, "body_line": j + 1,
                                     "nested": level > 1})
        return k


def compile_source(source, path="<shio>"):
    """-> (python source text, list mapping each python line to its .shio line)."""
    py, lines, _ = _compile(source, path)
    return py, lines


def program_map(source, path="<shio>"):
    """What `shio explain` documents: the program's types, models, agents, reads, checks, rules and sign-offs.
    The program must compile, so the map never describes a program that cannot run."""
    _, _, c = _compile(source, path)
    return c.meta


def _compile(source, path):
    c = Compiler(source, path)
    rows = c.compile()
    py = PRELUDE + "\n".join(t for t, _ in rows) + "\n"
    lines = [0] * PRELUDE.count("\n") + [ln for _, ln in rows]
    try:
        bound = _bound_names(ast.parse(py))
    except SyntaxError:
        bound = None             # reported with its .shio line by to_code
    if bound is not None:
        known = bound | set(dir(builtins)) | {"argv", "__file__", "__name__", "checks"}
        for names, line, where in c.uses:
            for n in names:
                if n not in known:
                    hint = " (write {{ and }} for literal braces)" if where.startswith("the prompt") else ""
                    c.error(line, f"'{n}' is not defined, in {where}{hint}")
    if bound is not None and not c.regulated:     # regulated programs get the stricter scan below
        _reflection_scan(c, py, lines)
    if c.regulated:
        _regulated_scan(c, py, lines)
    if bound is not None:
        _check_helpers(c, py, lines)
    return py, lines, c


# A check only decides. pure() looks at the check's own text; this looks at what it uses that the same .shio file
# defines: a function (def or lambda), a class's methods, or a name bound to an expression. Each must not change
# anything outside itself. Python modules a program imports are trusted code and not looked into.
FRESH_CALLS = {"list", "dict", "set", "sorted", "tuple", "frozenset", "str", "bytearray"}
MUTATING_FUNCS = {"setattr", "delattr", "heappush", "heappushpop", "heapreplace", "heapify", "insort", "insort_left",
                  "insort_right", "shuffle", "methodcaller", "attrgetter"}


def _impure_node(n):
    """Why a node in a check (or a helper it uses) changes something, ignoring receivers; None if it does not."""
    if isinstance(n, ast.Call):
        f = n.func
        name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
        if name in MUTATING_FUNCS:
            return f"{name}() changes an object"
        if name == "getattr" and len(n.args) > 1 and isinstance(n.args[1], ast.Constant) and \
                (str(n.args[1].value) in MUTATING_METHODS or str(n.args[1].value).startswith("__")):
            return f"getattr(..., {n.args[1].value!r}) takes a method that changes something"
    elif isinstance(n, ast.Name) and n.id in MUTATING_FUNCS:
        return f"{n.id} changes an object"
    return None


def _root(n):
    while isinstance(n, (ast.Attribute, ast.Subscript, ast.Starred)):
        n = n.value
    return n.id if isinstance(n, ast.Name) else None


def _targets(t):
    if isinstance(t, (ast.Tuple, ast.List)):
        for x in t.elts:
            yield from _targets(x)
    elif isinstance(t, ast.Starred):
        yield from _targets(t.value)
    else:
        yield t


def _new_container(v):
    return isinstance(v, (ast.List, ast.Dict, ast.Set, ast.ListComp, ast.DictComp, ast.SetComp)) or (
        isinstance(v, ast.Call) and isinstance(v.func, ast.Name) and v.func.id in FRESH_CALLS)


def _fresh_locals(fn):
    """Names a function only ever binds to a new container it built itself: changing those changes nothing outside."""
    if isinstance(fn, ast.Lambda):
        return set()
    params = {a.arg for a in ast.walk(fn.args) if isinstance(a, ast.arg)}
    seen, bad = set(), set(params)
    for n in ast.walk(fn):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                pairs = [(t, n.value)]
                if isinstance(t, (ast.Tuple, ast.List)) and isinstance(n.value, (ast.Tuple, ast.List)) and \
                        len(t.elts) == len(n.value.elts):          # vals, n = {}, 10
                    pairs = list(zip(t.elts, n.value.elts))
                for target, value in pairs:
                    if isinstance(target, ast.Name):
                        (seen if _new_container(value) else bad).add(target.id)
                    else:
                        bad |= {x.id for x in _targets(target) if isinstance(x, ast.Name)}
        elif isinstance(n, (ast.For, ast.AsyncFor, ast.comprehension)):
            bad |= {x.id for x in ast.walk(n.target) if isinstance(x, ast.Name)}
        elif isinstance(n, (ast.With, ast.AsyncWith)):
            bad |= {x.id for i in n.items if i.optional_vars for x in ast.walk(i.optional_vars) if isinstance(x, ast.Name)}
        elif isinstance(n, ast.NamedExpr):
            bad.add(n.target.id)
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            bad.add(n.target.id)
    return seen - bad


def _check_helpers(c, py, lines):
    tree = ast.parse(py)
    defs, aliases = {}, {}
    for node in ast.walk(tree):
        ln = lines[node.lineno - 1] if getattr(node, "lineno", 0) and node.lineno <= len(lines) else 0
        if not ln:               # the prelude the compiler adds
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("__shio"):
            defs.setdefault(node.name, []).append(node)
        elif isinstance(node, ast.ClassDef):
            defs.setdefault(node.name, []).extend(
                x for x in node.body if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef)))
        elif isinstance(node, ast.Assign):
            v = node.value
            # a name bound to a function value: a lambda, a method taken off an object, or functools.partial(...)
            called = isinstance(v, ast.Attribute) or (isinstance(v, ast.Call) and (
                getattr(v.func, "id", None) == "partial" or getattr(v.func, "attr", None) == "partial"))
            for t in node.targets:
                if isinstance(t, ast.Name) and not t.id.startswith("__shio"):
                    if isinstance(v, ast.Lambda):
                        defs.setdefault(t.id, []).append(v)
                    elif called:
                        aliases.setdefault(t.id, []).append(v)

    def impure(name, seen):
        """Why something this file binds to `name` changes what is outside it, or None."""
        if name in seen:
            return None
        seen.add(name)
        for fn in defs.get(name, []):
            why = impure_fn(fn, seen)
            if why:
                return why
        for value in aliases.get(name, []) if name not in defs else []:
            why = impure_expr(value, set(), seen)
            if why:
                return f"is bound to {ast.unparse(value)[:60]}, which {why}"
        return None

    def impure_expr(node, fresh, seen):
        for n in ast.walk(node):
            why = _impure_node(n)
            if why:
                return why.replace(" changes", " that changes", 1) if why.startswith("getattr") else why
            if isinstance(n, (ast.Global, ast.Nonlocal)):
                return f"declares {'global' if isinstance(n, ast.Global) else 'nonlocal'} {', '.join(n.names)}"
            if isinstance(n, ast.Attribute) and (n.attr in MUTATING_METHODS or n.attr.startswith("__")) \
                    and _root(n.value) not in fresh:
                return f"uses {ast.unparse(n)[:60]} (it changes {ast.unparse(n.value)[:40]})"
            for t in (n.targets if isinstance(n, (ast.Assign, ast.Delete)) else
                      [n.target] if isinstance(n, (ast.AugAssign, ast.AnnAssign)) else []):
                for x in _targets(t):
                    if isinstance(x, (ast.Attribute, ast.Subscript)) and _root(x) not in fresh:
                        return f"changes {ast.unparse(x)[:60]}"
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and (n.id in defs or n.id in aliases):
                why = impure(n.id, seen)
                if why:
                    return f"uses {n.id}, which {why}"
        return None

    def impure_fn(fn, seen):
        return impure_expr(fn, _fresh_locals(fn), seen)

    for expr, line, where in c.pure_uses:
        for name in dict.fromkeys(n.id for n in ast.walk(expr) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)):
            if name in defs or name in aliases:
                why = impure(name, set())
                if why:
                    c.error(line, f"{where} only decides, so it cannot change anything: {name}, defined in this "
                                  f"file, {why}. Move it to a rule, after the check (or into a Python module you "
                                  f"import, which Shio treats as trusted code and does not look into).")


# Every program: the routes that reach a reading without naming it, so the name check above never sees them (found by
# an escape test: each of these returned a reading's value from plain code). Regulated programs ban more, below.
REFLECTION_NAMES = {"locals", "vars", "globals", "eval", "exec", "compile", "__import__", "breakpoint", "__builtins__",
                    "attrgetter", "methodcaller"}
REFLECTION_ATTRS = {"_getframe", "f_locals", "f_globals", "f_back", "gi_frame", "cr_frame", "ag_frame", "tb_frame",
                    "currentframe", "stack", "trace", "get_objects", "get_referrers", "get_referents", "__dict__",
                    "__closure__", "__globals__", "__code__", "__self__", "_sealed", "_SEALED", "_gate",
                    "__getattribute__", "__builtins__", "attrgetter", "methodcaller", "last_run"}
REFLECTION_MODULES = {"gc", "inspect", "ctypes", "builtins"}
_PRIVATE = None


def shio_private_names():
    """Every private (_x, not dunder) name the shio package's modules define: the runtime's internals (_open, _run,
    _current, _config...). Shio code may not name them as an attribute or import them, in any mode."""
    global _PRIVATE
    if _PRIVATE is None:
        import importlib
        import pkgutil
        import shio
        names = set()
        for m in pkgutil.iter_modules(shio.__path__):
            try:
                mod = importlib.import_module(f"shio.{m.name}")
            except Exception:
                continue
            names |= {n for n in vars(mod) if n.startswith("_") and not n.startswith("__")}
        names |= {n for n in vars(shio) if n.startswith("_") and not n.startswith("__")}
        _PRIVATE = frozenset(names)
    return _PRIVATE


def _reflection_scan(c, py, lines):
    tree = ast.parse(py)
    for node in ast.walk(tree):
        ln = lines[node.lineno - 1] if getattr(node, "lineno", 0) and node.lineno <= len(lines) else 0
        if not ln:               # the prelude the compiler adds
            continue
        why = None
        private = shio_private_names()
        if isinstance(node, ast.Name) and node.id in REFLECTION_NAMES:
            why = f"'{node.id}' can reach a model reading without naming it, so it is not allowed in Shio code"
        elif isinstance(node, ast.Name) and node.id == "getattr" and not isinstance(node.ctx, ast.Load):
            why = "'getattr' is Shio's checked getattr in Shio code, so it can't be rebound or deleted"
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == "getattr":
            why = "'getattr' is Shio's checked getattr in Shio code, so it can't be redefined"
        elif isinstance(node, ast.Attribute) and node.attr in REFLECTION_ATTRS:
            why = f"'.{node.attr}' can reach a model reading without naming it, so it is not allowed in Shio code"
        elif isinstance(node, ast.Attribute) and node.attr in private:
            why = f"'.{node.attr}' is one of the Shio runtime's internals, which Shio code can't reach"
        elif isinstance(node, ast.Call) and getattr(node.func, "id", None) == "getattr" and len(node.args) > 1 \
                and isinstance(node.args[1], ast.Constant) and str(node.args[1].value).startswith("_"):
            why = f"getattr(..., {node.args[1].value!r}): Shio code can't fetch private or dunder attributes by name"
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            bad = [m for m in mods if m.split(".")[0] in REFLECTION_MODULES]
            if bad:
                why = f"importing {bad[0]} is not allowed: it can reach a model reading without naming it"
            elif isinstance(node, ast.ImportFrom):
                names = [a.name for a in node.names]
                bad = [n for n in names if n in REFLECTION_NAMES or n in REFLECTION_ATTRS or n == "getattr"
                       or ((node.module or "").split(".")[0] == "shio" and n.startswith("_"))]
                if bad:
                    why = f"importing {bad[0]} from {node.module} is not allowed: it can reach a model reading " \
                          f"without naming it"
            if not why and any((a.asname or "") == "getattr" for a in node.names):
                why = "'getattr' is Shio's checked getattr in Shio code, so it can't be rebound"
        if why:
            c.error(ln, why + " (put such code in a Python module you import, which Shio treats as trusted code)")


# What a regulated program may not do: every way to reach a reading's value, or the runtime's own state, without
# going through a check or a rule. Python is not a sandbox; this makes each such route a compile error in Shio code,
# and the hashes of imported local modules (trusted code) are recorded with every decision.
REGULATED_NAMES = {"locals", "vars", "globals", "eval", "exec", "compile", "getattr", "setattr", "delattr",
                   "__import__", "breakpoint", "memoryview"}
REGULATED_MODULES = {"shio", "importlib", "inspect", "ctypes", "gc", "sys", "operator", "pickle", "marshal",
                     "copyreg", "dill", "cloudpickle", "types", "builtins"}
# field names that hold personal data in most systems: a regulated program must classify them
PERSONAL = re.compile(r"(?i)(^|_)(e_?mail|phone|mobile|msisdn|pan|aadhaar|aadhar|ssn|passport|dob|birth|address|"
                      r"iban|account_?(no|number|holder)|holder|full_?name|customer_?name|applicant_?name)($|_)")
COMPILER_NAMES = {"_ShioType", "_ShioField", "__name__", "__file__"}


IDENT = re.compile(r"[^\W\d]\w*")


def _regulated_scan(c, py, lines):
    # names the compiler emits (__shio.read, __shio_tools__...) are exempt from the dunder rule below, so the program's
    # own text may not use them: read from the .shio source, as Python sees an identifier (NFKC-normalised)
    for k, text in enumerate(c.src):
        for m in IDENT.finditer(text):
            if unicodedata.normalize("NFKC", m.group(0)).startswith("__shio"):
                c.error(k + 1, f"regulated: '{m.group(0)}' is the compiler's own name; Shio code can't use it")
    tree = ast.parse(py)
    for node in ast.walk(tree):
        ln = lines[node.lineno - 1] if getattr(node, "lineno", 0) and node.lineno <= len(lines) else 0
        if not ln:               # the prelude the compiler adds
            continue
        why = None
        if isinstance(node, ast.Name):
            n = node.id
            if n in REGULATED_NAMES:
                why = f"'{n}' can reach a reading's value or the runtime without a check or rule"
            elif n.startswith("__") and not n.startswith("__shio") and n not in COMPILER_NAMES:
                why = f"'{n}' (a dunder name) is not allowed"
        elif isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            why = f"'.{node.attr}': private and dunder attributes are not allowed"
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "in_force" and not (
                len(node.args) == 1 and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
            why = "in_force() takes the table's name as a literal string, so checks and documentation can see it"
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            bad = [m for m in mods if m.split(".")[0] in REGULATED_MODULES]
            if bad:
                why = f"importing {bad[0]} is not allowed (it can reach the runtime's internals)"
        if why:
            c.error(ln, f"regulated: {why}")


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
