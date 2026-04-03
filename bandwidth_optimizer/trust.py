"""
Secure multi-node trust layer.

Provides HMAC-SHA256 based authentication for agent heartbeats, preventing
unauthenticated nodes from injecting stats into the coordinator.

How it works
------------
1. Both the ``NodeAgent`` and the ``AgentCoordinator`` share a **secret key**.
2. Before sending a heartbeat, the agent *signs* the JSON payload::

       X-Agent-Signature: <hmac_sha256_hex(secret, payload_bytes)>

3. The coordinator *verifies* the signature before ingesting stats.
   Requests with a missing or invalid signature are rejected (401).

The signing function uses ``hmac.new(secret, payload, sha256).hexdigest()``.
No external dependencies are required (stdlib ``hmac`` + ``hashlib``).

Usage::

    from bandwidth_optimizer.trust import sign_payload, verify_payload

    sig = sign_payload("my-shared-secret", b'{"node_id": "edge-01"}')
    ok  = verify_payload("my-shared-secret", b'{"node_id": "edge-01"}', sig)
    assert ok

Integration::

    # Agent side (via AgentConfig.auth_secret):
    cfg = AgentConfig(node_id="edge-01", auth_secret="my-shared-secret",
                      coordinator_url="http://coordinator:8000")

    # Coordinator side (via AgentCoordinator):
    coord = AgentCoordinator(require_auth=True, auth_secret="my-shared-secret")
"""

from __future__ import annotations

import hashlib
import hmac


# ─────────────────────────── core primitives ─────────────────────────────────

def sign_payload(secret: str, payload: bytes) -> str:
    """
    Sign *payload* with *secret* using HMAC-SHA256.

    :param secret: Shared secret string (UTF-8 encoded internally).
    :param payload: Raw bytes to sign.
    :returns: Lowercase hexadecimal HMAC-SHA256 digest.
    """
    return hmac.new(
        secret.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()


def verify_payload(secret: str, payload: bytes, signature: str) -> bool:
    """
    Verify that *signature* is the correct HMAC-SHA256 of *payload*.

    Uses ``hmac.compare_digest`` to prevent timing-based side-channel attacks.

    :param secret: Shared secret string.
    :param payload: The raw bytes that were signed.
    :param signature: Expected hexadecimal digest.
    :returns: ``True`` if valid, ``False`` otherwise.
    """
    expected = sign_payload(secret, payload)
    try:
        return hmac.compare_digest(expected, signature.lower())
    except (TypeError, AttributeError):
        return False


# ─────────────────────────── header name ─────────────────────────────────────

#: HTTP header used to carry the HMAC signature.
SIGNATURE_HEADER = "X-Agent-Signature"
