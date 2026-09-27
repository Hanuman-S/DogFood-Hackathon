"""Scoring methods. Every module in this package is imported here, so a new method is one new
file with `@register` -- no list to edit anywhere else."""

import importlib
import pkgutil

_INFRASTRUCTURE = {"base", "registry"}

for _module in sorted(pkgutil.iter_modules(__path__), key=lambda m: m.name):
    if _module.name not in _INFRASTRUCTURE and not _module.name.startswith("_"):
        importlib.import_module(f"{__name__}.{_module.name}")
