"""The final score: judges and community, combined by percentile rank. Pure: no Django, no clock.

    judge_pct = the mid-rank percentile of the project's judged (M2) score within the event
    vote_pct  = the mid-rank percentile of its total vote influence within the event
    final     = judge_weight/100 * judge_pct + community_weight/100 * vote_pct

Percentile of a value among n: (r - 1) / (n - 1), where r is its mid-rank (1 = lowest; exactly
equal values share the average of the ranks they span). So the top value is 1, the bottom 0, and
ties sit halfway. n = 1 gives 1/2. A project with no votes has influence 0: all such projects tie,
and share the bottom mid-rank.

Why ranks, not the raw numbers: a percentile cannot be pushed past the top by piling on more votes
(stuffing buys at most first place in the vote column), and it puts two very differently scaled
numbers on one scale. The cost: it throws away magnitude (a landslide and a hair's-breadth win look
the same), and the combined score has no standard error -- only the judged component carries one.

Exactness: equal inputs are decided after rounding to `decimals` (the engine's `equal_decimals`),
and every percentile and final score is a Fraction, so the combined ranking has no hidden tie-breaks
from floating-point rounding: two projects share a final rank exactly when their fractions are
equal. Weights are integers summing to 100.

With community_weight = 0, final = judge_pct, a strictly increasing function of the judged score,
so the final ranking is exactly the judged ranking (ties included).
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Mapping


@dataclass(frozen=True)
class CombinedRow:
    project_id: str
    judge_score: float
    judge_pct: Fraction
    influence: float
    vote_pct: Fraction
    final: Fraction
    final_rank: int  # shared by exact ties: 1 + the number of projects with a strictly higher final


def mid_rank_percentiles(values: Mapping[str, float], decimals: int = 9) -> dict[str, Fraction]:
    """{id: percentile in [0, 1]} by mid-rank, equal values (after rounding) sharing one."""
    n = len(values)
    if n == 0:
        return {}
    if n == 1:
        return {next(iter(values)): Fraction(1, 2)}
    rounded = {k: round(float(v), decimals) for k, v in values.items()}
    ordered = sorted(set(rounded.values()))
    counts = {v: 0 for v in ordered}
    for v in rounded.values():
        counts[v] += 1
    pct, below = {}, 0
    for v in ordered:
        k = counts[v]
        # ranks below+1 .. below+k share the mid-rank below + (k + 1) / 2
        mid = Fraction(2 * below + k + 1, 2)
        pct[v] = (mid - 1) / (n - 1)
        below += k
    return {key: pct[rounded[key]] for key in values}


def combine(judge_scores: Mapping[str, float], influence: Mapping[str, float], judge_weight: int,
            community_weight: int, decimals: int = 9) -> list[CombinedRow]:
    """The combined ranking of the projects in `judge_scores` (the ranked ones), best first.
    `influence` may omit projects (no votes = 0) and may name others (ignored)."""
    if not (isinstance(judge_weight, int) and isinstance(community_weight, int)):
        raise TypeError("weights are whole numbers")
    if judge_weight < 0 or community_weight < 0 or judge_weight + community_weight != 100:
        raise ValueError("weights are each at least 0 and sum to 100")
    ids = list(judge_scores)
    votes = {pid: float(influence.get(pid, 0.0) or 0.0) for pid in ids}
    judge_pct = mid_rank_percentiles({pid: judge_scores[pid] for pid in ids}, decimals)
    vote_pct = mid_rank_percentiles(votes, decimals)
    final = {pid: (judge_weight * judge_pct[pid] + community_weight * vote_pct[pid]) / 100 for pid in ids}
    rows = [
        CombinedRow(pid, float(judge_scores[pid]), judge_pct[pid], votes[pid], vote_pct[pid], final[pid],
                    1 + sum(1 for other in ids if final[other] > final[pid]))
        for pid in ids
    ]
    # best first; within an exact tie, by judged score, then id -- display order only, the rank is shared
    rows.sort(key=lambda r: (-r.final, -round(r.judge_score, decimals), r.project_id))
    return rows


def to_json(rows: list[CombinedRow]) -> list[dict]:
    """Plain JSON: fractions as floats for display, and as exact "p/q" strings."""
    return [{
        "project_id": r.project_id,
        "judge_score": r.judge_score,
        "judge_pct": float(r.judge_pct), "judge_pct_exact": f"{r.judge_pct.numerator}/{r.judge_pct.denominator}",
        "influence": r.influence,
        "vote_pct": float(r.vote_pct), "vote_pct_exact": f"{r.vote_pct.numerator}/{r.vote_pct.denominator}",
        "final": float(r.final), "final_exact": f"{r.final.numerator}/{r.final.denominator}",
        "final_rank": r.final_rank,
    } for r in rows]
