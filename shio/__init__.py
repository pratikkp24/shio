"""Shio: a language for agents in which the model reads and code decides. Aonami Labs."""
import hashlib
import re
import sys
import types
from pathlib import Path

from . import train  # noqa: F401  (shio.train.reward is what generated training scripts use)
from .compiler import ShioError, compile_source, to_code
from .core import (BoundaryError, Checks, Reading, RegulatedError, Source, agent, check, configure, each,  # noqa: F401
                   parallel, read, rule)

__version__ = "0.1.0"


def load(path, name=None, argv=()):
    """Compile and import a .shio file. Returns the module; its agents are ordinary callables."""
    path = Path(path).resolve()
    name = name or "shio__" + "".join(c if c.isalnum() else "_" for c in path.stem)
    src = path.read_text()
    code = to_code(src, str(path))
    if str(path.parent) not in sys.path:      # a program can import Python modules that sit next to it
        sys.path.insert(0, str(path.parent))
    mod = types.ModuleType(name)
    mod.__file__ = str(path)
    mod.argv = list(argv)
    mod.__shio_program__ = program_identity(path, src)
    sys.modules[name] = mod     # checks and types are found by "module:qualname" when traces are replayed
    exec(code, mod.__dict__)
    return mod


def program_identity(path, src):
    """What every decision record says about the program that made it: the exact source (sha256), the compiler,
    whether it is regulated, and the local Python modules it imports (trusted code, part of what is audited)."""
    deps = {}
    for m in dict.fromkeys(re.findall(r"(?m)^\s*(?:from|import)\s+([A-Za-z_]\w*)", src)):
        f = path.parent / f"{m}.py"
        if f.exists():
            deps[f.name] = hashlib.sha256(f.read_bytes()).hexdigest()
    return {"path": str(path), "sha256": hashlib.sha256(src.encode()).hexdigest(), "shio": __version__,
            "regulated": bool(re.search(r"(?m)^regulated\s*(#.*)?$", src)), "dependencies": deps}
