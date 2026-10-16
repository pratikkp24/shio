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
    drop    written nowhere in any form, not even as a hash (PCI DSS 3.3: card verification codes, PINs, OTPs): the
            record keeps only a marker that the field existed, and the value is scrubbed from every text in the
            record and the vault. Readings and checks still see the value in memory during the run.

When a regulation changes, compliance adds a new version with its effective date; no program changes. Each decision
is written under the versions in force on its date, and the record says which (name, version, sha256, source).
Where two policies cover the same class, the more protective rule applies.

One program may serve several regulators (an Indian bank with a UAE branch). A version with `applies_to "IN"` (or
`applies_to "IN", "AE"`) governs only decisions made for those jurisdictions; a version without it applies
everywhere. A decision's jurisdiction comes from the run (shio.configure(jurisdiction=...), shio run --jurisdiction,
SHIO_JURISDICTION) and is stamped into its record.
"""
import datetime as dt
import hashlib
import json
import re
from pathlib import Path

HANDLING = ("clear", "vault", "hashed", "drop")
STRICTNESS = {h: i for i, h in enumerate(HANDLING)}
DEFAULT = "vault"           # a class no policy in force mentions: protected, but kept for audit and erasure

HEADER = re.compile(r'^(?P<kind>policy|table)\s+"(?P<name>[^"]+)"\s+version\s+(?P<version>\d+)\s*:\s*$')
ROW = re.compile(r"^(?P<key>[a-z_][a-z0-9_]*)\s*:\s*(?P<value>.+)$")
SETTING = re.compile(r'^(?P<key>effective|source|approved_by|applies_to)\s+(?P<value>.+)$')
JURISDICTION = re.compile(r"^[A-Z][A-Z0-9_-]*$")
RULE = re.compile(r"^(?P<cls>\*|[A-Z][A-Za-z0-9_]*)\s*:\s*(?P<handling>clear|vault|hashed|drop)"
                  r"(?:\s*,\s*retain\s+(?P<years>\d+)\s+years?)?\s*$")


class PolicyError(ValueError):
    pass


def jurisdiction_code(text):
    """A jurisdiction as Shio records it: upper case, like IN, AE or AE-DIFC."""
    code = str(text).strip().upper()
    if not JURISDICTION.match(code):
        raise PolicyError(f"{text!r} is not a jurisdiction code (use something like IN, AE or AE-DIFC)")
    return code


class Policy:
    def __init__(self, name, version, effective, source, approved_by, rules, file, line, applies_to=()):
        self.name, self.version, self.effective = name, version, effective
        self.source, self.approved_by, self.rules = source, approved_by, rules   # rules: {class: (handling, years)}
        self.file, self.line = file, line
        self.applies_to = tuple(applies_to)      # () = every jurisdiction

    def applies(self, jurisdiction):
        """Governs decisions made for this jurisdiction (None: a decision with no jurisdiction)."""
        return not self.applies_to or jurisdiction in self.applies_to

    def _scope(self):          # only scoped versions carry the key, so older versions keep their sha256
        return {"applies_to": list(self.applies_to)} if self.applies_to else {}

    def as_dict(self):
        return {"name": self.name, "version": self.version, "effective": self.effective.isoformat(),
                "source": self.source, "approved_by": self.approved_by, **self._scope(),
                "rules": {c: {"handling": h, "retain_years": y} for c, (h, y) in sorted(self.rules.items())}}

    @property
    def sha256(self):
        return hashlib.sha256(json.dumps(self.as_dict(), sort_keys=True).encode()).hexdigest()

    def stamp(self):
        return {"name": self.name, "version": self.version, "effective": self.effective.isoformat(),
                "sha256": self.sha256, "source": self.source, "approved_by": self.approved_by, **self._scope()}

    def label(self):
        return f"{self.name} v{self.version}" + (f" [{', '.join(self.applies_to)}]" if self.applies_to else "")

    def __repr__(self):
        return f"<{self.name} v{self.version} from {self.effective}" + \
            (f" for {', '.join(self.applies_to)}>" if self.applies_to else ">")


class Table(Policy):
    """A decision table: the numbers a regulation sets (limits, caps, periods), as a dated version.

        table "Gold loan limits" version 2:
            effective 2027-04-01
            source "RBI circular 2026-27/150, para 2"
            approved_by "Head of Credit Risk"
            max_ltv: 0.75
            max_tenure_months: 12
            needs_hallmark: true
    """

    def __init__(self, name, version, effective, source, approved_by, rows, file, line, applies_to=()):
        super().__init__(name, version, effective, source, approved_by, {}, file, line, applies_to)
        self.rows = rows

    def as_dict(self):
        return {"kind": "table", "name": self.name, "version": self.version, "effective": self.effective.isoformat(),
                "source": self.source, "approved_by": self.approved_by, **self._scope(),
                "rows": dict(sorted(self.rows.items()))}


def _literal(text, file, n):
    t = text.strip()
    low = t.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("none", "null"):
        return None
    if t[:1] in "\"'":
        return _unquote(t) if t[0] == '"' else t.strip("'")
    try:
        return int(t.replace("_", "")) if re.fullmatch(r"-?[\d_]+", t) else float(t.replace("_", ""))
    except ValueError:
        raise PolicyError(f"{file}:{n}: a table value is a number, true/false or a \"quoted string\" (got {t!r})") from None


def _unquote(v):
    v = v.strip()
    return v[1:-1] if len(v) >= 2 and v[0] == v[-1] == '"' else v


def parse(text, file="<policy>"):
    policies, cur = [], None

    def done():
        if cur is None:
            return
        kind = cur.pop("kind")
        if cur["effective"] is None:
            raise PolicyError(f"{file}:{cur['line']}: {kind} {cur['name']!r} v{cur['version']} has no effective date")
        if not cur["source"]:
            raise PolicyError(f"{file}:{cur['line']}: {kind} {cur['name']!r} v{cur['version']} has no source "
                              "(the regulation or circular the rule comes from)")
        if kind == "table":
            rows = cur.pop("rules")
            policies.append(Table(**cur, rows=rows, file=file))
        else:
            policies.append(Policy(**cur, file=file))

    for n, raw in enumerate(text.splitlines(), 1):
        s = raw.split("#", 1)[0].rstrip() if '"' not in raw.split("#", 1)[0] else raw.rstrip()
        s = re.sub(r"\s+#.*$", "", s) if s.count('"') % 2 == 0 else s
        if not s.strip():
            continue
        m = HEADER.match(s.strip())
        if m and not raw.startswith((" ", "\t")):
            done()
            cur = {"kind": m["kind"], "name": m["name"], "version": int(m["version"]), "effective": None,
                   "source": "", "approved_by": "", "rules": {}, "line": n, "applies_to": ()}
            continue
        if cur is None or not raw.startswith((" ", "\t")):
            raise PolicyError(f"{file}:{n}: expected `policy \"NAME\" version N:` or `table \"NAME\" version N:`")
        body = s.strip()
        m = SETTING.match(body)
        if m:
            if m["key"] == "effective":
                try:
                    cur["effective"] = dt.date.fromisoformat(m["value"].strip())
                except ValueError:
                    raise PolicyError(f"{file}:{n}: effective date must be YYYY-MM-DD") from None
            elif m["key"] == "applies_to":
                codes = re.findall(r'"([^"]*)"', m["value"])
                if not codes or re.sub(r'"[^"]*"|[\s,]', "", m["value"]):
                    raise PolicyError(f'{file}:{n}: expected `applies_to "IN"` or `applies_to "IN", "AE"`')
                try:
                    cur["applies_to"] = tuple(dict.fromkeys(jurisdiction_code(c) for c in codes))
                except PolicyError as e:
                    raise PolicyError(f"{file}:{n}: {e}") from None
            else:
                cur[m["key"]] = _unquote(m["value"])
            continue
        if cur["kind"] == "table":
            m = ROW.match(body)
            if not m:
                raise PolicyError(f"{file}:{n}: expected `name: value` (lower-case name)")
            if m["key"] in cur["rules"]:
                raise PolicyError(f"{file}:{n}: {m['key']} is listed twice")
            cur["rules"][m["key"]] = _literal(m["value"], file, n)
            continue
        m = RULE.match(body)
        if not m:
            raise PolicyError(f"{file}:{n}: expected `Class: clear|vault|hashed|drop[, retain N years]`")
        if m["cls"] in cur["rules"]:
            raise PolicyError(f"{file}:{n}: {m['cls']} is listed twice")
        if m["handling"] == "drop" and m["years"]:
            raise PolicyError(f"{file}:{n}: a dropped class is never kept, so it has no retention period")
        cur["rules"][m["cls"]] = (m["handling"], int(m["years"]) if m["years"] else None)
    done()
    seen = set()
    for p in policies:
        if (type(p), p.name, p.version) in seen:
            raise PolicyError(f"{file}:{p.line}: {p.name!r} version {p.version} is defined twice")
        seen.add((type(p), p.name, p.version))
    return policies


class Store:
    """Every policy version in a folder of *.policy files (or one file)."""

    def __init__(self, policies, where=""):
        self.where = where
        self.policies = [p for p in policies if not isinstance(p, Table)]
        self.tables = [p for p in policies if isinstance(p, Table)]
        self.by_name, self.tables_by_name = self._group(self.policies), self._group(self.tables)

    @staticmethod
    def _group(items):
        names = {}
        for p in items:
            names.setdefault(p.name, []).append(p)
        for name, ps in names.items():
            ps.sort(key=lambda p: p.version)
            for i, b in enumerate(ps):         # within the jurisdictions two versions share, versions go forward in time
                for a in ps[:i]:
                    shared = not a.applies_to or not b.applies_to or set(a.applies_to) & set(b.applies_to)
                    if shared and b.effective < a.effective:
                        raise PolicyError(f"{name!r} v{b.version} takes effect before v{a.version}")
        return names

    @property
    def jurisdictions(self):
        """Every jurisdiction a version is scoped to (empty when every version applies everywhere)."""
        return sorted({j for p in self.policies + self.tables for j in p.applies_to})

    @property
    def scoped(self):
        return bool(self.jurisdictions)

    def scoped_on(self, on):
        """Some jurisdiction-scoped version had taken effect by this date."""
        return any(p.applies_to and p.effective <= on for p in self.policies + self.tables)

    @staticmethod
    def _latest(versions, on, jurisdiction):
        live = [p for p in versions if p.effective <= on and p.applies(jurisdiction)]
        return max(live, key=lambda p: (p.effective, p.version)) if live else None

    def table(self, name, on, jurisdiction=None):
        """The version of a decision table in force on a date for a jurisdiction, or None. With no jurisdiction,
        only versions that apply everywhere count."""
        return self._latest(self.tables_by_name.get(name, []), on, jurisdiction)

    @classmethod
    def load(cls, path):
        path = Path(path)
        files = sorted(path.glob("*.policy")) if path.is_dir() else [path]
        if not files:
            raise PolicyError(f"no .policy files in {path}")
        return cls([p for f in files for p in parse(f.read_text(), str(f))], str(path))

    def in_force(self, on, jurisdiction=None):
        """The version of each data-handling policy in force on a date for a jurisdiction (none if the policy had not
        started there). With no jurisdiction, only versions that apply everywhere count."""
        return [p for p in (self._latest(ps, on, jurisdiction) for ps in self.by_name.values()) if p is not None]

    def handling(self, on, jurisdiction=None):
        """-> Handling: the rule for every class on a date, the most protective where policies overlap."""
        h = Handling.from_policies(self.in_force(on, jurisdiction))
        h.jurisdiction = jurisdiction
        return h

    def require_jurisdiction(self, jurisdiction, what="this decision"):
        """-> the jurisdiction code to resolve under, refusing to guess: a store with jurisdiction-scoped versions
        needs one, and it must be one the store knows."""
        if jurisdiction is None:
            if self.scoped:
                raise PolicyError(f"the policies in {self.where or 'the store'} are scoped to jurisdictions "
                                  f"({', '.join(self.jurisdictions)}); {what} needs one "
                                  "(--jurisdiction, SHIO_JURISDICTION or shio.configure(jurisdiction=...))")
            return None
        code = jurisdiction_code(jurisdiction)
        if self.scoped and code not in self.jurisdictions:
            raise PolicyError(f"no policy or table version in {self.where or 'the store'} is scoped to {code} "
                              f"(scoped ones: {', '.join(self.jurisdictions)}); add `applies_to \"{code}\"` to the "
                              "versions that govern it")
        return code


class Handling:
    def __init__(self, rules, default, stamps):
        self.rules, self.default, self.stamps = rules, default, stamps   # rules: {class: (handling, years, policy)}
        self.jurisdiction = None

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
        return {**({"jurisdiction": self.jurisdiction} if self.jurisdiction else {}), "in_force": self.stamps,
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
    """{path: "dropped" | "protected" | "clear"} for every leaf of a reading's value as stored."""
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
        if isinstance(v, dict) and "$dropped" in v:
            out[path] = "dropped"
        elif isinstance(v, dict) and "$pii" in v:
            out[path] = "protected"
        elif isinstance(v, (dict, list)):
            out.update(_field_states(v, f"{path}[]." if isinstance(v, list) else f"{path}."))
        else:
            out[path] = "clear"
    return out


