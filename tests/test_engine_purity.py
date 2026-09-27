"""The engine is pure: no Django, no clock, no unseeded randomness. Checked two ways -- by reading
every module's syntax tree, and by importing the whole package in a fresh interpreter and looking
at what got loaded."""

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parents[1] / "src" / "scoring" / "engine"
FORBIDDEN_MODULES = {"django", "random"}
CLOCK_ATTRIBUTES = {"now", "utcnow", "today"}
TIME_FUNCTIONS = {"time", "monotonic", "perf_counter", "time_ns", "monotonic_ns"}


def violations(source: str, filename: str = "<src>") -> list[str]:
    tree = ast.parse(source, filename)
    found = []

    def where(node):
        return f"{filename}:{node.lineno}"

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in FORBIDDEN_MODULES:
                    found.append(f"{where(node)} imports {alias.name}")
                if alias.name == "numpy.random":
                    found.append(f"{where(node)} imports numpy.random")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.split(".")[0] in FORBIDDEN_MODULES:
                found.append(f"{where(node)} imports from {module}")
            if module == "numpy" and any(a.name == "random" for a in node.names):
                found.append(f"{where(node)} imports numpy.random")
            if module == "numpy.random" and any(a.name != "default_rng" for a in node.names):
                found.append(f"{where(node)} imports from numpy.random beyond default_rng")
            if module == "time" and any(a.name in TIME_FUNCTIONS for a in node.names):
                found.append(f"{where(node)} imports a clock from time")
        elif isinstance(node, ast.Attribute):
            if node.attr in CLOCK_ATTRIBUTES:
                found.append(f"{where(node)} reads the clock (.{node.attr})")
            if node.attr in TIME_FUNCTIONS and isinstance(node.value, ast.Name) and node.value.id == "time":
                found.append(f"{where(node)} reads the clock (time.{node.attr})")
            if (isinstance(node.value, ast.Attribute) and node.value.attr == "random"
                    and node.attr != "default_rng"):
                found.append(f"{where(node)} uses random.{node.attr} (only default_rng(seed) is allowed)")
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name == "default_rng" and not (node.args or node.keywords):
                found.append(f"{where(node)} calls default_rng() without a seed")
    return found


def engine_files():
    return sorted(ENGINE.rglob("*.py"))


def test_the_engine_package_exists():
    assert (ENGINE / "pipeline.py").exists() and len(engine_files()) >= 10


@pytest.mark.parametrize("path", engine_files(), ids=lambda p: str(p.relative_to(ENGINE)))
def test_engine_module_is_pure(path):
    assert violations(path.read_text(encoding="utf-8"), path.name) == []


@pytest.mark.parametrize("source", [
    "import django",
    "from django.db import models",
    "import random",
    "from random import shuffle",
    "from datetime import datetime\nx = datetime.now()",
    "import datetime\nx = datetime.date.today()",
    "import time\nx = time.time()",
    "import time\nx = time.monotonic()",
    "from time import perf_counter",
    "import numpy as np\nnp.random.seed(1)",
    "import numpy as np\nx = np.random.rand(3)",
    "import numpy as np\nrng = np.random.default_rng()",
    "from numpy.random import permutation",
    "from numpy import random",
])
def test_the_checker_catches_violations(source):
    assert violations(source), source


def test_the_checker_allows_a_seeded_generator():
    assert violations("import numpy as np\nrng = np.random.default_rng(seed)") == []
    assert violations("import numpy as np\nrng = np.random.default_rng([seed, 1])") == []


def test_importing_the_whole_engine_loads_no_django():
    code = (
        "import importlib, pkgutil, sys\n"
        "import scoring.engine as e\n"
        "for m in pkgutil.walk_packages(e.__path__, 'scoring.engine.'):\n"
        "    importlib.import_module(m.name)\n"
        "loaded = sorted(n for n in sys.modules if n == 'django' or n.startswith('django.'))\n"
        "print(','.join(loaded))\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "DJANGO_SETTINGS_MODULE"}
    env["PYTHONPATH"] = str(ENGINE.parents[1])
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == ""
