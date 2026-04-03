"""
Tests for bandwidth_optimizer.policy (Upgrade 3 – YAML Policy DSL)
"""

import textwrap

import pytest

from bandwidth_optimizer.classifier import TrafficClassifier
from bandwidth_optimizer.config import TrafficPriority
from bandwidth_optimizer.policy import Policy, PolicyLoadError, PolicyLoader, PolicyRule


MINIMAL_POLICY = textwrap.dedent("""\
    rules:
      - name: https_rule
        match:
          ports: [443]
          protocols: [tcp]
        priority: HIGH
""")

FULL_POLICY = textwrap.dedent("""\
    version: "1"
    defaults:
      priority: LOW
      bandwidth_budget:
        CRITICAL:   0.35
        HIGH:       0.25
        MEDIUM:     0.20
        LOW:        0.10
        BACKGROUND: 0.05
    rules:
      - name: voip
        description: "VoIP SIP"
        match:
          ports: [5060]
          protocols: [udp]
        priority: CRITICAL
        bandwidth_min_pct: 30

      - name: web
        match:
          ports: [80, 443]
          protocols: [tcp]
        priority: HIGH

      - name: torrent
        match:
          ports: [6881]
          protocols: [tcp, udp]
          payload_pattern: "BitTorrent"
        priority: BACKGROUND
""")


class TestPolicyLoader:
    def test_load_minimal_policy(self):
        policy = PolicyLoader.load_string(MINIMAL_POLICY)
        assert isinstance(policy, Policy)
        assert len(policy.rules) == 1

    def test_rule_fields(self):
        policy = PolicyLoader.load_string(MINIMAL_POLICY)
        rule = policy.rules[0]
        assert rule.name == "https_rule"
        assert rule.priority == TrafficPriority.HIGH
        assert 443 in rule.ports
        assert "tcp" in rule.protocols

    def test_full_policy_parses(self):
        policy = PolicyLoader.load_string(FULL_POLICY)
        assert len(policy.rules) == 3
        assert policy.default_priority == TrafficPriority.LOW

    def test_custom_bandwidth_budget(self):
        policy = PolicyLoader.load_string(FULL_POLICY)
        assert policy.bandwidth_budget[TrafficPriority.CRITICAL] == pytest.approx(0.35)

    def test_bandwidth_min_pct(self):
        policy = PolicyLoader.load_string(FULL_POLICY)
        voip = policy.rules[0]
        assert voip.bandwidth_min_pct == 30.0

    def test_payload_pattern_parsed(self):
        policy = PolicyLoader.load_string(FULL_POLICY)
        torrent = policy.rules[2]
        assert torrent.payload_pattern == "BitTorrent"

    def test_description_preserved(self):
        policy = PolicyLoader.load_string(FULL_POLICY)
        assert "SIP" in policy.rules[0].description

    def test_default_priority_fallback(self):
        policy = PolicyLoader.load_string(MINIMAL_POLICY)
        assert policy.default_priority == TrafficPriority.MEDIUM

    def test_empty_yaml_returns_default_policy(self):
        policy = PolicyLoader.load_string("")
        assert isinstance(policy, Policy)
        assert policy.rules == []

    def test_invalid_yaml_raises(self):
        with pytest.raises(PolicyLoadError, match="YAML"):
            PolicyLoader.load_string("{ bad yaml :")

    def test_missing_name_raises(self):
        bad = textwrap.dedent("""\
            rules:
              - match:
                  ports: [80]
                priority: HIGH
        """)
        with pytest.raises(PolicyLoadError, match="name"):
            PolicyLoader.load_string(bad)

    def test_missing_priority_raises(self):
        bad = textwrap.dedent("""\
            rules:
              - name: no_prio
                match:
                  ports: [80]
        """)
        with pytest.raises(PolicyLoadError, match="priority"):
            PolicyLoader.load_string(bad)

    def test_invalid_priority_value_raises(self):
        bad = textwrap.dedent("""\
            rules:
              - name: bad
                match:
                  ports: [80]
                priority: SUPER_CRITICAL
        """)
        with pytest.raises(PolicyLoadError, match="SUPER_CRITICAL"):
            PolicyLoader.load_string(bad)

    def test_invalid_budget_fraction_raises(self):
        bad = textwrap.dedent("""\
            defaults:
              bandwidth_budget:
                CRITICAL: 1.5
            rules: []
        """)
        with pytest.raises(PolicyLoadError, match="CRITICAL"):
            PolicyLoader.load_string(bad)

    def test_load_file(self, tmp_path):
        policy_file = tmp_path / "test.yaml"
        policy_file.write_text(MINIMAL_POLICY)
        policy = PolicyLoader.load_file(str(policy_file))
        assert len(policy.rules) == 1

    def test_load_file_not_found(self):
        with pytest.raises(OSError):
            PolicyLoader.load_file("/nonexistent/path/policy.yaml")


class TestPolicyToClassificationRules:
    def test_converts_correctly(self):
        policy = PolicyLoader.load_string(FULL_POLICY)
        rules = policy.to_classification_rules()
        assert len(rules) == 3

    def test_classifier_uses_policy_rules(self):
        from bandwidth_optimizer.classifier import Packet, TrafficClassifier

        policy = PolicyLoader.load_string(FULL_POLICY)
        clf = TrafficClassifier(
            rules=policy.to_classification_rules(),
            default_priority=policy.default_priority,
        )
        # VoIP → CRITICAL
        pkt = Packet(dst_port=5060, protocol="udp")
        assert clf.classify(pkt) == TrafficPriority.CRITICAL

        # Web → HIGH
        pkt2 = Packet(dst_port=443, protocol="tcp")
        assert clf.classify(pkt2) == TrafficPriority.HIGH

        # Unknown → default (LOW in full policy)
        pkt3 = Packet(dst_port=9999, protocol="tcp")
        assert clf.classify(pkt3) == TrafficPriority.LOW

    def test_example_policy_file_loads(self):
        """The checked-in policy_example.yaml must parse without errors."""
        import os
        path = os.path.join(
            os.path.dirname(__file__), "..", "policy_example.yaml"
        )
        policy = PolicyLoader.load_file(path)
        assert len(policy.rules) > 0
