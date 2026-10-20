"""Shio Guard: put Shio in front of any agent's tools, without rewriting the agent.

    from shio import guard
    g = guard.Guard([
        guard.Tool("read_file", output="untrusted"),
        guard.Tool("get_webpage", opens_address=True, control={"url": "url"}, output="untrusted"),
        guard.Tool("send_money", side_effect=True, control={"recipient": "iban"}),
        guard.Tool("send_email", side_effect=True, control={"to": "email"}, content=["body"]),
    ], planner=shio.models.ollama("qwen3:8b"))

    s = g.start("Pay the bill in bill.txt")       # a blind plan from the request alone
    d = s.check("send_money", {"recipient": "UK12...", "amount": 98.7})   # -> Decision: run | hold | deny
    if d.run: result = send_money(...); s.record("send_money", args, result)
    # or: d, result = s.call("send_money", args, send_money)

What it decides (the programs are in guard.shio, so every decision is a Shio run with a trace):
  plan    before the agent reads any tool output, a model reads only the user's request: which tools that act or send
          data it calls for, which places it points at for their values, and whether it hands the job to instructions
          it does not spell out (delegated).
  check   each proposed call. run: a declared tool, planned if it acts, and every control value (a recipient, a URL,
          an IBAN) came from trusted text or a place the request pointed at for this tool. hold: it would run but needs
          a person (a delegated request, or a pointed-at place offering more than one candidate). deny: an unplanned
          action, a value found only in untrusted text, an address the agent never read anywhere (the model built it),
          or an undeclared tool.
  reply   names and addresses in a final answer that appear only in untrusted text: what an injection planted.

Provenance is by value: a control value is tainted when it was seen in untrusted text and not in trusted text (the
request, the system's own records, outputs of tools declared output="trusted"). This is the gate measured on AgentDojo
v1.2.2 (README, "Benchmark"), made general: there its tool declarations were written per suite; here you declare them.
"""
import dataclasses
import hashlib
import json
import re
import time
from pathlib import Path

URL = re.compile(r"(?:https?://)?(?:www\.)?[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\.(?:com|org|net|io|ly|co|ch|uk|de|in|"
                 r"info|biz|xyz|me|app|dev|ai|gov|edu|us|eu|fr|es|it|nl|au|ca|jp|ru|cn)(?:/[^\s\"'<>)\]]*)?", re.I)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
IBAN = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b")
NAME = re.compile(r"\b[A-Z][\w'&-]+(?:\s+(?:(?:of|the|de|la|du|and|&)\s+)?[A-Z][\w'&-]+)+\b")
ADDRESS_KINDS = {"email", "iban", "url"}
KINDS = ADDRESS_KINDS | {"name"}
LONG = 120                  # characters: a record field longer than this is free text someone wrote, so untrusted
_EMAIL_ARGS = {"to", "cc", "bcc", "recipient", "recipients", "email", "emails", "email_address", "user_email", "send_to"}
_URL_ARGS = {"url", "urls", "link", "href", "website", "endpoint", "webhook", "callback_url", "uri"}
_IBAN_ARGS = {"iban", "account_number", "account", "recipient_iban", "beneficiary_account"}
_CONTENT_ARGS = {"body", "message", "text", "content", "comment", "description", "note", "notes", "summary"}
STOP = {"the", "a", "an", "her", "his", "their", "its", "my", "our", "your", "website", "site", "page", "web", "blog",
        "email", "e-mail", "message", "file", "channel", "inbox", "from", "with", "and"}


