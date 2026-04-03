"""
Tests for the three last-mile PVM improvements:
  Gap 1 – ValueCoefficientsGuide (calibration presets)
  Gap 2 – AgentCoordinator.fleet_value_summary (multi-node coordination)
  Gap 3 – ValueCoefficientTuner (feedback loop)
"""

import pytest

from bandwidth_optimizer.value import (
    FlowValuePolicy,
    ValueCoefficientsGuide,
    ValueCoefficientTuner,
    ValueSLAContract,
    TRAFFIC_PRESETS,
)
from bandwidth_optimizer.coordinator import AgentCoordinator
from bandwidth_optimizer.classifier import Packet
from bandwidth_optimizer.config import TrafficPriority


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_packet(dst_port: int = 443, protocol: str = "tcp") -> Packet:
    return Packet(
        src_ip="10.0.0.1", dst_ip="10.0.0.2",
        src_port=1234, dst_port=dst_port,
        protocol=protocol, payload=b"X" * 100,
    )


def _stats_with_value(eff_pct: float, delivered: float, lost: float) -> dict:
    return {
        "packets_received": 1000,
        "value": {
            "value_efficiency_pct": eff_pct,
            "value_delivered_per_sec": delivered,
            "value_lost_per_sec": lost,
        },
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Gap 1 – ValueCoefficientsGuide
# ═══════════════════════════════════════════════════════════════════════════════

class TestValueCoefficientsGuide:
    def test_presets_not_empty(self):
        guide = ValueCoefficientsGuide()
        assert len(guide.presets) > 0

    def test_traffic_presets_tuple_not_empty(self):
        assert len(TRAFFIC_PRESETS) > 0

    def test_voip_is_highest(self):
        guide = ValueCoefficientsGuide()
        coefficients = {p.name: p.value_coefficient for p in guide.presets}
        assert coefficients["voip"] == max(coefficients.values())

    def test_bulk_transfer_is_lowest(self):
        guide = ValueCoefficientsGuide()
        coefficients = {p.name: p.value_coefficient for p in guide.presets}
        assert coefficients["bulk_transfer"] == min(coefficients.values())

    def test_bulk_transfer_below_one(self):
        """Bulk flows should yield even to unclassified traffic (coefficient < 1)."""
        guide = ValueCoefficientsGuide()
        coefficients = {p.name: p.value_coefficient for p in guide.presets}
        assert coefficients["bulk_transfer"] < 1.0

    def test_revenue_scale_multiplies_all(self):
        guide = ValueCoefficientsGuide(revenue_scale=10.0)
        base = ValueCoefficientsGuide(revenue_scale=1.0)
        for sp, sb in zip(guide.presets, base.presets):
            assert sp.name == sb.name
            assert sp.value_coefficient == pytest.approx(sb.value_coefficient * 10.0)

    def test_revenue_scale_zero_gives_all_zeros(self):
        guide = ValueCoefficientsGuide(revenue_scale=0.0)
        for p in guide.presets:
            assert p.value_coefficient == 0.0

    def test_get_known_preset(self):
        guide = ValueCoefficientsGuide()
        preset = guide.get("voip")
        assert preset is not None
        assert preset.name == "voip"
        assert preset.value_coefficient == pytest.approx(100.0)

    def test_get_unknown_preset_returns_none(self):
        guide = ValueCoefficientsGuide()
        assert guide.get("nonexistent") is None

    def test_get_with_scale(self):
        guide = ValueCoefficientsGuide(revenue_scale=2.0)
        preset = guide.get("voip")
        assert preset.value_coefficient == pytest.approx(200.0)

    def test_to_dict_structure(self):
        guide = ValueCoefficientsGuide(revenue_scale=5.0)
        d = guide.to_dict()
        assert d["revenue_scale"] == 5.0
        assert "presets" in d
        assert len(d["presets"]) == len(TRAFFIC_PRESETS)
        for entry in d["presets"]:
            assert "name" in entry
            assert "value_coefficient" in entry
            assert "rationale" in entry

    def test_preset_has_rationale(self):
        for p in TRAFFIC_PRESETS:
            assert p.rationale, f"Preset {p.name!r} is missing rationale"

    def test_preset_has_description(self):
        for p in TRAFFIC_PRESETS:
            assert p.description, f"Preset {p.name!r} is missing description"

    def test_preset_priority_is_traffic_priority(self):
        for p in TRAFFIC_PRESETS:
            assert isinstance(p.priority, TrafficPriority)

    def test_preset_ports_non_empty(self):
        for p in TRAFFIC_PRESETS:
            assert len(p.ports) > 0, f"Preset {p.name!r} has no ports"


class TestFlowValuePolicyFromPresets:
    def test_from_presets_creates_policy(self):
        vp = FlowValuePolicy.from_presets()
        assert vp is not None

    def test_voip_port_gets_high_coefficient(self):
        vp = FlowValuePolicy.from_presets()
        pkt = _make_packet(dst_port=5060, protocol="udp")
        assert vp.get_coefficient(pkt) == pytest.approx(100.0)

    def test_bulk_port_gets_low_coefficient(self):
        vp = FlowValuePolicy.from_presets()
        pkt = _make_packet(dst_port=6881, protocol="tcp")
        assert vp.get_coefficient(pkt) < 1.0

    def test_unknown_port_returns_default_one(self):
        vp = FlowValuePolicy.from_presets()
        pkt = _make_packet(dst_port=9999, protocol="tcp")
        assert vp.get_coefficient(pkt) == pytest.approx(1.0)

    def test_revenue_scale_applied(self):
        vp = FlowValuePolicy.from_presets(revenue_scale=10.0)
        pkt = _make_packet(dst_port=5060, protocol="udp")
        assert vp.get_coefficient(pkt) == pytest.approx(1000.0)

    def test_override_specific_preset(self):
        vp = FlowValuePolicy.from_presets(overrides={"voip": 999.0})
        pkt = _make_packet(dst_port=5060, protocol="udp")
        assert vp.get_coefficient(pkt) == pytest.approx(999.0)

    def test_override_does_not_affect_others(self):
        vp = FlowValuePolicy.from_presets(overrides={"voip": 999.0})
        pkt = _make_packet(dst_port=6881, protocol="tcp")
        assert vp.get_coefficient(pkt) < 1.0  # bulk_transfer unchanged


class TestFlowValuePolicyRuntimeOverrides:
    def test_set_override_changes_coefficient(self):
        vp = FlowValuePolicy.from_presets()
        pkt = _make_packet(dst_port=5060, protocol="udp")
        vp.set_override("voip", 500.0)
        assert vp.get_coefficient(pkt) == pytest.approx(500.0)

    def test_clear_override_restores_original(self):
        vp = FlowValuePolicy.from_presets()
        pkt = _make_packet(dst_port=5060, protocol="udp")
        vp.set_override("voip", 500.0)
        vp.clear_override("voip")
        assert vp.get_coefficient(pkt) == pytest.approx(100.0)

    def test_overrides_snapshot(self):
        vp = FlowValuePolicy.from_presets()
        vp.set_override("voip", 200.0)
        snap = vp.overrides_snapshot()
        assert snap["voip"] == pytest.approx(200.0)

    def test_get_override_none_when_absent(self):
        vp = FlowValuePolicy.from_presets()
        assert vp.get_override("voip") is None

    def test_set_override_negative_clips_to_zero(self):
        vp = FlowValuePolicy.from_presets()
        vp.set_override("voip", -10.0)
        assert vp.get_override("voip") == pytest.approx(0.0)


# ═══════════════════════════════════════════════════════════════════════════════
# Gap 2 – AgentCoordinator.fleet_value_summary
# ═══════════════════════════════════════════════════════════════════════════════

class TestFleetValueSummary:
    def test_empty_coordinator_returns_empty(self):
        coord = AgentCoordinator()
        summary = coord.fleet_value_summary()
        assert summary["pvm_node_count"] == 0
        assert summary["fleet_value_efficiency_pct"] is None

    def test_non_pvm_nodes_listed(self):
        coord = AgentCoordinator()
        coord.ingest("node-01", {"packets_received": 100})
        coord.ingest("node-02", {"packets_received": 200})
        summary = coord.fleet_value_summary()
        assert "node-01" in summary["non_pvm_nodes"]
        assert "node-02" in summary["non_pvm_nodes"]
        assert summary["pvm_node_count"] == 0

    def test_single_pvm_node(self):
        coord = AgentCoordinator()
        coord.ingest("node-01", _stats_with_value(95.0, 190.0, 10.0))
        summary = coord.fleet_value_summary()
        assert summary["pvm_node_count"] == 1
        assert summary["fleet_value_efficiency_pct"] == pytest.approx(95.0, abs=0.1)
        assert summary["best_node"] == "node-01"
        assert summary["worst_node"] == "node-01"

    def test_fleet_efficiency_is_weighted_average(self):
        coord = AgentCoordinator()
        # node-01: delivers 90, loses 10  → 90%
        # node-02: delivers 190, loses 10 → 95%
        coord.ingest("node-01", _stats_with_value(90.0, 90.0, 10.0))
        coord.ingest("node-02", _stats_with_value(95.0, 190.0, 10.0))
        summary = coord.fleet_value_summary()
        # Fleet: (90+190)/(90+190+10+10) = 280/300 ≈ 93.33%
        assert summary["fleet_value_efficiency_pct"] == pytest.approx(93.33, abs=0.2)

    def test_best_and_worst_node_identified(self):
        coord = AgentCoordinator()
        coord.ingest("node-best", _stats_with_value(99.0, 990.0, 10.0))
        coord.ingest("node-worst", _stats_with_value(50.0, 50.0, 50.0))
        coord.ingest("node-mid", _stats_with_value(80.0, 80.0, 20.0))
        summary = coord.fleet_value_summary()
        assert summary["best_node"] == "node-best"
        assert summary["worst_node"] == "node-worst"

    def test_fleet_delivered_and_lost_sum(self):
        coord = AgentCoordinator()
        coord.ingest("node-01", _stats_with_value(90.0, 100.0, 10.0))
        coord.ingest("node-02", _stats_with_value(95.0, 200.0, 20.0))
        summary = coord.fleet_value_summary()
        assert summary["fleet_value_delivered_per_sec"] == pytest.approx(300.0)
        assert summary["fleet_value_lost_per_sec"] == pytest.approx(30.0)

    def test_nodes_per_node_breakdown(self):
        coord = AgentCoordinator()
        coord.ingest("node-01", _stats_with_value(95.0, 190.0, 10.0))
        summary = coord.fleet_value_summary()
        assert "node-01" in summary["nodes"]
        node = summary["nodes"]["node-01"]
        assert "value_efficiency_pct" in node
        assert "value_delivered_per_sec" in node
        assert "value_lost_per_sec" in node

    def test_mixed_pvm_and_non_pvm(self):
        coord = AgentCoordinator()
        coord.ingest("pvm-node", _stats_with_value(80.0, 80.0, 20.0))
        coord.ingest("legacy-node", {"packets_received": 500})
        summary = coord.fleet_value_summary()
        assert summary["pvm_node_count"] == 1
        assert "legacy-node" in summary["non_pvm_nodes"]
        assert "pvm-node" not in summary["non_pvm_nodes"]

    def test_all_nodes_zero_delivers_100_efficiency(self):
        """Edge case: if nothing is delivered or lost, efficiency should be 100%."""
        coord = AgentCoordinator()
        coord.ingest("node-01", _stats_with_value(100.0, 0.0, 0.0))
        summary = coord.fleet_value_summary()
        assert summary["fleet_value_efficiency_pct"] == pytest.approx(100.0)

    def test_stale_nodes_excluded(self):
        """Expired agents must not appear in the fleet value summary."""
        import time
        coord = AgentCoordinator(agent_ttl=0.01)  # 10 ms TTL
        coord.ingest("stale-node", _stats_with_value(50.0, 50.0, 50.0))
        time.sleep(0.05)  # let it expire
        summary = coord.fleet_value_summary()
        assert summary["pvm_node_count"] == 0

    def test_note_present_when_no_pvm_nodes(self):
        coord = AgentCoordinator()
        coord.ingest("legacy", {"packets_received": 100})
        summary = coord.fleet_value_summary()
        assert "note" in summary


# ═══════════════════════════════════════════════════════════════════════════════
# Gap 3 – ValueCoefficientTuner
# ═══════════════════════════════════════════════════════════════════════════════

class TestValueCoefficientTuner:
    def _make_policy_and_tuner(self, boost=1.2, decay=0.95):
        vp = FlowValuePolicy.from_presets()
        tuner = ValueCoefficientTuner(vp, boost_factor=boost, decay_factor=decay)
        return vp, tuner

    # ── boost on SLA violation ────────────────────────────────────────────

    def test_violation_boosts_coefficient(self):
        vp, tuner = self._make_policy_and_tuner(boost=1.2)
        base = 100.0  # voip preset
        new_coeff = tuner.observe("voip", delivered_rate=50.0, sla_min_rate=100.0)
        assert new_coeff == pytest.approx(base * 1.2)

    def test_violation_sets_override_in_policy(self):
        vp, tuner = self._make_policy_and_tuner()
        tuner.observe("voip", delivered_rate=50.0, sla_min_rate=100.0)
        pkt = _make_packet(dst_port=5060, protocol="udp")
        # Override should be higher than original
        assert vp.get_coefficient(pkt) > 100.0

    def test_multiple_violations_compound(self):
        vp, tuner = self._make_policy_and_tuner(boost=1.2)
        coeff = tuner.observe("voip", delivered_rate=0.0, sla_min_rate=100.0)
        coeff = tuner.observe("voip", delivered_rate=0.0, sla_min_rate=100.0)
        assert coeff == pytest.approx(100.0 * 1.2 * 1.2)

    def test_boost_capped_at_max_coefficient(self):
        vp, tuner = self._make_policy_and_tuner(boost=1000.0)
        tuner._max_coeff = 150.0
        coeff = tuner.observe("voip", delivered_rate=0.0, sla_min_rate=100.0)
        assert coeff == pytest.approx(150.0)

    # ── decay on perfect delivery ─────────────────────────────────────────

    def test_perfect_delivery_decays_coefficient(self):
        vp, tuner = self._make_policy_and_tuner(decay=0.9)
        # First boost it
        tuner.observe("voip", delivered_rate=0.0, sla_min_rate=100.0)
        current = vp.get_override("voip")
        # Now decay
        new_coeff = tuner.observe("voip", delivered_rate=200.0, sla_min_rate=100.0)
        assert new_coeff < current

    def test_decay_floors_at_base_coefficient(self):
        vp, tuner = self._make_policy_and_tuner(decay=0.0001)
        # Many perfect-delivery observations should not go below the base (100.0 for voip)
        for _ in range(20):
            new_coeff = tuner.observe("voip", delivered_rate=200.0, sla_min_rate=100.0)
        assert new_coeff == pytest.approx(100.0)

    # ── no change in the acceptable zone ─────────────────────────────────

    def test_acceptable_zone_no_change(self):
        vp, tuner = self._make_policy_and_tuner()
        # 95% efficiency: above violation threshold, below perfection threshold
        coeff1 = tuner.observe("voip", delivered_rate=95.0, sla_min_rate=100.0)
        coeff2 = tuner.observe("voip", delivered_rate=95.0, sla_min_rate=100.0)
        assert coeff1 == coeff2

    # ── observe_contract convenience ─────────────────────────────────────

    def test_observe_contract_uses_sla_min_rate(self):
        vp, tuner = self._make_policy_and_tuner(boost=1.2)
        contract = ValueSLAContract("voip_tenant", min_value_rate_per_sec=100.0)
        new_coeff = tuner.observe_contract(
            "voip", delivered_rate=50.0, contract=contract
        )
        assert new_coeff == pytest.approx(100.0 * 1.2)

    # ── reset ─────────────────────────────────────────────────────────────

    def test_reset_single_rule(self):
        vp, tuner = self._make_policy_and_tuner()
        tuner.observe("voip", delivered_rate=0.0, sla_min_rate=100.0)
        tuner.reset("voip")
        pkt = _make_packet(dst_port=5060, protocol="udp")
        # After reset, override is cleared → falls back to static 100.0
        assert vp.get_coefficient(pkt) == pytest.approx(100.0)

    def test_reset_all_clears_all_rules(self):
        vp, tuner = self._make_policy_and_tuner()
        tuner.observe("voip", delivered_rate=0.0, sla_min_rate=100.0)
        tuner.observe("interactive_api", delivered_rate=0.0, sla_min_rate=50.0)
        tuner.reset_all()
        assert vp.overrides_snapshot() == {}

    # ── tuning_report ─────────────────────────────────────────────────────

    def test_tuning_report_keys(self):
        vp, tuner = self._make_policy_and_tuner()
        tuner.observe("voip", delivered_rate=0.0, sla_min_rate=100.0)
        report = tuner.tuning_report()
        assert "boost_factor" in report
        assert "decay_factor" in report
        assert "rules" in report
        assert "voip" in report["rules"]

    def test_tuning_report_boost_count_increments(self):
        vp, tuner = self._make_policy_and_tuner()
        tuner.observe("voip", delivered_rate=0.0, sla_min_rate=100.0)
        tuner.observe("voip", delivered_rate=0.0, sla_min_rate=100.0)
        report = tuner.tuning_report()
        assert report["rules"]["voip"]["boost_count"] == 2

    def test_tuning_report_decay_count_increments(self):
        vp, tuner = self._make_policy_and_tuner()
        tuner.observe("voip", delivered_rate=0.0, sla_min_rate=100.0)
        # perfect delivery triggers decay
        tuner.observe("voip", delivered_rate=200.0, sla_min_rate=100.0)
        report = tuner.tuning_report()
        assert report["rules"]["voip"]["decay_count"] == 1

    def test_tuning_report_base_vs_current(self):
        vp, tuner = self._make_policy_and_tuner(boost=2.0)
        tuner.observe("voip", delivered_rate=0.0, sla_min_rate=100.0)
        report = tuner.tuning_report()["rules"]["voip"]
        assert report["base_coefficient"] == pytest.approx(100.0)
        assert report["current_coefficient"] == pytest.approx(200.0)

    # ── new rule (not in presets) ─────────────────────────────────────────

    def test_unknown_rule_defaults_to_base_one(self):
        vp, tuner = self._make_policy_and_tuner(boost=2.0)
        coeff = tuner.observe("unknown_rule", delivered_rate=0.0, sla_min_rate=100.0)
        # base is 1.0 (not found in rules), boosted by 2.0 → 2.0
        assert coeff == pytest.approx(2.0)

    # ── zero sla_min_rate edge case ────────────────────────────────────────

    def test_zero_sla_min_rate_no_boost(self):
        vp, tuner = self._make_policy_and_tuner()
        coeff = tuner.observe("voip", delivered_rate=0.0, sla_min_rate=0.0)
        # efficiency = 100% when sla_min_rate=0 → no boost
        assert coeff == pytest.approx(100.0)
