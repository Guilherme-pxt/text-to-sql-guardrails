"""Result matching, paired bootstrap, and calibration metrics.

Execution accuracy compares the result a system showed with the gold result,
not the SQL text. Two results match when they have the same number of rows
and there is a one-to-one mapping from every gold column to a distinct
predicted column under which the rows match as a multiset. Extra predicted
columns are allowed (a name next to the requested SKU is fine); row order is
ignored (the gold queries that use ORDER BY ... LIMIT have no tie at the
cut-off, so the set is what the question asks for). Values compare as numbers
when both are numbers: equal within 1e-6 relative, or the prediction equals
the gold value rounded to 1 or 2 decimals (or to an integer when the gold
value is 100 or more); strings compare after strip()
and lower().
"""

from __future__ import annotations

import itertools

import numpy as np


def _num(v):
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.replace(",", ""))
        except ValueError:
            return None
    return None


def value_eq(gold, pred) -> bool:
    if gold is None or pred is None:
        return gold is None and pred is None
    g, p = _num(gold), _num(pred)
    if g is not None and p is not None and not (isinstance(gold, str) and isinstance(pred, str)):
        if abs(g - p) <= 1e-6 * max(1.0, abs(g)):
            return True
        digits = (1, 2) if abs(g) < 100 else (0, 1, 2)
        return any(abs(p - round(g, d)) <= 1e-9 for d in digits)
    return str(gold).strip().lower() == str(pred).strip().lower()


def _rows_match(gold_rows, pred_rows, mapping) -> bool:
    used = [False] * len(pred_rows)
    for g in gold_rows:
        hit = False
        for i, p in enumerate(pred_rows):
            if not used[i] and all(value_eq(g[gc], p[pc]) for gc, pc in mapping):
                used[i] = hit = True
                break
        if not hit:
            return False
    return True


def result_matches(gold_cols: list, gold_rows: list[list], pred_cols: list, pred_rows: list[list]) -> bool:
    if len(gold_rows) != len(pred_rows):
        return False
    ng, npred = len(gold_cols), len(pred_cols)
    if npred < ng:
        return False
    if not gold_rows:
        return True
    # candidate predicted columns per gold column: same values as a multiset
    cands = []
    for gc in range(ng):
        gvals = [r[gc] for r in gold_rows]
        ok = []
        for pc in range(npred):
            pvals = [r[pc] for r in pred_rows]
            if _rows_match([[v] for v in gvals], [[v] for v in pvals], [(0, 0)]):
                ok.append(pc)
        if not ok:
            return False
        cands.append(ok)
    for combo in itertools.product(*cands):
        if len(set(combo)) == ng and _rows_match(gold_rows, pred_rows, list(zip(range(ng), combo))):
            return True
    return False


# ------------------------------------------------------------------ intervals


def bootstrap_ci(values, n: int = 10_000, seed: int = 42) -> tuple[float, float]:
    x = np.asarray(values, dtype=float)
    if len(x) == 0:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(x), size=(n, len(x)))
    lo, hi = np.percentile(x[idx].mean(axis=1), [2.5, 97.5])
    return float(lo), float(hi)


def paired_bootstrap(a, b, n: int = 10_000, seed: int = 42) -> dict:
    """Delta mean(b) - mean(a) over the same items, with a 95% percentile interval."""
    xa, xb = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    if xa.shape != xb.shape or len(xa) == 0:
        raise ValueError("paired bootstrap needs two equal-length, non-empty samples")
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(xa), size=(n, len(xa)))
    d = (xb[idx] - xa[idx]).mean(axis=1)
    lo, hi = np.percentile(d, [2.5, 97.5])
    return {"delta": float(xb.mean() - xa.mean()), "ci95": [float(lo), float(hi)], "n": int(len(xa)),
            "excludes_zero": bool(lo > 0 or hi < 0)}


# ------------------------------------------------------------------ calibration


def auroc(labels, scores) -> float | None:
    """Probability that a random positive scores above a random negative (ties count half)."""
    y = np.asarray(labels, dtype=bool)
    s = np.asarray(scores, dtype=float)
    pos, neg = s[y], s[~y]
    if len(pos) == 0 or len(neg) == 0:
        return None
    gt = (pos[:, None] > neg[None, :]).sum()
    eq = (pos[:, None] == neg[None, :]).sum()
    return float((gt + 0.5 * eq) / (len(pos) * len(neg)))


def brier(labels, probs) -> float | None:
    y = np.asarray(labels, dtype=float)
    p = np.asarray(probs, dtype=float)
    return float(((p - y) ** 2).mean()) if len(y) else None


def ece(labels, probs, bins: int = 5) -> float | None:
    """Expected calibration error over equal-width probability bins."""
    y = np.asarray(labels, dtype=float)
    p = np.asarray(probs, dtype=float)
    if not len(y):
        return None
    edges = np.linspace(0, 1, bins + 1)
    idx = np.clip(np.digitize(p, edges[1:-1]), 0, bins - 1)
    err = 0.0
    for b in range(bins):
        m = idx == b
        if m.any():
            err += m.mean() * abs(p[m].mean() - y[m].mean())
    return float(err)


def mean(xs) -> float | None:
    xs = list(xs)
    return float(sum(xs) / len(xs)) if xs else None


def median(xs) -> float | None:
    xs = list(xs)
    return float(np.median(xs)) if xs else None
