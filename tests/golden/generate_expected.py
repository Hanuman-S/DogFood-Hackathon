"""Generate golden (b): the scoring lab's own results, at full precision, for the portal's tests.

Run ONCE with the lab's own interpreter, from anywhere:

    <lab>/.venv/Scripts/python.exe generate_expected.py <lab root> <output dir>

It imports the lab's code (lab/data.py, methods/*) and writes one JSON per input file. It writes
nothing inside the lab. The portal never imports the lab: these JSON files are the only thing that
crosses over, and they are committed under tests/golden/expected/.
"""

import json
import os
import sys
from pathlib import Path

for _v in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
    os.environ[_v] = "1"

import numpy as np  # noqa: E402

LAB = Path(sys.argv[1]).resolve()
OUT = Path(sys.argv[2]).resolve()
sys.path.insert(0, str(LAB))

from lab.data import load  # noqa: E402
from lab.graph import data_components  # noqa: E402
from methods.common import rank  # noqa: E402
from methods.registry import run  # noqa: E402

SEED = 20260926  # the lab's MASTER_SEED (eval/real_data.py, eval/export_synthetic.py)
FILES = {
    "syn_small": "data/synthetic/syn_small.json",
    "syn_medium": "data/synthetic/syn_medium.json",
    "syn_large": "data/synthetic/syn_large.json",
    "fixtures": "reference/fixtures.json",
}


def clean(values):
    return [None if (isinstance(v, float) and np.isnan(v)) else v for v in np.asarray(values, float).tolist()]


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for name, rel in FILES.items():
        (d,) = load(LAB / rel)
        results = {m: run(m, d, lam=None, seed=SEED) for m in ("raw", "z", "M2")}
        m2 = results["M2"].diag
        nj = np.bincount(d.ji, minlength=d.J)
        reviewed_judges = [j for j in range(d.J) if nj[j] > 0]
        p_next = {str(d.projects[a]): float(pr) for a, b, _delta, _sd, _z, pr in m2["neighbours"]}
        doc = {
            "source": rel,
            "seed": SEED,
            "lab_log": list(d.log),
            "n_projects": int(d.P),
            "n_reviews_used": int(d.N),
            "n_components": int(len(set(data_components(d).tolist()))),
            "projects": [str(p) for p in d.projects],
            "tracks": [str(t) for t in d.track],
            "n_reviews": np.bincount(d.pi, minlength=d.P).tolist(),
            "methods": {
                m: {"score": clean(r.score), "rank": rank(r.score).tolist(),
                    "se": clean(r.se) if r.se is not None else None}
                for m, r in results.items()
            },
            "m2": {
                "lam_q": float(m2["lam_q"]), "lam_b": float(m2["lam_b"]),
                "sigma2": float(m2["sigma2"]), "trH": float(m2["trH"]), "rss": float(m2["rss"]),
                "mu": float(m2["mu"]),
                "tie_group": [int(g) for g in m2["tie_group"]],
                "p_ahead_of_next": p_next,
                "cv": [{"lam_q": k[0], "lam_b": k[1], "mse": float(v)} for k, v in m2["cv"].items()],
                "lean": {str(d.judges[j]): float(m2["lean"][j]) for j in reviewed_judges},
                "lean_centred": {str(d.judges[j]): float(m2["lean_centred"][j]) for j in reviewed_judges},
            },
            "z_floored_judges": results["z"].diag["sd_floored_judges"],
        }
        (OUT / f"{name}.lab.json").write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
        print(f"{name}: {d.P} projects, {d.N} reviews, lam=({m2['lam_q']}, {m2['lam_b']})")


if __name__ == "__main__":
    main()