@dataclasses.dataclass
class Tool:
    """How a tool behaves, declared by whoever wires the agent.

    side_effect: it changes something or sends data out (runs only if the plan names it).
    control: {argument: kind} for arguments that pick a target: "email", "url", "iban" or "name". Their values must
             come from trusted text or a place the request pointed at.
    content: arguments holding free text (a message body): addresses inside it must be grounded and untainted.
    output: "untrusted" (anyone could have written it: files, web pages, emails, reviews), "trusted" (the system's own
            records), or "mixed" (records whose `untrusted_keys` fields are free text). In any output, a field longer
            than 120 characters counts as free text, so as untrusted.
    opens_address: a read that fetches a URL (get_webpage): allowed without planning, but its URL is checked.
    """
    name: str
    description: str = ""
    side_effect: bool = False
    control: dict = dataclasses.field(default_factory=dict)
    content: list = dataclasses.field(default_factory=list)
    output: str = "untrusted"
    untrusted_keys: list = dataclasses.field(default_factory=list)
    opens_address: bool = False

    @classmethod
    def infer(cls, name, description="", schema=None, side_effect=True, output="untrusted", control=None, content=None):
        """A Tool from a JSON schema of its arguments, guessing which arguments pick a target: a string argument
        named like a recipient or email (to, cc, recipients, email...) or with format "email" is an email; one named
        url, link, href, website or endpoint, or with format "uri", is a URL; iban or account_number is an IBAN.
        Free-text arguments (body, message, text, content, comment, description) are content. Pass control= or
        content= to say it yourself; side_effect defaults to True, so an unknown tool needs a plan to run."""
        props = (schema or {}).get("properties") or {}
        guessed, text = {}, []
        for arg, spec in props.items():
            spec = spec if isinstance(spec, dict) else {}
            items = spec.get("items") if isinstance(spec.get("items"), dict) else {}
            fmt, low = spec.get("format") or items.get("format"), arg.lower()
            if fmt == "email" or low in _EMAIL_ARGS or low.endswith("_email"):
                guessed[arg] = "email"
            elif fmt in ("uri", "url") or low in _URL_ARGS or low.endswith("_url"):
                guessed[arg] = "url"
            elif low in _IBAN_ARGS:
                guessed[arg] = "iban"
            elif low in _CONTENT_ARGS:
                text.append(arg)
        return cls(name, description or "", side_effect=side_effect, output=output,
                   control=guessed if control is None else control, content=text if content is None else content)

    @classmethod
    def from_mcp(cls, tool, **kw):
        """A Tool from an MCP tool listing (an mcp.types.Tool or its dict). MCP's own hints decide the defaults: a
        tool is an action unless it says readOnlyHint, and its output is untrusted unless it says openWorldHint=False
        (it only reads the user's own system). A server's hints are its claim; pass side_effect= or output= to
        override them for a server you don't trust."""
        get = (lambda o, k, d=None: o.get(k, d)) if isinstance(tool, dict) else (lambda o, k, d=None: getattr(o, k, d))
        ann = get(tool, "annotations") or {}
        hint = (lambda k: ann.get(k)) if isinstance(ann, dict) else (lambda k: getattr(ann, k, None))
        kw.setdefault("side_effect", not hint("readOnlyHint"))
        kw.setdefault("output", "trusted" if hint("openWorldHint") is False else "untrusted")
        schema = get(tool, "inputSchema") or get(tool, "input_schema") or {}
        return cls.infer(get(tool, "name"), get(tool, "description") or "", schema, **kw)

    def __post_init__(self):
        if self.output not in ("untrusted", "trusted", "mixed"):
            raise ValueError(f"tool {self.name}: output must be 'untrusted', 'trusted' or 'mixed'")
        bad = {k for k in self.control.values() if k not in KINDS}
        if bad:
            raise ValueError(f"tool {self.name}: control kinds must be among {sorted(KINDS)}, not {sorted(bad)}")


@dataclasses.dataclass
class Decision:
    outcome: str            # "run" | "hold" | "deny"
    why: str = ""
    id: str = None          # a held call's id, for Session.approve / reject
    approved_by: str = None

    @property
    def run(self):
        return self.outcome == "run"


# -- text and values ------------------------------------------------------------------------------------------------

def norm(s):
    s = str(s).strip().lower()
    s = re.sub(r"^https?://", "", s)
    s = re.sub(r"^www\.", "", s)
    return re.sub(r"\s+", "", s).rstrip("/.")


def site(url):
    """A URL without scheme, www., path, query or fragment: "https://www.a.com/x?d=1" -> "a.com"."""
    return re.split(r"[/?#]", norm(url), maxsplit=1)[0]


