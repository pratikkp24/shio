"""Data-handling policies: what a regulation requires for each class of data, as dated versions.

Programs never say how a field is stored; they say what it is (`email: str as Email`). A policy file says how each
class must be handled, from a given date, and where that rule comes from:

    policy "RBI data handling" version 4:
        effective 2027-01-01
        source "RBI circular RBI/2026-27/112, para 5"
        approved_by "Chief Compliance Officer"
        Email:       clear
        PhoneNumber: hashed
        PAN:         vault, retain 8 years
        *:           vault                    # any class not listed

Handling, from least to most protective:
    clear   stored as it is in the record
    vault   hashed in the record; kept whole only in the erasable vault (shio erase)
    hashed  stored nowhere in clear: hashed in the record, and scrubbed from prompts, answers, explanations and
            outcomes in both the record and the vault

When a regulation changes, compliance adds a new version with its effective date; no program changes. Each decision
is written under the versions in force on its date, and the record says which (name, version, sha256, source).
Where two policies cover the same class, the more protective rule applies.
"""
import datetime as dt
import hashlib
import json
import re
from pathlib import Path

HANDLING = ("clear", "vault", "hashed")
STRICTNESS = {h: i for i, h in enumerate(HANDLING)}
DEFAULT = "vault"           # a class no policy in force mentions: protected, but kept for audit and erasure

HEADER = re.compile(r'^policy\s+"(?P<name>[^"]+)"\s+version\s+(?P<version>\d+)\s*:\s*$')
SETTING = re.compile(r'^(?P<key>effective|source|approved_by)\s+(?P<value>.+)$')
RULE = re.compile(r"^(?P<cls>\*|[A-Z][A-Za-z0-9_]*)\s*:\s*(?P<handling>clear|vault|hashed)"
                  r"(?:\s*,\s*retain\s+(?P<years>\d+)\s+years?)?\s*$")


class PolicyError(ValueError):
    pass


class Policy:
    def __init__(self, name, version, effective, source, approved_by, rules, file, line):
        self.name, self.version, self.effective = name, version, effective
        self.source, self.approved_by, self.rules = source, approved_by, rules   # rules: {class: (handling, years)}
        self.file, self.line = file, line

    def as_dict(self):
        return {"name": self.name, "version": self.version, "effective": self.effective.isoformat(),
                "source": self.source, "approved_by": self.approved_by,
                "rules": {c: {"handling": h, "retain_years": y} for c, (h, y) in sorted(self.rules.items())}}

    @property
    def sha256(self):
        return hashlib.sha256(json.dumps(self.as_dict(), sort_keys=True).encode()).hexdigest()

    def stamp(self):
        return {"name": self.name, "version": self.version, "effective": self.effective.isoformat(),
                "sha256": self.sha256, "source": self.source, "approved_by": self.approved_by}

    def __repr__(self):
        return f"<{self.name} v{self.version} from {self.effective}>"


def _unquote(v):
    v = v.strip()
    return v[1:-1] if len(v) >= 2 and v[0] == v[-1] == '"' else v


