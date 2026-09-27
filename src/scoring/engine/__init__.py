"""The scoring engine: pure computation, no Django, no clock.

Everything here takes plain frozen dataclasses in and gives frozen dataclasses out, so a method
can be tried on a lab file from the command line (`python -m scoring.engine.cli`) exactly as the
portal will run it on an event. `tests/test_engine_purity.py` fails if any module in this package
imports Django, reads the clock, or uses an unseeded random generator.

Adding a scoring method is one new file in `methods/` with the `@register` decorator; see
`methods/base.py` for the contract.
"""