def audit(folder, store, today=None, jurisdiction=None):
    """Check every record against the data-handling policy in force on its date, for its jurisdiction.
    jurisdiction: audit only the records decided for it. -> (ok, report lines, findings)."""
    from collections import Counter
    from .ledger import _entries, load_vault
    folder = Path(folder)
    today = today or dt.date.today()
    want_j = jurisdiction_code(jurisdiction) if jurisdiction else None
    erased = {e["file"] for e in _entries(folder) if e.get("kind") == "erasure"}
    findings, checked, retention_due, by_j = [], 0, [], Counter()
    for e in _entries(folder):
        if e.get("kind") != "record":
            continue
        path = folder / e["file"]
        if not path.exists():
            continue
        rec = json.loads(path.read_text())
        j = rec.get("jurisdiction")
        if want_j and j != want_j:
            continue
        checked += 1
        by_j[j or "(none)"] += 1
        on = dt.date.fromtimestamp(rec.get("started") or e.get("time"))
        expected = store.handling(on, j)
        stamped = (rec.get("data_policy") or {}).get("in_force")
        name = e["file"] + (f" [{j}]" if j else "")
        if j is None and store.scoped_on(on) and (stamped is not None or rec.get("tables")):
            findings.append(f"{name}: written {on} with no jurisdiction, though jurisdiction-scoped versions were "
                            f"in force ({', '.join(store.jurisdictions)}); its rules can't be told apart")
        elif j is not None and store.scoped and j not in store.jurisdictions:
            findings.append(f"{name}: decided for {j}, which no version in {store.where or 'the store'} is scoped to")
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
        for t in rec.get("tables") or []:
            current = store.table(t["name"], on, j)
            if current is None:
                findings.append(f"{name}: used {t['name']!r} v{t['version']}, but no version of it was in force on {on}"
                                + (f" for {j}" if j else ""))
            elif current.version != t["version"]:
                findings.append(f"{name}: decided {on} with {t['name']!r} v{t['version']}, but v{current.version} "
                                f"was in force from {current.effective}")
            elif current.sha256 != t["sha256"]:
                findings.append(f"{name}: table {t['name']!r} v{t['version']} has been edited since this decision "
                                "used it (add a new version instead)")
            else:
                changed = {k: (v, current.rows.get(k)) for k, v in (t.get("used") or {}).items()
                           if current.rows.get(k) != v}
                for k, (was, now) in changed.items():
                    findings.append(f"{name}: used {t['name']}.{k} = {was!r}, but v{t['version']} says {now!r}")
        vault = load_vault(path, rec)
        findings += [f"{name}: {where} holds {what} ({cls}) in clear outside the vault; {_by(expected, cls)} "
                     f"required {expected.of(cls)}" for where, what, cls in _copies(rec, vault, expected)]
        for vpath, r in _readings_at(rec):
            states = _field_states(r.get("value"))
            for fpath, cls in (r.get("classes") or {}).items():
                need = expected.of(cls)
                have = states.get(fpath)
                if have is None:
                    continue
                if need == "drop" and have != "dropped":
                    findings.append(f"{name}: {r.get('name')}.{fpath} ({cls}) is stored "
                                    f"{'in clear' if have == 'clear' else 'as a hash'}; "
                                    f"{_by(expected, cls)} required drop (written nowhere, not even hashed)")
                elif need in ("vault", "hashed") and have == "clear":
                    findings.append(f"{name}: {r.get('name')}.{fpath} ({cls}) is stored in clear; "
                                    f"{_by(expected, cls)} required {need}")
            if vault is not None and r.get("classes") and f"{vpath}/value" in vault:
                for v in (vault[f"{vpath}/value"],):
                    for fpath, state in _field_states(v).items():
                        cls = (r.get("classes") or {}).get(fpath)
                        if cls and expected.of(cls) == "hashed" and state == "clear":
                            findings.append(f"{name}: {fpath} ({cls}) is kept in the vault; "
                                            f"{_by(expected, cls)} required hashed (kept nowhere)")
                        elif cls and expected.of(cls) == "drop" and state != "dropped":
                            findings.append(f"{name}: {fpath} ({cls}) is kept in the vault; "
                                            f"{_by(expected, cls)} required drop (written nowhere)")
            for fpath, cls in (r.get("classes") or {}).items():
                rule = expected.rule(cls)
                if rule and rule[1] and vault is not None and e["file"] not in erased:
                    ends = on.replace(year=on.year + rule[1]) if not (on.month == 2 and on.day == 29) else \
                        on.replace(year=on.year + rule[1], day=28)
                    if ends < today:
                        retention_due.append((e["file"], cls, ends, rule[2]))
    due = sorted({(n, by, ends) for n, _, ends, by in retention_due})
    for n, by, ends in due:
        findings.append(f"{n}: retention under {by} ended {ends}; its vault should be erased (shio retention --apply)")
    split_j = "" if set(by_j) == {"(none)"} or not by_j else \
        " (" + ", ".join(f"{k}: {v}" for k, v in sorted(by_j.items())) + ")"
    report = [f"{checked} records checked against {store.where or 'the policies'}" + split_j,
              f"{len(findings)} finding{'s' if len(findings) != 1 else ''}"]
    report += [f"  FINDING: {f}" for f in findings]
    return not findings, report, {"findings": findings, "retention_due": due, "by_jurisdiction": dict(by_j)}


