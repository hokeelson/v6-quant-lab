from __future__ import annotations
import ast
import numpy as np
import pandas as pd

def _distance(a: dict, b: dict) -> float:
    keys = sorted(set(a) & set(b))
    if not keys:
        return np.inf
    d = []
    for k in keys:
        av, bv = a[k], b[k]
        if isinstance(av, (int,float)) and isinstance(bv, (int,float)):
            scale = max(abs(float(av)), abs(float(bv)), 1e-9)
            d.append(abs(float(av)-float(bv))/scale)
        else:
            d.append(0 if av == bv else 1)
    return float(np.mean(d))

def parameter_neighborhood_stability(ranking: pd.DataFrame, top_index: int = 0, radius: float = 0.30) -> dict:
    if ranking is None or ranking.empty or "params" not in ranking:
        return {}
    center = ranking.iloc[top_index]["params"]
    if isinstance(center, str):
        center = ast.literal_eval(center)
    distances = ranking["params"].apply(lambda p: _distance(center, ast.literal_eval(p) if isinstance(p,str) else p))
    neigh = ranking[distances <= radius].copy()
    if len(neigh) < 2:
        return {"neighbors": int(len(neigh)), "radius": radius, "stability_score": 0.0}
    scores = neigh["score"].replace([np.inf,-np.inf], np.nan).dropna()
    sharpes = neigh["sharpe"].replace([np.inf,-np.inf], np.nan).dropna()
    if len(scores) == 0:
        return {"neighbors": int(len(neigh)), "radius": radius, "stability_score": 0.0}
    center_score = float(ranking.iloc[top_index]["score"])
    med = float(scores.median())
    dispersion = float(scores.std(ddof=0))
    ratio = med / max(center_score, 1e-9)
    stability = np.clip(70*ratio + 30*(1-min(dispersion/30,1)), 0, 100)
    return {
        "neighbors": int(len(neigh)),
        "radius": float(radius),
        "center_score": center_score,
        "neighbor_median_score": med,
        "neighbor_score_std": dispersion,
        "neighbor_median_sharpe": float(sharpes.median()) if len(sharpes) else np.nan,
        "stability_score": float(stability),
    }

def final_research_grade(
    oos_score: float | None,
    generalization_score: float | None,
    pbo: float | None,
    dsr_probability: float | None,
    parameter_stability: float | None,
    stress_survival: float | None,
    bootstrap_loss_probability: float | None,
) -> dict:
    """
    Conservative composite grade. Missing evidence is not silently treated as good.
    """
    evidence = {
        "oos": oos_score,
        "generalization": generalization_score,
        "pbo_component": None if pbo is None or not np.isfinite(pbo) else (1-pbo)*100,
        "dsr": None if dsr_probability is None or not np.isfinite(dsr_probability) else dsr_probability*100,
        "stability": parameter_stability,
        "stress": stress_survival,
        "bootstrap": None if bootstrap_loss_probability is None or not np.isfinite(bootstrap_loss_probability) else (1-bootstrap_loss_probability)*100,
    }
    weights = {
        "oos": .20, "generalization": .20, "pbo_component": .15, "dsr": .15,
        "stability": .10, "stress": .10, "bootstrap": .10,
    }
    usable = {k:v for k,v in evidence.items() if v is not None and np.isfinite(v)}
    if not usable:
        return {"grade": 0.0, "evidence_coverage": 0.0, "components": evidence}
    used_weight = sum(weights[k] for k in usable)
    raw = sum(weights[k]*float(v) for k,v in usable.items()) / used_weight
    coverage = used_weight / sum(weights.values())
    # Missing tests lower final confidence instead of inflating score.
    grade = raw * (0.65 + 0.35*coverage)
    return {"grade": float(np.clip(grade,0,100)), "evidence_coverage": float(coverage), "components": evidence}
