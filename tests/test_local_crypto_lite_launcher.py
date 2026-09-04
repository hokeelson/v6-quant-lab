from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_local_launcher_is_crypto_lite_only():
    bat = (ROOT / "start_v6_auto.bat").read_text(encoding="utf-8")
    assert "V6_SINGLE_CRYPTO_ACCOUNT=1" in bat
    assert "data_crypto_lite" in bat
    assert "local_crypto_lite.py" in bat
    assert "trial_ledger_worker.py" not in bat
    assert "crypto_v2_shadow_worker.py" not in bat


def test_local_orchestrator_uses_current_stack_only():
    text = (ROOT / "local_crypto_lite.py").read_text(encoding="utf-8")
    for required in (
        "worker_supervisor_v8.py",
        "realtime_supervisor.py",
        "tca_supervisor.py",
        "direction_shadow_supervisor.py",
        "external_intelligence_worker.py",
        "runtime_health_exporter.py",
        "policy_epoch_exporter.py",
        "dashboard_v9.py",
    ):
        assert required in text

    for retired in (
        "trial_ledger_worker.py",
        "crypto_v2_shadow_worker.py",
        "crypto_v2_shadow_supervisor.py",
        "storage_rescue.py",
    ):
        assert retired not in text

    assert 'V6_ALLOW_PAPER_ORDERS"] = "false"' in text