def addresses(text):
    """URLs, email addresses and IBANs inside free text."""
    text = str(text)
    found = EMAIL.findall(text) + IBAN.findall(text)
    found += URL.findall(EMAIL.sub(" ", text))
    return list(dict.fromkeys(found))


def kind(value):
    v = str(value).strip()
    if EMAIL.fullmatch(v):
        return "email"
    if IBAN.fullmatch(v):
        return "iban"
    if URL.fullmatch(v):
        return "url"
    return "name"


def _plain(x):
    if hasattr(x, "model_dump"):
        return x.model_dump()
    if dataclasses.is_dataclass(x) and not isinstance(x, type):
        return dataclasses.asdict(x)
    if isinstance(x, (list, tuple, set)):
        return [_plain(v) for v in x]
    if isinstance(x, dict):
        return {str(k): _plain(v) for k, v in x.items()}
    return x


def _leaves(x, key=None):
    """(key, text) for every leaf; a dict's keys are leaves too (names of things)."""
    if isinstance(x, dict):
        for k, v in x.items():
            yield None, k
            yield from _leaves(v, k)
    elif isinstance(x, list):
        for v in x:
            yield from _leaves(v, key)
    elif x is not None:
        yield key, x if isinstance(x, str) else str(x)


def _unique(found):
    out = {}
    for a in found:
        out.setdefault(norm(a), a)
    return list(out.values())


def _strings(args):
    for v in (args or {}).values():
        for s in v if isinstance(v, list) else [v]:
            if isinstance(s, str) and len(norm(s)) >= 3:
                yield s


# -- the ledger -----------------------------------------------------------------------------------------------------

