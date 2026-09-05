from src.risk_sizing import _risk_account_id


def test_crypto_lite_leverage_guard_uses_shared_master_account(monkeypatch):
    monkeypatch.setenv("V6_SINGLE_CRYPTO_ACCOUNT", "1")
    assert _risk_account_id("crypto", "short") == "crypto"
    assert _risk_account_id("crypto", "medium") == "crypto"
    assert _risk_account_id("crypto", "long") == "crypto"


def test_legacy_and_noncrypto_account_mapping_is_unchanged(monkeypatch):
    monkeypatch.delenv("V6_SINGLE_CRYPTO_ACCOUNT", raising=False)
    assert _risk_account_id("crypto", "short") == "crypto_short"
    assert _risk_account_id("stock", "medium") == "stock_medium"