_NOT_COPIES = ("integrity", "data_policy", "classes", "handled", "pii", "program", "vault", "migrations")


def _copies(rec, vault, expected):
    """[(JSON path, reading.field, class)] for every classified value the policy keeps out of the record (vault,
    hashed, drop) that the record holds in clear somewhere other than the reading itself: a rule that copied it
    into its outcome, a check key, an error. Values are known from the reading (if in clear), its vault copy, or
    the hash the record keeps of it."""
    from .ledger import SCRUB_MIN, _copy_of, _leaves, sha256
    values, hashes = {}, {}
    for vpath, r in _readings_at(rec):
        classes = {f: c for f, c in (r.get("classes") or {}).items() if expected.of(c) != "clear"}
        if not classes:
            continue
        held = [r.get("value")] + ([vault[f"{vpath}/value"]] if vault and f"{vpath}/value" in vault else [])
        for v in held:
            for f, cls, x in _leaves(v, classes):
                values.setdefault(str(x), (f"{r.get('name')}.{f}", cls))
        for f, h in _hashes(r.get("value"), classes):
            hashes.setdefault(h, (f"{r.get('name')}.{f}", classes[f]))
    if not values and not hashes:
        return []
    found, texts = [], {k: k for k in values}

    def walk(node, where):
        if isinstance(node, dict):
            if not _protected_node(node):
                for k, v in node.items():
                    walk(v, f"{where}/{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{where}/{i}")
        elif isinstance(node, (str, int, float)) and not isinstance(node, bool):
            text = _copy_of(node, texts)
            if text is not None:
                found.append((where, *values[text]))
            elif len(str(node)) >= SCRUB_MIN and sha256(node) in hashes:
                found.append((where, *hashes[sha256(node)]))

    def run(r, base):                   # the run's and its readings' own bookkeeping is not a copy
        for k, v in r.items():
            if k == "children":
                for i, c in enumerate(v):
                    run(c, f"{base}/children/{i}")
            elif k == "readings":
                for i, rd in enumerate(v):
                    for k2, v2 in rd.items():
                        if k2 not in _NOT_COPIES and k2 != "value":
                            walk(v2, f"{base}/readings/{i}/{k2}")
            elif k not in _NOT_COPIES:
                walk(v, f"{base}/{k}")
    run(rec, "")
    return found