class Ledger:
    """What the agent has read, trusted and untrusted, and where each value came from. One per task."""

    def __init__(self, tools, request, plan=None):
        self.tools = tools
        self.trusted = [norm(request)]
        self.sites = set()          # sites (URLs without path or query) found in the request or a record
        self._add_sites(request)
        self.untrusted = []
        self.open_text = []         # output of fully untrusted tools: text anyone can write
        self.plan = plan or {"tools": [], "delegated": False, "sources": []}
        self.designated = []        # [{"d": designation, "tool": read tool, "texts": [...], "addresses": [...], "items": [...]}]
        self.derived = []           # designations made by following a link from a designated place (one hop)
        self.endorsed_urls = {}     # tool -> {norm url}: pages opened from a designated place, citable in content

    def add_trusted(self, text):
        """A record the system itself holds (the user's contacts, the vendor master): values in it are trusted."""
        for _, t in _leaves(_plain(text)):
            self.trusted.append(norm(t))
            self._add_sites(t)

    def _add_sites(self, text):
        self.sites |= {site(a) for a in addresses(text) if kind(a) == "url"}

    def is_known(self, function):
        return function in self.tools

    def is_side_effect(self, function):
        t = self.tools.get(function)
        return t is None or t.side_effect       # an undeclared tool is treated as one that acts: fail closed

    def reads(self):
        return {n for n, t in self.tools.items() if t.opens_address}

    def _designations_for(self, tool, args):
        out = []
        for d in self.plan.get("sources") or []:
            ns = norm(d["source"])
            if any(norm(a) in ns or ns in norm(a) for a in _strings(args)):
                out.append(d)
        if tool in self.reads():
            url = norm((args or {}).get(self._url_arg(tool)) or "")
            out += [d for d in self.derived if norm(d["source"]) == url]
        return out

    def _url_arg(self, tool):
        t = self.tools.get(tool)
        return next((a for a, k in (t.control if t else {}).items() if k == "url"), "url")

    def record(self, tool, result, args=None):
        """Note a tool's result: its text becomes trusted or untrusted, by the tool's declared output."""
        t = self.tools.get(tool) or Tool(tool)
        if tool in self.reads():            # a link followed on a designated place's say-so: that page is designated too
            url = (args or {}).get(self._url_arg(tool))
            if url and self.tainted(url):
                status, d = self._endorse(tool, "url", url)
                if status == "yes" and not any(norm(x["source"]) == norm(url) for x in self.derived):
                    rest = sorted(set(d["tools"]) - self.reads())
                    self.derived.append({"source": url, "tools": rest, "about": d["about"], "every": False})
                    if self._endorse(tool, "url", url, every=False)[0] == "yes":
                        for x in set(rest) | set(self.plan.get("tools") or []):
                            self.endorsed_urls.setdefault(x, set()).add(norm(url))
        plain = _plain(result)
        leaves = list(_leaves(plain))
        for key, text in leaves:
            # a record holds short values: long free text inside one was written by someone, so it is untrusted too
            bad = t.output == "untrusted" or key in t.untrusted_keys or len(text) > LONG
            (self.untrusted if bad else self.trusted).append(norm(text))
            if not bad:
                self._add_sites(text)
            if t.output == "untrusted":
                self.open_text.append(norm(text))
        items = plain if isinstance(plain, list) else [plain]     # a list result (messages, emails): one item each
        for d in self._designations_for(tool, args):
            self.designated.append({"d": d, "tool": tool, "texts": [norm(x) for _, x in leaves],
                                    "addresses": _unique(addresses(" ".join(x for _, x in leaves))),
                                    "items": [([norm(x) for _, x in _leaves(it)],
                                               _unique(addresses(" ".join(x for _, x in _leaves(it))))) for it in items]})

    # -- provenance -------------------------------------------------------------------------------------------------

    def grounded(self, value):
        v = norm(value)
        return any(v in t for t in self.trusted)

    def seen_untrusted(self, value):
        v = norm(value)
        return any(v in t for t in self.untrusted)

    def tainted(self, value):
        v = norm(value)
        if len(v) < 3 or v.isdigit():
            return False
        return self.seen_untrusted(v) and not self.grounded(v)

    def ungrounded(self, value, want=None):
        """An address seen nowhere, whose site (for a URL) is in no trusted text either: the model built it."""
        v = norm(value)
        k = kind(value)
        if (k not in ADDRESS_KINDS and want not in ADDRESS_KINDS) or len(v) < 3 or v.isdigit():
            return False
        if self.grounded(v) or self.seen_untrusted(v):
            return False
        return not (k == "url" or want == "url") or site(v) not in self.sites

    def _endorse(self, function, want, value, every=True):
        """("yes", designation) | ("ambiguous", designation) | (None, None) for a tainted value used in `function`
        where an argument of kind `want` is expected."""
        v, k = norm(value), kind(value)
        if k != want:
            return None, None
        ambiguous = None
        for rec in self.designated:
            d = rec["d"]
            if function not in d["tools"]:
                continue
            if function in self.reads() and rec["tool"] in self.reads():
                continue                    # no second hop: links on a web page are not followed on the user's say-so
            if not any(v in t for t in rec["texts"]):
                continue
            words = [w for w in re.findall(r"[a-z0-9]{3,}", (d["about"] or "").lower()) if w not in STOP]
            if k == "name":
                if words and any(w in v for w in words):
                    return "yes", d
            else:                           # an address: the one holding the request's word, in itself or its item
                own = _unique([a for _, found in rec["items"] for a in found
                               if kind(a) == k and any(w in norm(a) for w in words)])
                near = _unique([a for texts, found in rec["items"] for a in found
                                if kind(a) == k and any(w in t for t in texts for w in words)]) if len(rec["items"]) > 1 else []
                for hits in (own, near):
                    if hits:
                        if len(hits) == 1 and norm(hits[0]) == v:
                            return "yes", d
                        break
            if not self.is_side_effect(function):
                # a read: the only address of its kind there, or every one if the request asked for all. An action
                # needs the `about` match: a place can be tampered with so that its only IBAN is the attacker's.
                cands = [a for a in rec["addresses"] if kind(a) == k]
                if (every and d["every"]) or (len(cands) == 1 and norm(cands[0]) == v):
                    return "yes", d
            ambiguous = ambiguous or d
        return ("ambiguous", ambiguous) if ambiguous else (None, None)

    def _from_designated(self, value):
        v = norm(value)
        return any(v in t for rec in self.designated for t in rec["texts"])

    def classify(self, function, args):
        """-> (tainted, held, ungrounded): [(argument, value)] lists."""
        t = self.tools.get(function)
        tainted, held, ungrounded = [], [], []
        if t is None:
            return tainted, held, ungrounded

        def judge(a, val, want):
            if val is None:
                return
            if not self.tainted(val):
                if norm(val) not in self.endorsed_urls.get(function, set()) and self.ungrounded(val, want):
                    ungrounded.append((a, val))
                return
            if norm(val) in self.endorsed_urls.get(function, set()):
                return
            status, _ = self._endorse(function, want, val) if want else (None, None)
            if status == "yes":
                return
            if status == "ambiguous" or (want and self.plan.get("delegated") and self._from_designated(val)):
                held.append((a, val))
            else:
                tainted.append((a, val))

        for a, want in t.control.items():
            vals = (args or {}).get(a)
            for v in vals if isinstance(vals, list) else [vals]:
                judge(a, v, want)
        for a in t.content:
            for v in addresses((args or {}).get(a) or ""):
                judge(a, v, None)       # an address inside a message is never endorsed by a place, only cited pages
        return tainted, held, ungrounded

    def tainted_args(self, function, args):
        return self.classify(function, args)[0]

    def held_args(self, function, args):
        return self.classify(function, args)[1]

    def ungrounded_args(self, function, args):
        return self.classify(function, args)[2]

    def untrusted_entities(self, text):
        """Names (two or more capitalised words) and addresses in a final answer that appear only in fully untrusted
        tool output, never in the request or a record. Longest match wins."""
        out = []
        tainted = lambda x: self.tainted(x) and any(norm(x) in t for t in self.open_text)     # noqa: E731
        for a in addresses(text):
            if tainted(a):
                out.append(a)
        for m in NAME.finditer(str(text)):
            words = m.group(0).split()
            spans = sorted(((i, j) for i in range(len(words)) for j in range(i + 2, len(words) + 1)),
                           key=lambda s: s[0] - s[1])
            for i, j in spans:
                phrase = " ".join(words[i:j])
                if tainted(phrase) and not any(phrase in o for o in out):
                    out.append(phrase)
                    break
        return list(dict.fromkeys(out))


