"""M2: the ridge bias model. Ported from the scoring lab's `methods/m2_ridge.py`, which implements
the ridge PDF's sections 3-11 and reproduces its worked example.

    y_pj = mu + q_p + b_j + eps        (y: one review's weighted score)
    minimise |y - Ax|^2 + lambda_q |q|^2 + lambda_b |b|^2      (mu is not penalised)
    (A'A + Lambda) x = A'y
    sigma^2 = max(RSS / (N - tr H), sigma2_floor)
    Cov = sigma^2 (A'A + Lambda)^-1
    score S_p = mu + q_p,  SE_p = sqrt(C_00 + C_pp + 2 C_0p)

q_p is the project's quality and b_j the judge's lean; a harsh judge's low scores are explained by
their b, not by the projects they happened to review. The penalties shrink thinly-evidenced
estimates towards zero: a judge with one review barely moves, a project with two reviews is pulled
towards the mean. (lambda_q, lambda_b) is chosen by k-fold cross-validation over reviews:
lowest error wins; exact ties go to the larger lambda_b, then the larger lambda_q.

Differences from the lab are deliberate and do not change any number:
* P(ahead) uses Phi via math.erf, not scipy (see ties.py).
* The CV seed comes from the pipeline (`data.rng_seed`), not a module constant.
* The leverage h_ii of every review is returned too (for the outlier flag); tr H is their sum.
"""

from __future__ import annotations

import itertools

import numpy as np

from .base import MethodOutput
from .registry import register

FALLBACK_LAMBDAS = (0.5, 1.0)  # the lab's DEFAULT_LAM, the PDF's starting values


def _cols(pi, ji, P):
    return np.stack([np.zeros_like(pi), 1 + pi, 1 + P + ji], axis=1)


def gram(pi, ji, P, J):
    """A'A for the design (without penalties)."""
    K = 1 + P + J
    cols = _cols(pi, ji, P)
    M = np.zeros((K, K))
    for a in range(3):
        for b in range(3):
            np.add.at(M, (cols[:, a], cols[:, b]), 1.0)
    return M


def rhs(y, pi, ji, P, J):
    K = 1 + P + J
    cols = _cols(pi, ji, P)
    v = np.zeros(K)
    for a in range(3):
        np.add.at(v, cols[:, a], y)
    return v


def penalty(P, J, lq, lb):
    return np.concatenate([[0.0], np.full(P, lq), np.full(J, lb)])


def solve(y, pi, ji, P, J, lq, lb, sigma2_floor=0.05, full=True):
    """Closed-form ridge fit. Returns x, or (x, M^-1, tr H, RSS, sigma^2, h) when `full`."""
    M = gram(pi, ji, P, J)
    M[np.diag_indices_from(M)] += penalty(P, J, lq, lb)
    v = rhs(y, pi, ji, P, J)
    if not full:
        return np.linalg.solve(M, v)
    # A zero penalty leaves the mu/q/b shift unidentified (PDF section 3): the system is singular and
    # inv() can return garbage without raising, so use the minimum-norm pseudo-inverse instead.
    Minv = np.linalg.pinv(M, hermitian=True) if min(lq, lb) <= 0 else np.linalg.inv(M)
    x = Minv @ v
    cols = _cols(pi, ji, P)
    hat = sum(Minv[cols[:, a], cols[:, b]] for a in range(3) for b in range(3))
    trH = sum(Minv[cols[:, a], cols[:, b]].sum() for a in range(3) for b in range(3))
    fitted = x[cols].sum(axis=1)
    rss = float(((y - fitted) ** 2).sum())
    dof = len(y) - trH
    sigma2 = max(rss / dof, sigma2_floor) if dof > 0 else sigma2_floor
    return x, Minv, float(trH), rss, float(sigma2), np.asarray(hat, dtype=float)


def cross_validate(y, pi, ji, P, J, seed, k=5, grid=(0.25, 0.5, 1.0, 2.0, 4.0)):
    """K-fold CV over reviews (PDF section 8). Returns (lam_q, lam_b), {(lq, lb): mean sq. error}."""
    rng = np.random.default_rng(seed)
    folds = np.array_split(rng.permutation(len(y)), k)
    sse = {pair: 0.0 for pair in itertools.product(grid, grid)}
    for f in folds:
        train = np.ones(len(y), bool)
        train[f] = False
        base = gram(pi[train], ji[train], P, J)
        v = rhs(y[train], pi[train], ji[train], P, J)
        test_cols = _cols(pi[f], ji[f], P)
        for lq, lb in sse:
            M = base.copy()
            M[np.diag_indices_from(M)] += penalty(P, J, lq, lb)
            x = np.linalg.solve(M, v)
            sse[(lq, lb)] += float(((y[f] - x[test_cols].sum(axis=1)) ** 2).sum())
    table = {pair: e / len(y) for pair, e in sse.items()}
    # lowest error; exact ties go to the larger lam_b, then the larger lam_q (the lab's PLAN.md)
    best = min(table, key=lambda pr: (round(table[pr], 12), -pr[1], -pr[0]))
    return best, table


@register
class RidgeBiasModel:
    name = "m2"
    version = "1"
    capabilities = frozenset({"uncertainty", "judge_bias", "decomposition", "fitted"})

    def fit(self, data, config):
        y, pi, ji, P, J, N = data.y, data.pi, data.ji, data.P, data.J, data.N
        grid = tuple(config.lambda_grid)
        cv_table, reason, boundary = None, None, None
        if config.lambdas is not None:
            lam, source = tuple(config.lambdas), "fixed"
            reason = "lambdas set in the config; no cross-validation"
        elif N < config.cv_min_reviews:
            lam, source = FALLBACK_LAMBDAS, "fallback"
            reason = (f"{N} reviews is below cv_min_reviews={config.cv_min_reviews}: too few to "
                      f"cross-validate, so the lab's fixed ({FALLBACK_LAMBDAS[0]}, {FALLBACK_LAMBDAS[1]}) is used")
        else:
            lam, cv_table = cross_validate(y, pi, ji, P, J, data.rng_seed, k=config.cv_folds, grid=grid)
            source = "cv"
            boundary = bool(min(grid) in lam or max(grid) in lam)
        lq, lb = float(lam[0]), float(lam[1])

        x, Minv, trH, rss, sigma2, hat = solve(y, pi, ji, P, J, lq, lb, config.sigma2_floor)
        mu, q, b = x[0], x[1:1 + P], x[1 + P:]
        C = sigma2 * Minv
        scores = mu + q
        se = np.sqrt(C[0, 0] + np.diag(C)[1:1 + P] + 2 * C[0, 1:1 + P])
        fitted = x[_cols(pi, ji, P)].sum(axis=1)

        params = {
            "lam_q": lq, "lam_b": lb, "lambda_source": source,
            "cv_seed": list(data.rng_seed) if isinstance(data.rng_seed, tuple) else data.rng_seed,
            "lambda_at_grid_boundary": boundary,
            "sigma2": sigma2, "trH": trH, "rss": rss, "mu": float(mu), "n_reviews_used": int(N),
        }
        diagnostics = {}
        if reason:
            diagnostics["lambda_reason"] = reason
        if cv_table is not None:
            diagnostics["cv"] = [{"lam_q": k[0], "lam_b": k[1], "mse": v} for k, v in cv_table.items()]
        return MethodOutput(
            scores=scores, se=se, cov_q=C[1:1 + P, 1:1 + P], judge_bias=b,
            fitted=fitted, hat=hat, sigma2=sigma2, params=params, diagnostics=diagnostics,
        )
