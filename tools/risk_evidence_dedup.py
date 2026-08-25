from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def patch(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if old not in text:
        if new in text:
            print(f"ALREADY_PATCHED {path.relative_to(ROOT)}")
            return
        raise SystemExit(f"Expected block not found in {path}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    print(f"PATCHED {path.relative_to(ROOT)}")


risk = ROOT / "src" / "risk_sizing.py"
patch(
    risk,
    '        "expected_live_evidence_weight": 0.0,\n        "meta_multiplier": 1.0,',
    '        "expected_live_evidence_weight": 0.0,\n        "realized_evidence_multiplier": 1.0,\n        "realized_evidence_dedup_applied": False,\n        "meta_multiplier": 1.0,',
)
patch(
    risk,
    '''        # Expected-vs-live is an additional generalization test: absolute realized\n        # health can be weak while the more important question is whether live\n        # behavior materially contradicts the model's own OOS expectation. It only\n        # activates after >=5 closed trades, so small-sample combinations remain 1x.\n        combined = (\n            result["portfolio_multiplier"]\n            * health_multiplier\n            * expected_live_multiplier\n            * meta_multiplier\n            * quality_multiplier\n        )\n''',
    '''        # Broad strategy health, symbol×strategy health, and expected-vs-live all\n        # reuse the same forward realized trades. Expected-vs-live adds an OOS\n        # reference, but multiplying these penalties would count the realized loss\n        # evidence more than once. Keep all diagnostics, but size from the strictest\n        # realized-evidence view only. Independent portfolio, Meta, and data/drift\n        # layers remain multiplicative.\n        realized_evidence_multiplier = min(health_multiplier, expected_live_multiplier)\n        result["realized_evidence_multiplier"] = realized_evidence_multiplier\n        result["realized_evidence_dedup_applied"] = (\n            health_multiplier < 0.999999 and expected_live_multiplier < 0.999999\n        )\n        combined = (\n            result["portfolio_multiplier"]\n            * realized_evidence_multiplier\n            * meta_multiplier\n            * quality_multiplier\n        )\n''',
)

exporter = ROOT / "storage_status_exporter.py"
patch(
    exporter,
    '            "expected_live_evidence_weight": _finite(payload.get("expected_live_evidence_weight")),\n            "meta_multiplier": _mult(payload, "meta_multiplier"),',
    '            "expected_live_evidence_weight": _finite(payload.get("expected_live_evidence_weight")),\n            "realized_evidence_multiplier": _mult(payload, "realized_evidence_multiplier"),\n            "realized_evidence_dedup_applied": bool(payload.get("realized_evidence_dedup_applied", False)),\n            "meta_multiplier": _mult(payload, "meta_multiplier"),',
)
patch(
    exporter,
    '            "expected_live_reduced": reduced("expected_live_multiplier"),\n            "meta_reduced": reduced("meta_multiplier"),',
    '            "expected_live_reduced": reduced("expected_live_multiplier"),\n            "realized_evidence_reduced": reduced("realized_evidence_multiplier"),\n            "realized_evidence_dedup_applied": sum(1 for x in entries if x.get("realized_evidence_dedup_applied")),\n            "meta_reduced": reduced("meta_multiplier"),',
)

text = risk.read_text(encoding="utf-8")
if '* health_multiplier\n            * expected_live_multiplier' in text:
    raise SystemExit("Old double-penalty multiplication still present")
if 'realized_evidence_multiplier = min(health_multiplier, expected_live_multiplier)' not in text:
    raise SystemExit("Dedup formula missing")
print("RISK_EVIDENCE_DEDUP OK")
