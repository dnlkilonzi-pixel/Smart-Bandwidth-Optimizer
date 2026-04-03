"""
License key parsing and feature-gating for BandwidthOS Pro / Enterprise.

Format
------
A license key is a two-part string::

    <base64url-encoded JSON payload>.<HMAC-SHA256 hex digest>

The JSON payload contains::

    {
        "tier":     "pro",                           # "community" | "pro" | "enterprise"
        "features": ["pvm", "sla", "multi_node"],    # enabled feature names
        "customer": "acme-corp",                     # optional identifier
        "issued":   "2026-01-01",                    # ISO date string
        "expires":  "2027-12-31"                     # ISO date string or null
    }

The HMAC is computed as HMAC-SHA256(secret, base64url_payload) where the
secret is either provided explicitly, read from the ``BANDWIDTHOS_LICENSE_SECRET``
environment variable, or falls back to the built-in development key
``bandwidthos-dev-key``.

The dev key is intentionally public — it lets users evaluate all features
locally without contacting a licensing server.  Production keys are signed
with a private secret known only to the BandwidthOS licensing backend.

Usage
-----
::

    # Verify and parse
    key = parse_license_key("eyJ0aWVyIjoi....<hmac>")
    key.require_feature("pvm")   # raises LicenseError if not licensed

    # Generate a trial key (for testing / dev)
    raw_key = LicenseKey.generate_trial(features=["pvm", "sla"])
    key = parse_license_key(raw_key)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from dataclasses import dataclass, field
from datetime import date
from typing import List, Optional


# Built-in development/evaluation key – intentionally public.
_DEV_SECRET = "bandwidthos-dev-key"

# Features that exist; used for validation.
KNOWN_FEATURES = {"pvm", "sla", "multi_node"}
TIER_FEATURES = {
    "community": [],
    "pro":       ["pvm", "sla", "multi_node"],
    "enterprise": ["pvm", "sla", "multi_node"],
}


# ─────────────────────────── exceptions ──────────────────────────────────────

class LicenseError(ValueError):
    """Raised when a license key is missing, invalid, expired, or insufficient."""


# ─────────────────────────── LicenseKey ──────────────────────────────────────

@dataclass
class LicenseKey:
    """
    Parsed and verified license key.

    Attributes
    ----------
    tier:
        License tier: ``"community"``, ``"pro"``, or ``"enterprise"``.
    features:
        Explicit list of enabled feature names.
    customer:
        Optional customer / organisation identifier.
    issued:
        ISO date string (e.g. ``"2026-01-01"``).
    expires:
        ISO date string or ``None`` for perpetual licenses.
    raw_payload:
        The original base64-encoded JSON string (for logging / audit).
    """

    tier: str = "community"
    features: List[str] = field(default_factory=list)
    customer: str = ""
    issued: str = ""
    expires: Optional[str] = None
    raw_payload: str = ""

    # ── feature checks ────────────────────────────────────────────────────

    def has_feature(self, feature: str) -> bool:
        """Return ``True`` if *feature* is enabled by this license."""
        return feature in self.features

    def require_feature(self, feature: str) -> None:
        """
        Assert that *feature* is enabled.

        :raises LicenseError: If the feature is not included in this key.
        """
        if not self.has_feature(feature):
            raise LicenseError(
                f"Feature {feature!r} is not enabled by this license "
                f"(tier={self.tier!r}).  Upgrade to Pro or Enterprise."
            )

    def is_expired(self) -> bool:
        """Return ``True`` if the license expiry date has passed."""
        if not self.expires:
            return False
        try:
            exp = date.fromisoformat(self.expires)
            return date.today() > exp
        except ValueError:
            return False

    def require_valid(self) -> None:
        """
        Assert that the license has not expired.

        :raises LicenseError: If ``is_expired()`` is ``True``.
        """
        if self.is_expired():
            raise LicenseError(
                f"License expired on {self.expires}.  "
                "Please renew at https://bandwidthos.io/renew"
            )

    def to_dict(self) -> dict:
        return {
            "tier": self.tier,
            "features": list(self.features),
            "customer": self.customer,
            "issued": self.issued,
            "expires": self.expires,
            "expired": self.is_expired(),
        }

    # ── key generation (for dev / trial) ─────────────────────────────────

    @classmethod
    def generate_trial(
        cls,
        features: Optional[List[str]] = None,
        tier: str = "pro",
        customer: str = "trial",
        expires: Optional[str] = None,
        secret: Optional[str] = None,
    ) -> str:
        """
        Generate a signed trial license key string.

        Parameters
        ----------
        features:
            Features to enable.  Defaults to all features for *tier*.
        tier:
            License tier (``"community"``, ``"pro"``, ``"enterprise"``).
        customer:
            Customer identifier embedded in the payload.
        expires:
            Expiry date as ``"YYYY-MM-DD"``, or ``None`` for perpetual.
        secret:
            Signing secret.  Uses the dev secret if not provided.

        :returns: A ``"<b64_payload>.<hmac>"`` string ready to pass as
                  ``--license-key``.
        """
        today = date.today().isoformat()
        payload_dict = {
            "tier": tier,
            "features": features if features is not None else TIER_FEATURES.get(tier, []),
            "customer": customer,
            "issued": today,
            "expires": expires,
        }
        payload_bytes = json.dumps(payload_dict, separators=(",", ":")).encode()
        b64 = base64.urlsafe_b64encode(payload_bytes).rstrip(b"=").decode()
        sig = _sign(b64, secret or _DEV_SECRET)
        return f"{b64}.{sig}"


# ─────────────────────────── public helpers ───────────────────────────────────

def _sign(b64_payload: str, secret: str) -> str:
    """Return HMAC-SHA256 hex digest of *b64_payload* using *secret*."""
    return hmac.new(
        secret.encode(),
        b64_payload.encode(),
        hashlib.sha256,
    ).hexdigest()


def _get_secret() -> str:
    """Return the active signing secret (env var > dev key)."""
    return os.environ.get("BANDWIDTHOS_LICENSE_SECRET", _DEV_SECRET)


def parse_license_key(
    key_string: str,
    secret: Optional[str] = None,
) -> LicenseKey:
    """
    Parse and verify a license key string.

    Parameters
    ----------
    key_string:
        The ``"<b64_payload>.<hmac>"`` key string.
    secret:
        Signing secret to verify against.  If ``None``, uses the
        ``BANDWIDTHOS_LICENSE_SECRET`` environment variable, falling back
        to the built-in development key.

    :returns: A verified :class:`LicenseKey`.
    :raises LicenseError: If the key is malformed, has an invalid signature,
                           or contains unknown fields.
    """
    key_string = key_string.strip()
    if not key_string:
        raise LicenseError("License key is empty.")

    parts = key_string.rsplit(".", 1)
    if len(parts) != 2:
        raise LicenseError(
            "Invalid license key format.  Expected '<payload>.<signature>'."
        )

    b64_payload, provided_sig = parts
    effective_secret = secret if secret is not None else _get_secret()
    expected_sig = _sign(b64_payload, effective_secret)

    if not hmac.compare_digest(provided_sig.lower(), expected_sig.lower()):
        raise LicenseError(
            "License key signature is invalid.  "
            "The key may be tampered with or signed with a different secret."
        )

    # Decode payload
    try:
        padding = "=" * (4 - len(b64_payload) % 4)
        payload_bytes = base64.urlsafe_b64decode(b64_payload + padding)
        payload = json.loads(payload_bytes)
    except Exception as exc:
        raise LicenseError(f"License key payload is malformed: {exc}") from exc

    tier = str(payload.get("tier", "community"))
    raw_features = payload.get("features", [])
    if not isinstance(raw_features, list):
        raise LicenseError("License key 'features' must be a list.")
    features = [str(f) for f in raw_features]

    return LicenseKey(
        tier=tier,
        features=features,
        customer=str(payload.get("customer", "")),
        issued=str(payload.get("issued", "")),
        expires=payload.get("expires") or None,
        raw_payload=b64_payload,
    )