def parse(text, file="<policy>"):
    policies, cur = [], None

    def done():
        if cur is None:
            return
        if cur["effective"] is None:
            raise PolicyError(f"{file}:{cur['line']}: policy {cur['name']!r} v{cur['version']} has no effective date")
        if not cur["source"]:
            raise PolicyError(f"{file}:{cur['line']}: policy {cur['name']!r} v{cur['version']} has no source "
                              "(the regulation or circular the rule comes from)")
        policies.append(Policy(**cur, file=file))

    for n, raw in enumerate(text.splitlines(), 1):
        s = raw.split("#", 1)[0].rstrip() if '"' not in raw.split("#", 1)[0] else raw.rstrip()
        s = re.sub(r"\s+#.*$", "", s) if s.count('"') % 2 == 0 else s
        if not s.strip():
            continue
        m = HEADER.match(s.strip())
        if m and not raw.startswith((" ", "\t")):
            done()
            cur = {"name": m["name"], "version": int(m["version"]), "effective": None, "source": "",
                   "approved_by": "", "rules": {}, "line": n}
            continue
        if cur is None or not raw.startswith((" ", "\t")):
            raise PolicyError(f"{file}:{n}: expected `policy \"NAME\" version N:`")
        body = s.strip()
        m = SETTING.match(body)
        if m:
            if m["key"] == "effective":
                try:
                    cur["effective"] = dt.date.fromisoformat(m["value"].strip())
                except ValueError:
                    raise PolicyError(f"{file}:{n}: effective date must be YYYY-MM-DD") from None
            else:
                cur[m["key"]] = _unquote(m["value"])
            continue
        m = RULE.match(body)
        if not m:
            raise PolicyError(f"{file}:{n}: expected `Class: clear|vault|hashed[, retain N years]`")
        if m["cls"] in cur["rules"]:
            raise PolicyError(f"{file}:{n}: {m['cls']} is listed twice")
        cur["rules"][m["cls"]] = (m["handling"], int(m["years"]) if m["years"] else None)
    done()
    seen = set()
    for p in policies:
        if (p.name, p.version) in seen:
            raise PolicyError(f"{file}:{p.line}: {p.name!r} version {p.version} is defined twice")
        seen.add((p.name, p.version))
    return policies


class Store:
    """Every policy version in a folder of *.policy files (or one file)."""

    def __init__(self, policies, where=""):
        self.policies, self.where = policies, where
        names = {}
        for p in policies:
            names.setdefault(p.name, []).append(p)
        for name, ps in names.items():
            ps.sort(key=lambda p: p.version)
            for a, b in zip(ps, ps[1:]):
                if b.effective < a.effective:
                    raise PolicyError(f"{name!r} v{b.version} takes effect before v{a.version}")
        self.by_name = names

    @classmethod
    def load(cls, path):
        path = Path(path)
        files = sorted(path.glob("*.policy")) if path.is_dir() else [path]
        if not files:
            raise PolicyError(f"no .policy files in {path}")
        return cls([p for f in files for p in parse(f.read_text(), str(f))], str(path))

    def in_force(self, on):
        """The version of each policy in force on a date (none if the policy had not started)."""
        out = []
        for ps in self.by_name.values():
            live = [p for p in ps if p.effective <= on]
            if live:
                out.append(max(live, key=lambda p: (p.effective, p.version)))
        return out

    def handling(self, on):
        """-> Handling: the rule for every class on a date, the most protective where policies overlap."""
        return Handling.from_policies(self.in_force(on))


class Handling:
    def __init__(self, rules, default, stamps):
        self.rules, self.default, self.stamps = rules, default, stamps   # rules: {class: (handling, years, policy)}

    @classmethod
    def from_policies(cls, policies):
        rules, default = {}, None
        for p in policies:
            for c, (h, y) in p.rules.items():
                if c == "*":
                    if default is None or STRICTNESS[h] > STRICTNESS[default[0]]:
                        default = (h, y, f"{p.name} v{p.version}")
                    continue
                if c not in rules or STRICTNESS[h] > STRICTNESS[rules[c][0]]:
                    rules[c] = (h, y, f"{p.name} v{p.version}")
        return cls(rules, default, [p.stamp() for p in policies])

    def rule(self, cls):
        """(handling, retain_years, which policy), or None when no policy in force covers the class."""
        if cls in self.rules:
            return self.rules[cls]
        return self.default

    def of(self, cls):
        r = self.rule(cls)
        return r[0] if r else DEFAULT

    def as_dict(self):
        return {"in_force": self.stamps,
                "applied": {c: {"handling": h, "retain_years": y, "by": by} for c, (h, y, by) in sorted(self.rules.items())},
                "default": ({"handling": self.default[0], "by": self.default[2]} if self.default
                            else {"handling": DEFAULT, "by": "Shio default (no policy covers the class)"})}