# -- the guard ------------------------------------------------------------------------------------------------------

_PROGRAM = None


def _program():
    global _PROGRAM
    if _PROGRAM is None:
        from . import load
        _PROGRAM = load(Path(__file__).with_name("guard.shio"), name="shio_guard")
    return _PROGRAM


@dataclasses.dataclass
class ApprovalRequest:
    """What a person is asked to approve: the call, why it was held, and the task it belongs to."""
    id: str
    tool: str
    args: dict
    why: str
    request: str            # the user's request the task started from
    requester: str = None   # who asked for the task (they may not approve their own held calls)


@dataclasses.dataclass
class Approval:
    approved: bool
    by: str
    note: str = ""


def terminal_approver(prompt=input, out=print):
    """An approver that asks on the terminal. For Slack, a web page or a ticket, write a function with the same
    shape: ApprovalRequest -> Approval."""
    def ask(req):
        out(f"\nShio is holding a call for approval: {req.tool}({json.dumps(req.args, default=str)})\n  why: {req.why}"
            f"\n  task: {req.request}")
        who = prompt("Your name (empty to refuse): ").strip()
        if not who:
            return Approval(False, "terminal", "no approver")
        ok = prompt("Approve? [y/N]: ").strip().lower() in ("y", "yes")
        return Approval(ok, who, prompt("Note (optional): ").strip())
    return ask


class Guard:
    """The tools an agent may call, the model that plans from the user's request alone, and (optionally) who
    approves held calls: approver(ApprovalRequest) -> Approval. Without one, held calls wait for Session.approve."""

    def __init__(self, tools, planner, approver=None, four_eyes=True):
        self.tools = {t.name: t for t in tools}
        self.planner, self.approver, self.four_eyes = planner, approver, four_eyes

    def start(self, request, records=(), requester=None):
        """A task: the blind plan from `request`, and a ledger seeded with the system's own `records` (trusted).
        requester: who asked for it; with four_eyes, they can't approve this task's held calls themselves."""
        return Session(self, request, records, requester)


