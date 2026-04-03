"""
Tests for bandwidth_optimizer.trust (Secure Multi-node Trust Layer)
"""

import json

import pytest

from bandwidth_optimizer.trust import (
    SIGNATURE_HEADER,
    sign_payload,
    verify_payload,
)


# ── sign_payload ──────────────────────────────────────────────────────────────

class TestSignPayload:
    def test_returns_hex_string(self):
        sig = sign_payload("secret", b"hello")
        assert isinstance(sig, str)
        assert all(c in "0123456789abcdef" for c in sig)

    def test_64_char_sha256_hex(self):
        sig = sign_payload("secret", b"hello")
        assert len(sig) == 64

    def test_same_inputs_same_output(self):
        s1 = sign_payload("key", b"data")
        s2 = sign_payload("key", b"data")
        assert s1 == s2

    def test_different_secret_different_sig(self):
        s1 = sign_payload("secret1", b"data")
        s2 = sign_payload("secret2", b"data")
        assert s1 != s2

    def test_different_payload_different_sig(self):
        s1 = sign_payload("secret", b"payload1")
        s2 = sign_payload("secret", b"payload2")
        assert s1 != s2

    def test_empty_payload(self):
        sig = sign_payload("secret", b"")
        assert len(sig) == 64

    def test_unicode_secret(self):
        sig = sign_payload("sécret-clé", "données".encode("utf-8"))
        assert len(sig) == 64

    def test_large_payload(self):
        sig = sign_payload("key", b"X" * 100_000)
        assert len(sig) == 64


# ── verify_payload ────────────────────────────────────────────────────────────

class TestVerifyPayload:
    def test_valid_signature(self):
        payload = b'{"node_id": "edge-01"}'
        sig = sign_payload("my-secret", payload)
        assert verify_payload("my-secret", payload, sig) is True

    def test_wrong_secret_fails(self):
        payload = b"data"
        sig = sign_payload("correct", payload)
        assert verify_payload("wrong", payload, sig) is False

    def test_tampered_payload_fails(self):
        payload = b"original"
        sig = sign_payload("secret", payload)
        assert verify_payload("secret", b"tampered", sig) is False

    def test_tampered_signature_fails(self):
        payload = b"data"
        sig = sign_payload("secret", payload)
        bad_sig = sig[:-4] + "0000"
        assert verify_payload("secret", payload, bad_sig) is False

    def test_empty_signature_fails(self):
        assert verify_payload("secret", b"data", "") is False

    def test_case_insensitive_hex(self):
        payload = b"data"
        sig_lower = sign_payload("secret", payload)
        sig_upper = sig_lower.upper()
        assert verify_payload("secret", payload, sig_upper) is True

    def test_non_string_signature_returns_false(self):
        assert verify_payload("secret", b"data", None) is False  # type: ignore

    def test_json_payload_round_trip(self):
        secret = "shared-secret-key-1234"
        data = {"node_id": "edge-01", "packets": 9999}
        payload = json.dumps(data).encode("utf-8")
        sig = sign_payload(secret, payload)
        assert verify_payload(secret, payload, sig) is True


# ── signature header constant ─────────────────────────────────────────────────

class TestSignatureHeader:
    def test_header_name_defined(self):
        assert SIGNATURE_HEADER == "X-Agent-Signature"


# ── integration: agent-coordinator auth flow ──────────────────────────────────

class TestAgentCoordinatorAuthIntegration:
    def test_unsigned_agent_rejected_when_auth_required(self):
        from bandwidth_optimizer.coordinator import AgentCoordinator

        coord = AgentCoordinator(require_auth=True, auth_secret="secret")
        result = coord.ingest_authenticated(
            "node-01", b'{"node_id": "node-01"}', "", {"packets_received": 1}
        )
        assert result is False
        assert coord.agent_count() == 0

    def test_signed_agent_accepted(self):
        from bandwidth_optimizer.coordinator import AgentCoordinator

        secret = "shared-secret"
        coord = AgentCoordinator(require_auth=True, auth_secret=secret)
        payload = b'{"node_id": "node-01"}'
        sig = sign_payload(secret, payload)
        result = coord.ingest_authenticated(
            "node-01", payload, sig, {"packets_received": 1}
        )
        assert result is True
        assert coord.agent_count() == 1

    def test_wrong_secret_rejected(self):
        from bandwidth_optimizer.coordinator import AgentCoordinator

        coord = AgentCoordinator(require_auth=True, auth_secret="correct")
        payload = b'{"node_id": "n1"}'
        sig = sign_payload("wrong", payload)
        result = coord.ingest_authenticated("n1", payload, sig, {})
        assert result is False

    def test_auth_not_required_accepts_any(self):
        from bandwidth_optimizer.coordinator import AgentCoordinator

        coord = AgentCoordinator(require_auth=False)
        # No signature provided – should still work
        result = coord.ingest_authenticated("n1", b"data", "", {"v": 1})
        assert result is True
        assert coord.agent_count() == 1

    def test_require_auth_property(self):
        from bandwidth_optimizer.coordinator import AgentCoordinator

        coord_no_auth = AgentCoordinator(require_auth=False)
        coord_auth = AgentCoordinator(require_auth=True, auth_secret="k")
        assert coord_no_auth.require_auth is False
        assert coord_auth.require_auth is True


# ── API endpoint signature enforcement ───────────────────────────────────────

class TestAPISignatureEnforcement:
    def test_unauthenticated_post_rejected(self):
        from fastapi.testclient import TestClient
        from bandwidth_optimizer import BandwidthOptimizer
        from bandwidth_optimizer.coordinator import AgentCoordinator
        from api.server import create_app

        coord = AgentCoordinator(require_auth=True, auth_secret="secret")
        app = create_app(optimizer=BandwidthOptimizer(), coordinator=coord)
        client = TestClient(app)

        resp = client.post("/agent/node-01/stats",
                           json={"node_id": "node-01"})
        assert resp.status_code == 401

    def test_authenticated_post_accepted(self):
        from fastapi.testclient import TestClient
        from bandwidth_optimizer import BandwidthOptimizer
        from bandwidth_optimizer.coordinator import AgentCoordinator
        from api.server import create_app

        secret = "my-secret-key"
        coord = AgentCoordinator(require_auth=True, auth_secret=secret)
        app = create_app(optimizer=BandwidthOptimizer(), coordinator=coord)
        client = TestClient(app)

        payload_data = {"node_id": "node-01", "packets_received": 5}
        payload_bytes = json.dumps(payload_data).encode("utf-8")
        sig = sign_payload(secret, payload_bytes)

        resp = client.post(
            "/agent/node-01/stats",
            content=payload_bytes,
            headers={
                "Content-Type": "application/json",
                SIGNATURE_HEADER: sig,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_unauthenticated_coordinator_accepts_all(self):
        from fastapi.testclient import TestClient
        from bandwidth_optimizer import BandwidthOptimizer
        from bandwidth_optimizer.coordinator import AgentCoordinator
        from api.server import create_app

        coord = AgentCoordinator(require_auth=False)
        app = create_app(optimizer=BandwidthOptimizer(), coordinator=coord)
        client = TestClient(app)

        resp = client.post("/agent/node-01/stats",
                           json={"node_id": "node-01"})
        assert resp.status_code == 200
