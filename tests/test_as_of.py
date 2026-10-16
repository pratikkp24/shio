"""configure(as_of=...): decisions read the tables in force on a chosen date, not the day they run."""
import datetime
import tempfile
from pathlib import Path

import shio
from shio.core import configure, _config


SRC = '''
agent rate():
    fx = in_force("FX")
    rule out:
        return {"rate": fx.gbp}
    return out
'''
POLICY = '''
table "FX" version 1:
    effective 2026-09-30
    source "test"
    approved_by "test"
    gbp: 1.29

table "FX" version 2:
    effective 2026-10-31
    source "test"
    approved_by "test"
    gbp: 1.31
'''


def test_as_of_picks_the_version_in_force_then():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "p").mkdir()
        (d / "p" / "fx.policy").write_text(POLICY)
        (d / "prog.shio").write_text(SRC)
        mod = shio.load(d / "prog.shio")
        try:
            configure(trace_dir=None, policies=str(d / "p"), as_of="2026-10-05")
            assert mod.rate() == {"rate": 1.29}
            configure(trace_dir=None, as_of="2026-11-02")
            assert mod.rate() == {"rate": 1.31}
            configure(trace_dir=None, as_of=datetime.date(2026, 10, 1))
            assert mod.rate() == {"rate": 1.29}
        finally:
            configure(trace_dir=None, policies=False, as_of="")
            assert _config["as_of"] is None