def classes_of(schema, prefix=""):
    """{dotted field path: class} for a type and the types nested in it ([] marks list items)."""
    out = {}
    fields = getattr(schema, "model_fields", None)
    if not fields:
        return out
    own = dict(getattr(schema, "__shio_classes__", {}) or {})
    for f in getattr(schema, "__shio_pii__", ()) or ():
        own.setdefault(f, "PII")
    for name, info in fields.items():
        path = f"{prefix}{name}"
        if name in own:
            out[path] = own[name]
        for inner in _inner_types(info.annotation):
            out.update(classes_of(inner, f"{path}[]." if _is_list(info.annotation) else f"{path}."))
    return out


def _is_list(t):
    return getattr(t, "__origin__", None) in (list, tuple) or "list[" in str(t).lower()


def _inner_types(t):
    found = []
    for a in getattr(t, "__args__", ()) or ():
        found += _inner_types(a)
    if isinstance(t, type) and hasattr(t, "model_fields"):
        found.append(t)
    return found


# ---------------- audit ----------------
def _field_states(value, prefix=""):
    """{path: "protected" | "clear"} for every leaf of a reading's value as stored."""
    out = {}
    if isinstance(value, list):         # a field is clear if any item holds it in clear
        for v in value:
            for k, state in _field_states(v, prefix).items():
                if out.get(k) != "clear":
                    out[k] = state
        return out
    if not isinstance(value, dict):
        return out
    for k, v in value.items():
        path = f"{prefix}{k}"
        if isinstance(v, dict) and "$pii" in v:
            out[path] = "protected"
        elif isinstance(v, (dict, list)):
            out.update(_field_states(v, f"{path}[]." if isinstance(v, list) else f"{path}."))
        else:
            out[path] = "clear"
    return out


def audit(folder, store, today=None):
    """Check every record against the data-handling policy in force on its date. -> (ok, report lines, findings)."""
    from .ledger import _entries, load_vault
    folder = Path(folder)
    today = today or dt.date.today()
    erased = {e["file"] for e in _entries(folder) if e.get("kind") == "erasure"}
    findings, checked, retention_due = [], 0, []
    for e in _entries(folder):
        if e.get("kind") != "record":
            continue
        path = folder / e["file"]
        if not path.exists():
            continue
        rec = json.loads(path.read_text())
        checked += 1
        on = dt.date.fromtimestamp(rec.get("started") or e.get("time"))
        expected = store.handling(on)
        stamped = (rec.get("data_policy") or {}).get("in_force")
        name = e["file"]
        if stamped is None:
            if expected.stamps:
                findings.append(f"{name}: written {on} with no data policy, though "
                                f"{', '.join(s['name'] + ' v' + str(s['version']) for s in expected.stamps)} was in force")
        else:
            want = {(s["name"], s["version"]): s for s in expected.stamps}
            got = {(s["name"], s["version"]): s for s in stamped}
            for key, s in want.items():
                if key not in got:
                    older = [g for g in got if g[0] == key[0]]
                    findings.append(f"{name}: written {on} under {key[0]} v{older[0][1] if older else '?'}, but "
                                    f"v{key[1]} was in force from {s['effective']}")
                elif got[key]["sha256"] != s["sha256"]:
                    findings.append(f"{name}: {key[0]} v{key[1]} has been edited since this record used it "
                                    "(a policy version must not change once in force; add a new version)")
        vault = load_vault(path, rec)
        for vpath, r in _readings_at(rec):
            states = _field_states(r.get("value"))
            for fpath, cls in (r.get("classes") or {}).items():
                need = expected.of(cls)
                have = states.get(fpath)
                if have is None:
                    continue
                if need in ("vault", "hashed") and have == "clear":
                    findings.append(f"{name}: {r.get('name')}.{fpath} ({cls}) is stored in clear; "
                                    f"{_by(expected, cls)} required {need}")
            if vault is not None and r.get("classes") and f"{vpath}/value" in vault:
                for v in (vault[f"{vpath}/value"],):
                    for fpath, state in _field_states(v).items():
                        cls = (r.get("classes") or {}).get(fpath)
                        if cls and expected.of(cls) == "hashed" and state == "clear":
                            findings.append(f"{name}: {fpath} ({cls}) is kept in the vault; "
                                            f"{_by(expected, cls)} required hashed (kept nowhere)")
            for fpath, cls in (r.get("classes") or {}).items():
                rule = expected.rule(cls)
                if rule and rule[1] and vault is not None and name not in erased:
                    ends = on.replace(year=on.year + rule[1]) if not (on.month == 2 and on.day == 29) else \
                        on.replace(year=on.year + rule[1], day=28)
                    if ends < today:
                        retention_due.append((name, cls, ends, rule[2]))
    due = sorted({(n, by, ends) for n, _, ends, by in retention_due})
    for n, by, ends in due:
        findings.append(f"{n}: retention under {by} ended {ends}; its vault should be erased (shio retention --apply)")
    report = [f"{checked} records checked against {store.where or 'the policies'}",
              f"{len(findings)} finding{'s' if len(findings) != 1 else ''}"]
    report += [f"  FINDING: {f}" for f in findings]
    return not findings, report, {"findings": findings, "retention_due": due}


