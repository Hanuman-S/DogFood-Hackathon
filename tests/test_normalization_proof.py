"""JUDGING.md's Normalization Proof is generated, not written by hand: regenerating it from the fixture
file with the current engine must give exactly the committed text. If the engine or the fixture
changes, rerun `PYTHONPATH=src python scripts/normalization_proof.py --write`."""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "normalization_proof.py"


def load():
    spec = importlib.util.spec_from_file_location("normalization_proof", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_judging_md_holds_the_current_generated_proof():
    proof = load()
    committed = proof.current()
    assert committed is not None, "JUDGING.md has no normalization-proof section"
    assert committed == proof.generate()


def test_the_split_is_exact_and_the_script_says_so():
    section = load().generate()
    assert "is at most" in section and "e-1" in section  # |(raw - M2) - (lean + shrinkage)| ~ 1e-16
