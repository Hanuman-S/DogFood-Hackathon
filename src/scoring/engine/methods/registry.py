"""Name -> scoring method. `@register` on a class in `methods/` is all it takes to add one."""

from __future__ import annotations

import re

from ..errors import MethodContractError, UnknownMethod
from .base import CAPABILITIES

_REGISTRY: dict[str, type] = {}
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


def register(cls):
    """Class decorator. Checks the declared contract at import time, not at first use."""
    name = getattr(cls, "name", None)
    version = getattr(cls, "version", None)
    caps = getattr(cls, "capabilities", None)
    if not isinstance(name, str) or not _NAME.match(name):
        raise MethodContractError(f"{cls.__name__}: name must match {_NAME.pattern}.")
    if not isinstance(version, str) or not version:
        raise MethodContractError(f"{name}: version must be a non-empty string.")
    if not isinstance(caps, (set, frozenset)) or not caps <= CAPABILITIES:
        raise MethodContractError(
            f"{name}: capabilities must be a subset of {sorted(CAPABILITIES)}, got {caps!r}."
        )
    if not callable(getattr(cls, "fit", None)):
        raise MethodContractError(f"{name}: no fit() method.")
    existing = _REGISTRY.get(name)
    if existing is not None and existing is not cls:
        raise MethodContractError(f"A method named {name} is already registered ({existing.__qualname__}).")
    cls.capabilities = frozenset(caps)
    _REGISTRY[name] = cls
    return cls


def unregister(name: str) -> None:
    """Remove a method (tests register throwaway methods and clean up after themselves)."""
    _REGISTRY.pop(name, None)


def get(name: str):
    """An instance of the method registered as `name`."""
    _load_builtin_methods()
    cls = _REGISTRY.get(name)
    if cls is None:
        raise UnknownMethod(
            f"No scoring method named {name!r}. Available: {', '.join(available()) or 'none'}."
        )
    return cls()


def available() -> list[str]:
    _load_builtin_methods()
    return sorted(_REGISTRY)


def describe() -> list[dict]:
    """[{name, version, capabilities}] for every registered method, sorted by name."""
    _load_builtin_methods()
    return [
        {"name": n, "version": _REGISTRY[n].version, "capabilities": sorted(_REGISTRY[n].capabilities)}
        for n in sorted(_REGISTRY)
    ]


def _load_builtin_methods():
    # Importing the package imports every module in it (see methods/__init__.py).
    from .. import methods  # noqa: F401
