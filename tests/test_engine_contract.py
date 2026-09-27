"""The contract every registered scoring method must meet. Parametrised over the registry, so a
new method file is tested here without anyone editing this file."""

import json

import pytest
from engine_helpers import FIXTURES, assert_contract, make_input

from scoring.engine import pipeline
from scoring.engine.io import load_organizer_file
from scoring.engine.methods import registry

METHODS = registry.available()


def _inputs():
    fixtures, _ = load_organizer_file(FIXTURES)
    return {
        "fixtures": fixtures,
        "small": make_input([("A", "J1", 4), ("B", "J1", 2), ("A", "J2", 5), ("C", "J2", 3),
                             ("B", "J3", 3), ("C", "J3", (4, 2, 3))]),
        "unreviewed": make_input([("A", "J1", 4), ("B", "J1", 3)], extra_projects=("Z",)),
        # every project gets each of 2..5 once (a Latin square): all raw means are 3.5
        "all_tied": make_input([(p, f"J{j}", 2 + (i + j) % 4) for i, p in enumerate("ABCD") for j in range(4)]),
        "disconnected": make_input([("A", "J1", 4), ("B", "J1", 2), ("A", "J2", 5), ("B", "J2", 3),
                                    ("C", "J3", 3), ("D", "J3", 5), ("C", "J4", 2), ("D", "J4", 4)]),
        "one_project": make_input([("A", "J1", 4)]),
        "empty": make_input([], extra_projects=("A", "B")),
    }


INPUTS = _inputs()


def test_registry_has_the_builtin_methods():
    assert {"raw_mean", "zscore", "m2"} <= set(METHODS)


@pytest.mark.parametrize("name", METHODS)
@pytest.mark.parametrize("case", sorted(INPUTS))
def test_every_method_meets_the_contract(name, case):
    inp = INPUTS[case]
    result = pipeline.run(inp, name)
    assert result.method == name
    assert_contract(inp, result)
    json.loads(result.to_json())  # serialisable, and valid JSON


@pytest.mark.parametrize("name", METHODS)
def test_every_method_declares_its_contract(name):
    method = registry.get(name)
    assert isinstance(method.version, str) and method.version
    assert isinstance(method.capabilities, frozenset)
