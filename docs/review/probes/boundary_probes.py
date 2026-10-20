"""Boundary probes (docs/REVIEW-BRIEF.md, findings B1-B5): does each program compile, and does the model's value
reach plain code? Run from the repository root:  python docs/review/probes/boundary_probes.py

P4 is a control (refused). P7 and P8 compile but the runtime holds: plain code gets the sealed Reading object only."""
import os, sys, textwrap, json
os.environ.setdefault("SHIO_SIGNING_KEY", "hmac:probe-only")   # regulated runs refuse to start without a key
from pathlib import Path
from tempfile import TemporaryDirectory
import shio
from shio.compiler import ShioError

HEAD = '''
LEAK = []
def stash(v):
    LEAK.append(v)
    return True

type Amount:
    value: float?
    as_printed: str

model m = replay(lambda req: {"value": 4242.5, "as_printed": "MARK-7f3"})

agent a():
    read x: Amount by m:
        How much?
'''
PROBES = {
  "P1 check purity: getattr(LEAK,'append')": 'check c: getattr(LEAK, "append")(x.value) or True, "ok"\n    return 1',
  "P2 check purity: helper defined in .shio": 'check c: stash(x.value), "ok"\n    return 1',
  "P3 check purity: list.__iadd__": 'check c: LEAK.__iadd__([x.value]) is not None, "ok"\n    return 1',
  "P4 control: __import__ in a check": 'check c: __import__ is None or True, "ok"\n    return 1',
  "P5 plain code: _SEALED via getattr string": 'import shio as S\n    return [v.value for v in getattr(S.core, "_SE" + "ALED").values()]',
  "P6 plain code: _open + _run (no reading name)": 'import shio as S\n    S.core._open.set(True)\n    return S.core._run.get().readings[0].value',
  "P7 plain code: comprehension shadows name": 'y = [x for x in [0]] + [x]\n    LEAK.append(y)\n    return 1',
  "P8 plain code: lambda arg shadows name": 'LEAK.append((lambda x: x)(x))\n    return 1',
}
MAIN_PROBES = {
  "P9 main: last_run.to_dict() after the agent": 'agent b():\n    read x: Amount by m:\n        How much?\n    rule r:\n        return 1\n    return r\n\nmain:\n    b()\n    LEAK.append(__shio.core.last_run["run"].to_dict()["readings"][0]["value"])\n',
  "P10 main: read own trace file": 'agent b():\n    read x: Amount by m:\n        How much?\n    rule r:\n        return 1\n    return r\n\nmain:\n    b()\n    import json\n    LEAK.append(json.load(open(b.last_trace))["readings"][0]["value"])\n',
}

def leaked(o):
    s = json.dumps(o, default=repr)
    return "4242.5" in s or "MARK-7f3" in s

def run(label, src, regulated, call):
    with TemporaryDirectory() as tmp:
        shio.configure(tmp)
        p = Path(tmp) / "p.shio"
        p.write_text(("regulated\n" if regulated else "") + src)
        try:
            mod = shio.load(p, name="probe_" + str(abs(hash(label + str(regulated)))))
        except ShioError as e:
            return "refused: " + str(e).splitlines()[0].split("error: ")[-1][:90]
        except Exception as e:
            return f"load error {type(e).__name__}: {e}"[:120]
        try:
            out = call(mod)
        except shio.BoundaryError as e:
            return "blocked at run time (BoundaryError)" + (" — LEAKED before" if leaked(mod.LEAK) else "")
        except Exception as e:
            return f"run error {type(e).__name__}: {str(e)[:80]}" + (" — LEAKED" if leaked(mod.LEAK) else "")
        finally:
            shio.configure("shio-traces")
        return ("LEAKED " if leaked(out) or leaked(mod.LEAK) else "ran, no leak ") + repr(mod.LEAK)[:60] + " / out=" + repr(out)[:60]

for reg in (False, True):
    print("== regulated" if reg else "== not regulated")
    for k, body in PROBES.items():
        print(f"{k}: {run(k, textwrap.dedent(HEAD) + '    ' + body + chr(10), reg, lambda m: m.a())}")
    for k, body in MAIN_PROBES.items():
        print(f"{k}: {run(k, textwrap.dedent(HEAD) + '    return 1' + chr(10) + chr(10) + body, reg, lambda m: m.__shio_main__())}")