def _readings_at(run, base=""):
    """(vault path, reading) for every reading, in the path scheme ledger.split uses."""
    for i, r in enumerate(run.get("readings", [])):
        yield f"{base}/readings/{i}", r
    for j, c in enumerate(run.get("children", [])):
        yield from _readings_at(c, f"{base}/children/{j}")


def _by(handling, cls):
    r = handling.rule(cls)
    return r[2] if r else "Shio's default"


def retention(folder, store, signer=None, apply=False, today=None):
    """Vaults whose retention period (under the policy in force when written) has ended; erase them with apply."""
    from .ledger import erase
    _, _, info = audit(folder, store, today)
    done = []
    for name, by, ends in info["retention_due"]:
        done.append((name, by, ends))
        if apply:
            erase(Path(folder) / name, f"retention ended {ends} under {by}", signer)
    return done


# ---------------- programs ----------------
def program_classes(path):
    """[(type, field, class)] declared in a .shio program."""
    from .compiler import Compiler
    c = Compiler(Path(path).read_text(), str(path))
    c.compile()
    return [(t["name"], f["name"], f.get("class")) for t in c.meta["types"] for f in t["fields"] if f.get("class")]


def check_programs(programs, store, on):
    """Every classified field of every program has an explicit rule in the policies in force on a date."""
    h = store.handling(on)
    lines, ok = [f"policies in force on {on}: " + (", ".join(f"{s['name']} v{s['version']}" for s in h.stamps) or
                                                     "none")], True
    for prog in programs:
        for tname, field, cls in program_classes(prog):
            rule = h.rule(cls)
            if rule is None:
                ok = False
                lines.append(f"  NO RULE  {Path(prog).name}: {tname}.{field} is {cls}, which no policy in force "
                             f"covers (Shio would keep it in the vault by default)")
            else:
                lines.append(f"  {rule[0]:<7}  {Path(prog).name}: {tname}.{field} ({cls}) by {rule[2]}"
                             + (f", retain {rule[1]} years" if rule[1] else ""))
    return ok, lines


def impact(programs, store, on):
    """What changes for each program when the policies in force on `on` replace those of the day before."""
    before, after = store.handling(on - dt.timedelta(days=1)), store.handling(on)
    lines = [f"on {on}: " + (", ".join(f"{s['name']} v{s['version']}" for s in after.stamps) or "no policy") +
             " replaces " + (", ".join(f"{s['name']} v{s['version']}" for s in before.stamps) or "no policy")]
    changed = 0
    for prog in programs:
        for tname, field, cls in program_classes(prog):
            a, b = before.of(cls), after.of(cls)
            if a != b:
                changed += 1
                tighter = STRICTNESS[b] > STRICTNESS[a]
                lines.append(f"  {Path(prog).name}: {tname}.{field} ({cls}) {a} -> {b}"
                             + ("  (existing records can be re-sealed: shio migrate)" if tighter else
                                "  (applies to new records only; existing ones stay as they are)"))
    lines.append(f"{changed} field{'s' if changed != 1 else ''} change")
    return lines
