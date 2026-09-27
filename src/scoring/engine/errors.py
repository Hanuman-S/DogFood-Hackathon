"""Errors the engine raises. All are plain exceptions with a readable message."""


class EngineError(Exception):
    """Base class for everything the engine refuses."""


class EngineInputError(EngineError):
    """The input is malformed: an unknown project, a repeated review, a value out of range..."""


class ConfigError(EngineError):
    """An engine config has an unknown key or a value of the wrong type or range."""


class UnknownMethod(EngineError, LookupError):
    """No scoring method is registered under that name."""


class MethodContractError(EngineError):
    """A method returned something the contract in `methods/base.py` forbids."""