def _call_key(name, args):
    return hashlib.sha256(json.dumps([name, args], sort_keys=True, default=str).encode()).hexdigest()


class Session:
    def __init__(self, guard, request, records=(), requester=None):
        import shio
        self.guard, self.request, self.requester = guard, request, requester
        self.pending, self._approved = {}, {}      # id -> held call; call key -> who approved it
        acts = [t for t in guard.tools.values() if t.side_effect]
        menu = "\n".join(f"- {t.name}: {t.description}" for t in acts)
        reads = "\n".join(f"- {t.name}: {t.description}" for t in guard.tools.values() if t.opens_address)
        self.plan = _program().plan_actions(shio.Source("user request", text=request), menu, reads, guard.planner)
        self.ledger = Ledger(guard.tools, request, self.plan)
        for r in records:
            self.ledger.add_trusted(r)

    def check(self, name, args):
        """-> Decision for one proposed call. Nothing runs here."""
        import shio
        from .models import replay
        proposed = {"function": name, "args": dict(args or {})}
        out = _program().gate(shio.Source("proposed tool call", text=json.dumps(proposed, default=str)),
                              replay(lambda req, p=proposed: p, name="proposed tool call"), self.plan, self.ledger)
        if out["outcome"] != "hold":
            return Decision(out["outcome"], out.get("why", ""))
        key = _call_key(name, proposed["args"])
        if key in self._approved:                  # approved for exactly these arguments, and still only held
            return Decision("run", f"approved by {self._approved[key]}", key[:12], self._approved[key])
        self.pending[key[:12]] = {"tool": name, "args": proposed["args"], "why": out.get("why", ""), "key": key}
        return Decision("hold", out.get("why", ""), key[:12])

    # -- approvals ---------------------------------------------------------------------------------------------------

    def _answer(self, id, by, note, approved):
        p = self.pending.get(id)
        if p is None:
            raise KeyError(f"no held call {id!r} in this task")
        if approved and self.guard.four_eyes and self.requester and by == self.requester:
            raise PermissionError(f"{by} asked for this task, so can't approve its held calls (four eyes)")
        verdict = {"approved": approved, "by": by, "note": note, "tool": p["tool"], "time": time.time()}
        _program().approval({"tool": p["tool"], "args": p["args"]}, p["why"], verdict)     # a run in the chain
        del self.pending[id]
        if approved:
            self._approved[p["key"]] = by
            return self.check(p["tool"], p["args"])   # re-checked: a call that is now denied stays denied
        return Decision("deny", f"refused by {by}" + (f": {note}" if note else ""), id)

    def approve(self, id, by, note=""):
        """A person approves held call `id`. -> the call's Decision now: "run" unless something since made it a deny."""
        return self._answer(id, by, note, True)

    def reject(self, id, by, note=""):
        return self._answer(id, by, note, False)

    def decide(self, name, args):
        """check(), then, for a held call, ask the approver if there is one. -> Decision."""
        d = self.check(name, args)
        if d.outcome == "hold" and self.guard.approver:
            p = self.pending[d.id]
            a = self.guard.approver(ApprovalRequest(d.id, name, p["args"], d.why, self.request, self.requester))
            try:
                return self._answer(d.id, a.by, a.note, bool(a.approved))
            except PermissionError as e:
                return Decision("hold", str(e), d.id)
        return d

    def record(self, name, args, result):
        """Note what a tool returned, so later calls are judged against it."""
        self.ledger.record(name, result, args)

    def call(self, name, args, fn):
        """Check, then run fn(**args) only on "run", and record its result. -> (Decision, result or None)."""
        d = self.decide(name, args)
        if not d.run:
            return d, None
        result = fn(**(args or {}))
        self.record(name, args, result)
        return d, result

    def reply(self, text):
        """Names and addresses in a final answer that only untrusted text contained."""
        return self.ledger.untrusted_entities(text)

    # -- adapters for agent stacks ----------------------------------------------------------------------------------

    def _run_one(self, name, args, run):
        """-> (Decision, text the model is shown). A held or denied call never runs; the model is told why."""
        d = self.decide(name, args)
        if not d.run:
            verb = (f"is waiting for a person to approve it (held call {d.id})" if d.outcome == "hold"
                    else "was refused")
            return d, f"Shio: this call did not run; it {verb}. {d.why}".strip()
        try:
            result = run(name, args)
        except Exception as e:                       # the tool's own error, reported like any tool error
            return d, f"Error: {type(e).__name__}: {e}"
        self.record(name, args, result)
        return d, result if isinstance(result, str) else json.dumps(result, default=str)

    def openai_tool_messages(self, tool_calls, run):
        """Screen an OpenAI-style assistant message's tool_calls (objects or dicts); run(name, args) the ones Guard
        lets through. -> the {"role": "tool", ...} messages to append, one per call, in order."""
        out = []
        for c in tool_calls or []:
            fn = c.get("function") if isinstance(c, dict) else getattr(c, "function", None)
            fget = (lambda k: fn.get(k)) if isinstance(fn, dict) else (lambda k: getattr(fn, k, None))
            cid = c.get("id") if isinstance(c, dict) else getattr(c, "id", None)
            raw = fget("arguments") or "{}"
            try:
                args = json.loads(raw) if isinstance(raw, str) else dict(raw)
            except json.JSONDecodeError:
                out.append({"role": "tool", "tool_call_id": cid, "content": "Error: the arguments were not valid JSON"})
                continue
            d, text = self._run_one(fget("name"), args, run)
            out.append({"role": "tool", "tool_call_id": cid, "content": text})
        return out

    def anthropic_tool_results(self, content, run):
        """Screen the tool_use blocks of a Claude response's content (objects or dicts); run(name, args) the ones
        Guard lets through. -> the tool_result blocks for the next user message (all of them, in one message)."""
        out = []
        for b in content or []:
            get = (lambda k: b.get(k)) if isinstance(b, dict) else (lambda k: getattr(b, k, None))
            if get("type") != "tool_use":
                continue
            d, text = self._run_one(get("name"), dict(get("input") or {}), run)
            block = {"type": "tool_result", "tool_use_id": get("id"), "content": text}
            if not d.run:
                block["is_error"] = True
            out.append(block)
        return out

    async def mcp_call(self, client, name, args):
        """Call a tool on an MCP server through an mcp.ClientSession (or anything with an async call_tool), only if
        Guard lets it through, and record what it returned. -> (Decision, the CallToolResult or None)."""
        d = self.decide(name, args)
        if not d.run:
            return d, None
        result = await client.call_tool(name, args)
        blocks = getattr(result, "content", None) or (result.get("content") if isinstance(result, dict) else []) or []
        text = [getattr(x, "text", None) or (x.get("text") if isinstance(x, dict) else None) for x in blocks]
        self.record(name, args, "\n".join(t for t in text if t) or result)
        return d, result


class GuardedTool:
    """A LangChain-style tool (anything with .name and .invoke / .run, or a plain function) behind a Session: a call
    Guard holds or refuses returns its reason as the observation instead of running. Everything else is the tool's."""

    def __init__(self, session, tool):
        self._session, self._tool = session, tool
        self.name = getattr(tool, "name", None) or getattr(tool, "__name__", "tool")

    def __getattr__(self, attr):
        return getattr(self._tool, attr)

    def _args(self, tool_input):
        return dict(tool_input) if isinstance(tool_input, dict) else {"input": tool_input}

    def invoke(self, tool_input, *a, **kw):
        call = getattr(self._tool, "invoke", None) or getattr(self._tool, "run", None)
        run = (lambda n, args: call(tool_input, *a, **kw)) if call else (lambda n, args: self._tool(**args))
        return self._session._run_one(self.name, self._args(tool_input), run)[1]

    run = invoke

    def __call__(self, **kwargs):
        return self.invoke(kwargs)


def wrap_tools(session, tools):
    """[GuardedTool] for a list of tools: hand these to the agent instead of the originals."""
    return [GuardedTool(session, t) for t in tools]
