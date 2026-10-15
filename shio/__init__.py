"""Shio: a language for agents in which the model reads and code decides. Aonami Labs."""
import sys
import types
from pathlib import Path

from . import train  # noqa: F401  (shio.train.reward is what generated training scripts use)
from .compiler import ShioError, compile_source, to_code
from .core import (BoundaryError, Checks, Reading, Source, agent, check, configure, parallel, read,  # noqa: F401
                   rule)

__version__ = "0.1.0"


def load(path, name=None, argv=()):
    """Compile and import a .shio file. Returns the module; its agents are ordinary callables."""
    path = Path(path).resolve()
    name = name or "shio__" + "".join(c if c.isalnum() else "_" for c in path.stem)
    code = to_code(path.read_text(), str(path))
    if str(path.parent) not in sys.path:      # a program can import Python modules that sit next to it
        sys.path.insert(0, str(path.parent))
    mod = types.ModuleType(name)
    mod.__file__ = str(path)
    mod.argv = list(argv)
    sys.modules[name] = mod     # checks and types are found by "module:qualname" when traces are replayed
    exec(code, mod.__dict__)
    return mod
