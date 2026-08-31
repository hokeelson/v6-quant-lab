from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

from src.external_intelligence import _headline_metrics


def _item(title, source, hours_old):
    return {
        "title": title,
        "source": source,
        "published_at": format_datetime(datetime.now(timezone.utc) - timedelta(hours=hours_old)),
    }


def test_duplicate_headlines_are_counted_once():
    item = _item("Fed hawkish as inflation fear rises", "Reuters", 1)
    out = _headline_metrics([item, dict(item), dict(item)])
    assert out["raw_headline_count"] == 3
    assert out["headline_count"] == 1
    assert out["duplicate_count"] == 2


def test_old_headline_has_less_effective_weight_than_recent():
    recent = _headline_metrics([_item("Crypto hack sparks liquidation fear", "Source A", 1)])
    old = _headline_metrics([_item("Crypto hack sparks liquidation fear", "Source A", 46)])
    assert recent["effective_headline_count"] > old["effective_headline_count"]


def test_source_diversity_improves_evidence_quality():
    same_source = [_item(f"Market rally item {i}", "Source A", i) for i in range(6)]
    diverse = [_item(f"Market rally item {i}", f"Source {i}", i) for i in range(6)]
    one = _headline_metrics(same_source)
    many = _headline_metrics(diverse)
    assert many["source_diversity"] > one["source_diversity"]
    assert many["evidence_quality"] > one["evidence_quality"]


def test_legacy_string_headlines_remain_supported():
    out = _headline_metrics(["Fed hawkish as inflation fear rises", "Stock market rally on rate cut optimism"])
    assert out["headline_count"] == 2
    assert -1.0 <= out["sentiment"] <= 1.0