def _protected_node(node):
    return "$pii" in node or "$dropped" in node or "$vault" in node


def _hashes(value, classes, prefix=""):
    """(path, sha256) of every classified field a reading's value keeps as a hash."""
    if isinstance(value, list):
        for v in value:
            yield from _hashes(v, classes, prefix)
    elif isinstance(value, dict) and "$pii" not in value:
        for k, v in value.items():
            path = f"{prefix}{k}"
            if isinstance(v, dict) and "$pii" in v:
                if path in classes:
                    yield path, v["$pii"]
            elif isinstance(v, (dict, list)):
                yield from _hashes(v, classes, f"{path}[]." if isinstance(v, list) else f"{path}.")


def _readings_at(run, base=""):
    """(vault path, reading) for every reading, in the path scheme ledger.split uses."""
    for i, r in enumerate(run.get("readings", [])):
        yield f"{base}/readings/{i}", r
    for j, c in enumerate(run.get("children", [])):
        yield from _readings_at(c, f"{base}/children/{j}")


def _by(handling, cls):
    r = handling.rule(cls)
    return r[2] if r else "Shio's default"


def retention(folder, store, signer=None, apply=False, today=None, jurisdiction=None):
    """Vaults whose retention period (under the policy in force when written, for its jurisdiction) has ended;
    erase them with apply."""
    from .ledger import erase
    _, _, info = audit(folder, store, today, jurisdiction)
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


