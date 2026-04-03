"""
Network Policy DSL (Domain-Specific Language).

Provides a YAML-based policy language for defining traffic classification
rules without editing Python code.  The policy format is designed to be
readable by network engineers while remaining expressive.

Policy file format (YAML)
--------------------------

.. code-block:: yaml

    version: "1"

    defaults:
      priority: MEDIUM          # fallback priority for unmatched traffic
      bandwidth_budget:
        CRITICAL:   0.30
        HIGH:       0.30
        MEDIUM:     0.20
        LOW:        0.10
        BACKGROUND: 0.05

    rules:
      - name: voip_sip
        description: "SIP signalling – highest priority"
        match:
          ports: [5060, 5061]
          protocols: [udp, tcp]
        priority: CRITICAL

      - name: zoom_video
        description: "Zoom video – critical"
        match:
          ports: [8801, 8802]
          protocols: [udp]
          payload_pattern: "ZRTP"
        priority: CRITICAL
        bandwidth_min_pct: 30

      - name: bittorrent_throttle
        description: "BitTorrent bulk transfer"
        match:
          ports: [6881, 6882, 6883]
          protocols: [tcp, udp]
        priority: BACKGROUND

Fields
------
``name`` (required)
    Unique rule identifier used in log messages.
``match`` (required)
    One or more match criteria (all are AND-combined):

    * ``ports`` – list of port numbers (matches source OR destination port).
    * ``protocols`` – list of protocol names (``tcp``, ``udp``, ``icmp``, …).
    * ``payload_pattern`` – regular expression matched against the first
      64 bytes of the application payload (ASCII/latin-1).

``priority`` (required)
    One of ``CRITICAL``, ``HIGH``, ``MEDIUM``, ``LOW``, ``BACKGROUND``.
``bandwidth_min_pct`` (optional, 0–100)
    Hint: minimum guaranteed bandwidth percentage for this class.
    Stored in the rule but does not currently override ``OptimizerConfig``
    directly; callers may read it to configure token-bucket rates.
``description`` (optional)
    Human-readable documentation string; ignored during processing.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import yaml

from .classifier import ClassificationRule
from .config import DEFAULT_BANDWIDTH_BUDGET, TrafficPriority


# ─────────────────────────── policy models ───────────────────────────────────

@dataclass
class PolicyRule:
    """
    A single rule as parsed from the YAML policy file.

    This is the "rich" representation that preserves all metadata.
    Call :meth:`to_classification_rule` to convert it for use with
    ``TrafficClassifier``.
    """
    name: str
    priority: TrafficPriority
    ports: Tuple[int, ...] = field(default_factory=tuple)
    protocols: Tuple[str, ...] = field(default_factory=tuple)
    payload_pattern: Optional[str] = None
    bandwidth_min_pct: float = 0.0
    description: str = ""
    # Continuous business-value weight for PVM scheduling (higher = more valuable).
    # Default 1.0 means no value uplift; set to e.g. 50.0 for high-revenue tenants.
    value_coefficient: float = 1.0

    def to_classification_rule(self) -> ClassificationRule:
        """Convert to a ``ClassificationRule`` for use with ``TrafficClassifier``."""
        return ClassificationRule(
            priority=self.priority,
            ports=self.ports,
            protocols=self.protocols,
            payload_pattern=self.payload_pattern,
        )


@dataclass
class Policy:
    """
    A complete loaded policy.

    Attributes
    ----------
    rules:
        Ordered list of ``PolicyRule`` objects (first match wins).
    default_priority:
        Priority assigned to packets that match no rule.
    bandwidth_budget:
        Per-priority bandwidth budget fractions (derived from ``defaults``
        section of the YAML; falls back to ``DEFAULT_BANDWIDTH_BUDGET``).
    """
    rules: List[PolicyRule] = field(default_factory=list)
    default_priority: TrafficPriority = TrafficPriority.MEDIUM
    bandwidth_budget: Dict[TrafficPriority, float] = field(
        default_factory=lambda: dict(DEFAULT_BANDWIDTH_BUDGET)
    )

    def to_classification_rules(self) -> List[ClassificationRule]:
        """Return the policy as an ordered list of ``ClassificationRule`` objects."""
        return [r.to_classification_rule() for r in self.rules]


# ─────────────────────────── loader ──────────────────────────────────────────

class PolicyLoader:
    """
    Parse YAML policy files into ``Policy`` objects.

    Usage::

        policy = PolicyLoader.load_file("policy.yaml")
        classifier = TrafficClassifier(rules=policy.to_classification_rules(),
                                        default_priority=policy.default_priority)

    Raises
    ------
    PolicyLoadError
        If the YAML is malformed or contains invalid values.
    """

    @staticmethod
    def load_file(path: str) -> Policy:
        """Load and parse a YAML policy file from *path*."""
        with open(path, "r", encoding="utf-8") as fh:
            return PolicyLoader.load_string(fh.read())

    @staticmethod
    def load_string(text: str) -> Policy:
        """Parse a YAML policy string."""
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise PolicyLoadError(f"Invalid YAML: {exc}") from exc

        if data is None:
            data = {}

        return PolicyLoader._parse(data)

    @staticmethod
    def _parse(data: dict) -> Policy:
        # ── defaults ──────────────────────────────────────────────────────
        defaults = data.get("defaults", {}) or {}
        default_priority = PolicyLoader._parse_priority(
            defaults.get("priority", "MEDIUM"), "defaults.priority"
        )
        bandwidth_budget = dict(DEFAULT_BANDWIDTH_BUDGET)
        if "bandwidth_budget" in defaults:
            for name, frac in defaults["bandwidth_budget"].items():
                p = PolicyLoader._parse_priority(name, "defaults.bandwidth_budget")
                val = float(frac)
                if not 0.0 <= val <= 1.0:
                    raise PolicyLoadError(
                        f"bandwidth_budget.{name} must be in [0, 1], got {val}"
                    )
                bandwidth_budget[p] = val

        # ── rules ─────────────────────────────────────────────────────────
        raw_rules = data.get("rules", []) or []
        if not isinstance(raw_rules, list):
            raise PolicyLoadError("'rules' must be a list")

        rules: List[PolicyRule] = []
        for i, raw in enumerate(raw_rules):
            rules.append(PolicyLoader._parse_rule(raw, index=i))

        return Policy(
            rules=rules,
            default_priority=default_priority,
            bandwidth_budget=bandwidth_budget,
        )

    @staticmethod
    def _parse_rule(raw: dict, index: int) -> PolicyRule:
        ctx = f"rules[{index}]"

        name = raw.get("name")
        if not name:
            raise PolicyLoadError(f"{ctx}: 'name' is required")

        priority_str = raw.get("priority")
        if not priority_str:
            raise PolicyLoadError(f"{ctx} ({name}): 'priority' is required")
        priority = PolicyLoader._parse_priority(priority_str, f"{ctx}.priority")

        match = raw.get("match", {}) or {}

        # ports
        raw_ports = match.get("ports", []) or []
        ports: Tuple[int, ...] = tuple(int(p) for p in raw_ports)

        # protocols
        raw_protos = match.get("protocols", []) or []
        protocols: Tuple[str, ...] = tuple(str(p).lower() for p in raw_protos)

        # payload_pattern
        payload_pattern: Optional[str] = match.get("payload_pattern") or None

        # value_coefficient
        raw_vc = raw.get("value_coefficient", 1.0)
        value_coefficient = float(raw_vc)
        if value_coefficient < 0.0:
            raise PolicyLoadError(
                f"{ctx} ({name}): value_coefficient must be ≥ 0, got {value_coefficient}"
            )

        return PolicyRule(
            name=str(name),
            priority=priority,
            ports=ports,
            protocols=protocols,
            payload_pattern=payload_pattern,
            bandwidth_min_pct=float(raw.get("bandwidth_min_pct", 0.0)),
            description=str(raw.get("description", "")),
            value_coefficient=value_coefficient,
        )

    @staticmethod
    def _parse_priority(value: str, context: str) -> TrafficPriority:
        try:
            return TrafficPriority[value.upper()]
        except KeyError:
            valid = [p.name for p in TrafficPriority]
            raise PolicyLoadError(
                f"{context}: unknown priority {value!r}; valid values: {valid}"
            )


class PolicyLoadError(ValueError):
    """Raised when a policy file cannot be parsed."""
