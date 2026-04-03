"""
Tests for bandwidth_optimizer.license (LicenseKey + feature gating)
"""

import pytest

from bandwidth_optimizer.license import (
    LicenseError,
    LicenseKey,
    KNOWN_FEATURES,
    TIER_FEATURES,
    parse_license_key,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _trial(features=None, tier="pro", expires=None, secret=None):
    """Generate a signed trial key and parse it back."""
    raw = LicenseKey.generate_trial(
        features=features, tier=tier, expires=expires, secret=secret
    )
    return parse_license_key(raw, secret=secret)


# ─────────────────────────── key generation ──────────────────────────────────

class TestGenerateTrial:
    def test_returns_string_with_dot(self):
        raw = LicenseKey.generate_trial()
        assert "." in raw

    def test_roundtrip_parses_without_error(self):
        raw = LicenseKey.generate_trial()
        key = parse_license_key(raw)
        assert isinstance(key, LicenseKey)

    def test_tier_preserved(self):
        key = _trial(tier="enterprise")
        assert key.tier == "enterprise"

    def test_features_preserved(self):
        key = _trial(features=["pvm", "sla"])
        assert "pvm" in key.features
        assert "sla" in key.features

    def test_customer_preserved(self):
        raw = LicenseKey.generate_trial(customer="acme-corp")
        key = parse_license_key(raw)
        assert key.customer == "acme-corp"

    def test_expires_preserved(self):
        raw = LicenseKey.generate_trial(expires="2099-12-31")
        key = parse_license_key(raw)
        assert key.expires == "2099-12-31"

    def test_custom_secret(self):
        raw = LicenseKey.generate_trial(secret="my-secret")
        # should verify with same secret
        key = parse_license_key(raw, secret="my-secret")
        assert key.tier == "pro"

    def test_custom_secret_fails_with_wrong_secret(self):
        raw = LicenseKey.generate_trial(secret="correct")
        with pytest.raises(LicenseError, match="signature"):
            parse_license_key(raw, secret="wrong")

    def test_pro_tier_defaults_all_features(self):
        key = _trial(tier="pro")
        assert set(TIER_FEATURES["pro"]).issubset(set(key.features))


# ─────────────────────────── parse_license_key ───────────────────────────────

class TestParseLicenseKey:
    def test_empty_string_raises(self):
        with pytest.raises(LicenseError, match="empty"):
            parse_license_key("")

    def test_whitespace_only_raises(self):
        with pytest.raises(LicenseError, match="empty"):
            parse_license_key("   ")

    def test_no_dot_raises(self):
        with pytest.raises(LicenseError, match="format"):
            parse_license_key("nodotinhere")

    def test_tampered_signature_raises(self):
        raw = LicenseKey.generate_trial()
        b64, sig = raw.rsplit(".", 1)
        tampered = f"{b64}.{'a' * len(sig)}"
        with pytest.raises(LicenseError, match="signature"):
            parse_license_key(tampered)

    def test_tampered_payload_raises(self):
        raw = LicenseKey.generate_trial()
        b64, sig = raw.rsplit(".", 1)
        # Flip a character in the payload
        flipped = b64[:-1] + ("A" if b64[-1] != "A" else "B")
        with pytest.raises(LicenseError, match="signature"):
            parse_license_key(f"{flipped}.{sig}")

    def test_malformed_json_payload_raises(self):
        import base64
        import hmac as _hmac
        import hashlib
        bad_payload = base64.urlsafe_b64encode(b"not json").rstrip(b"=").decode()
        sig = _hmac.new(
            b"bandwidthos-dev-key", bad_payload.encode(), hashlib.sha256
        ).hexdigest()
        with pytest.raises(LicenseError, match="malformed"):
            parse_license_key(f"{bad_payload}.{sig}")

    def test_raw_payload_stored(self):
        raw = LicenseKey.generate_trial()
        key = parse_license_key(raw)
        b64, _ = raw.rsplit(".", 1)
        assert key.raw_payload == b64


# ─────────────────────────── LicenseKey methods ──────────────────────────────

class TestLicenseKeyMethods:
    def test_has_feature_true(self):
        key = _trial(features=["pvm", "sla"])
        assert key.has_feature("pvm")
        assert key.has_feature("sla")

    def test_has_feature_false(self):
        key = _trial(features=["pvm"])
        assert not key.has_feature("multi_node")

    def test_require_feature_passes(self):
        key = _trial(features=["pvm"])
        key.require_feature("pvm")  # must not raise

    def test_require_feature_raises(self):
        key = _trial(features=["pvm"])
        with pytest.raises(LicenseError, match="multi_node"):
            key.require_feature("multi_node")

    def test_is_expired_false_for_future(self):
        key = _trial(expires="2099-12-31")
        assert not key.is_expired()

    def test_is_expired_true_for_past(self):
        key = _trial(expires="2000-01-01")
        assert key.is_expired()

    def test_is_expired_false_for_null(self):
        key = _trial(expires=None)
        assert not key.is_expired()

    def test_require_valid_passes_for_future(self):
        key = _trial(expires="2099-12-31")
        key.require_valid()  # must not raise

    def test_require_valid_raises_for_expired(self):
        key = _trial(expires="2000-01-01")
        with pytest.raises(LicenseError, match="expired"):
            key.require_valid()

    def test_to_dict_keys(self):
        key = _trial(tier="enterprise", features=["pvm"])
        d = key.to_dict()
        assert d["tier"] == "enterprise"
        assert "pvm" in d["features"]
        assert "expired" in d

    def test_community_tier_no_features(self):
        key = _trial(tier="community", features=[])
        assert not key.has_feature("pvm")
        assert not key.has_feature("sla")