def program_tables(path):
    """Decision-table names a program reads with in_force("...")."""
    return sorted(set(re.findall(r'in_force\(\s*"([^"]+)"\s*\)', Path(path).read_text())))


def _scopes(store, jurisdiction):
    """The jurisdictions to look at: the one asked for, else every one the store is scoped to (or just None)."""
    if jurisdiction:
        return [store.require_jurisdiction(jurisdiction)]
    return store.jurisdictions or [None]


def check_programs(programs, store, on, jurisdiction=None):
    """Every classified field has an explicit rule, and every decision table a program reads has a version, in the
    policies in force on a date, for a jurisdiction (default: each jurisdiction the store is scoped to)."""
    ok, lines = True, []
    for j in _scopes(store, jurisdiction):
        one_ok, one = _check_programs(programs, store, on, j)
        ok, lines = ok and one_ok, lines + one
    return ok, lines


def _check_programs(programs, store, on, j):
    h = store.handling(on, j)
    lines, ok = [f"policies in force on {on}" + (f" for {j}" if j else "") + ": "
                 + (", ".join(f"{s['name']} v{s['version']}" for s in h.stamps) or "none")], True
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
        for name in program_tables(prog):
            t = store.table(name, on, j)
            if t is None:
                ok = False
                lines.append(f"  NO TABLE {Path(prog).name}: reads table {name!r}, which has no version in force"
                             + (f" for {j}" if j else ""))
            else:
                lines.append(f"  table    {Path(prog).name}: {name!r} v{t.version} (from {t.effective}): "
                             + ", ".join(f"{k}={v!r}" for k, v in sorted(t.rows.items())))
    return ok, lines


