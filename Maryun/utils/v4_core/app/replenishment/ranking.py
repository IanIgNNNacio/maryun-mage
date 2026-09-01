"""Ranking score for candidate sourcing moves.

The engine iterates needs in priority order; within each layer it may find
several origins that could satisfy the same need. Ranking decides which
origin wins.

Score is on [0, 1]; higher is better. Weighted sum of three normalised
sub-scores:

* ``w_distance``      — closer is better (1 − km / km_max)
* ``w_cost``          — cheaper is better (1 − cost / cost_max)
* ``w_origin_impact`` — less disruptive at the origin is better
                        (1 − share_of_origin_stock)

All three inputs are already known per-move; normalisation is done relative
to a pool of candidates so the top choice always scores 1.0 on whichever
dimension it dominates.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.config.engine_params import RankingWeights


@dataclass(frozen=True)
class Candidate:
    origen: str
    destino: str
    km: float
    costo: float           # CLP per unit
    share_origin: float    # fraction of origin stock this move consumes (0..1)
    disponible: float      # max units available at origin
    extra: dict | None = None


def _normalise_lower_is_better(v: float, ref_max: float) -> float:
    if ref_max <= 0 or not isinstance(v, (int, float)):
        return 0.0
    if v == float("inf"):
        return 0.0
    return max(0.0, 1.0 - (v / ref_max))


def score_candidate(c: Candidate, weights: RankingWeights, *, km_max: float, cost_max: float) -> float:
    s_dist = _normalise_lower_is_better(c.km, km_max)
    s_cost = _normalise_lower_is_better(c.costo, cost_max)
    s_impact = max(0.0, 1.0 - max(0.0, min(1.0, c.share_origin)))
    return (
        weights.w_distance * s_dist
        + weights.w_cost * s_cost
        + weights.w_origin_impact * s_impact
    )


def rank_candidates(
    candidates: list[Candidate],
    weights: RankingWeights,
) -> list[tuple[Candidate, float]]:
    """Return ``[(candidate, score)]`` sorted by score desc."""
    if not candidates:
        return []
    km_max = max((c.km for c in candidates if c.km != float("inf")), default=1.0) or 1.0
    cost_max = max((c.costo for c in candidates if c.costo != float("inf")), default=1.0) or 1.0
    scored = [(c, score_candidate(c, weights, km_max=km_max, cost_max=cost_max)) for c in candidates]
    scored.sort(key=lambda t: (-t[1], t[0].km, t[0].costo, t[0].origen))
    return scored
