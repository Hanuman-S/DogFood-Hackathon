"""Adding a method is one class with @register: this test defines one inline and runs it through
run, compare and the CLI without touching any other file."""

import io

import numpy as np
import pytest
from engine_helpers import FIXTURES, assert_contract, make_input

from scoring.engine import pipeline
from scoring.engine.cli import main as cli_main
from scoring.engine.errors import MethodContractError
from scoring.engine.io import load_organizer_file
from scoring.engine.methods import registry
from scoring.engine.methods.base import MethodOutput


class BestReview:
    """Scores a project by its single best review."""

    name = "test_best_review"
    version = "0.1"
    capabilities = frozenset()

    def fit(self, data, config):
        best = np.full(data.P, -np.inf)
        np.maximum.at(best, data.pi, data.y)
        return MethodOutput(scores=best, params={"rule": "max"})


@pytest.fixture
def plugin():
    registry.register(BestReview)
    yield BestReview.name
    registry.unregister(BestReview.name)


def test_plugin_runs_through_run_compare_and_cli(plugin):
    inp, _ = load_organizer_file(FIXTURES)
    result = pipeline.run(inp, plugin)
    assert_contract(inp, result)
    assert result.method_version == "0.1"
    assert result.params_chosen == {"components": [{"component": 0, "rule": "max"}]}

    comparison = pipeline.compare(inp, [plugin, "zscore"], baseline="raw_mean")
    assert comparison.primary == plugin
    assert comparison.methods == (plugin, "zscore", "raw_mean")
    ranked = [row for row in comparison.rows if row.ranks[plugin] is not None]
    assert len(ranked) == 40                       # prj_41, the duplicate, is excluded
    assert [row.project_id for row in comparison.rows if row.ranks[plugin] is None] == ["prj_41"]

    out = io.StringIO()
    assert cli_main(["--list"], out) == 0 and plugin in out.getvalue()
    out = io.StringIO()
    assert cli_main([str(FIXTURES), "--method", plugin], out) == 0
    assert f"method {plugin} v0.1" in out.getvalue()


def test_plugin_can_be_the_configured_primary(plugin):
    result = pipeline.compare(make_input([("A", "J1", 4), ("B", "J1", 3)]),
                              config={"primary": plugin, "compare": ["raw_mean"]})
    assert result.primary == plugin


def _register_temporarily(cls):
    registry.register(cls)
    return cls.name


@pytest.mark.parametrize("scores, se, message", [
    ([1.0], None, "expected 2 scores"),
    ([1.0, float("nan")], None, "no finite score"),
    ([1.0, 2.0], [0.1, 0.1], "exactly when the method claims 'uncertainty'"),
])
def test_contract_violations_are_refused(scores, se, message):
    class Broken:
        name = "test_broken"
        version = "1"
        capabilities = frozenset()

        def fit(self, data, config):
            return MethodOutput(scores=np.array(scores), se=None if se is None else np.array(se))

    name = _register_temporarily(Broken)
    try:
        with pytest.raises(MethodContractError, match=message):
            pipeline.run(make_input([("A", "J1", 4), ("B", "J1", 3)]), name)
    finally:
        registry.unregister(name)


def test_registration_checks_the_declared_contract():
    class BadCapability:
        name = "test_bad_cap"
        version = "1"
        capabilities = frozenset({"telepathy"})

        def fit(self, data, config): ...

    class BadName:
        name = "Has Spaces"
        version = "1"
        capabilities = frozenset()

        def fit(self, data, config): ...

    class Impostor:
        name = "raw_mean"
        version = "9"
        capabilities = frozenset()

        def fit(self, data, config): ...

    for cls, message in ((BadCapability, "subset"), (BadName, "name must match"), (Impostor, "already registered")):
        with pytest.raises(MethodContractError, match=message):
            registry.register(cls)
    assert registry.get("raw_mean").version == "1"