def impact(programs, store, on, jurisdiction=None):
    """What changes for each program when the policies in force on `on` replace those of the day before, for a
    jurisdiction (default: each jurisdiction the store is scoped to)."""
    lines = []
    for j in _scopes(store, jurisdiction):
        lines += _impact(programs, store, on, j)
    return lines


def _impact(programs, store, on, j):
    day_before = on - dt.timedelta(days=1)
    before, after = store.handling(day_before, j), store.handling(on, j)
    lines = [f"what changes on {on}" + (f" for {j}" if j else "") + ":"]
    for name, ps in store.by_name.items():
        new = [p for p in ps if p.effective == on and p.applies(j)]
        if new:
            old = store.in_force(day_before, j)
            was = next((p for p in old if p.name == name), None)
            lines.append(f"  data policy {name!r} v{new[-1].version} replaces " + (f"v{was.version}" if was else "none"))
    for name, ts in store.tables_by_name.items():
        new = [t for t in ts if t.effective == on and t.applies(j)]
        if new:
            was = store.table(name, day_before, j)
            lines.append(f"  table {name!r} v{new[-1].version} replaces " + (f"v{was.version}" if was else "none"))
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
        for name in program_tables(prog):
            a, b = store.table(name, day_before, j), store.table(name, on, j)
            if (a and a.version) != (b and b.version):
                diff = {k: ((a.rows if a else {}).get(k), (b.rows if b else {}).get(k))
                        for k in set((a.rows if a else {})) | set((b.rows if b else {}))}
                diff = {k: v for k, v in diff.items() if v[0] != v[1]}
                changed += len(diff)
                for k, (was, now) in sorted(diff.items()):
                    lines.append(f"  {Path(prog).name}: table {name!r}.{k} {was!r} -> {now!r}"
                                 f"  (see what past decisions would become: shio replay RECORD --policies DIR "
                                 f"--at {on}" + (f" --jurisdiction {j}" if j else "") + ")")
    lines.append(f"{changed} field{'s' if changed != 1 else ''} change")
    return lines
