from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_local_runtime_health_ignores_cloud_only_persistence_checks():
    text = (ROOT / "runtime_health_exporter.py").read_text(encoding="utf-8")
    assert 'V6_LOCAL_MODE' in text
    assert '"status": "LOCAL_DIRECT"' in text
    assert '"backup_mode"] = "NOT_REQUIRED_LOCAL"' in text
    assert '"LOCAL_WINDOWS_DIRECT"' in text
