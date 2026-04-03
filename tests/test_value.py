"""
Tests for bandwidth_optimizer.value (Packet Value Model – PVM)
"""

import textwrap
import time

import pytest

from bandwidth_optimizer import (
    BandwidthOptimizer,
    OptimizerConfig,
    Packet,
    TrafficPriority,
)
from bandwidth_optimizer.policy import PolicyLoader
from bandwidth_optimizer.value import (
    FlowValuePolicy,
    ValueLossTracker,
    ValueScheduler,
    ValueSLAContract,
)
from bandwidth_optimizer.policy import PolicyRule


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_packet(
    dst_port: int = 443,
    protocol: str = "tcp",
    size: int = 200,
    priority: TrafficPriority = TrafficPriority.MEDIUM,
    value_coefficient: float = 1.0,
) -> Packet:
    pkt = Packet(
        src_ip="10.0.0.1",
        dst_ip="10.0.0.2",
        src_port=1234,
        dst_port=dst_port,
        protocol=protocol,
        payload=b"X" * size,
        size_bytes=size,
    )
    pkt.priority = priority
    pkt.value_coefficient = value_coefficient
    return pkt


_VALUE_POLICY_YAML = textwrap.dedent("""\
    version: "1"
    defaults:
      priority: MEDIUM
    rules:
      - name: voip
        match:
          ports: [5060]
          protocols: [udp]
        priority: CRITICAL
        value_coefficient: 100.0

      - name: enterprise_https
        match:
          ports: [443]
          protocols: [tcp]
        priority: HIGH
        value_coefficient: 50.0

      - name: bulk_transfer
        match:
          ports: [6881]
          protocols: [tcp]
        priority: BACKGROUND
        value_coefficient: 0.5
""")


# ─────────────────────────── FlowValuePolicy ─────────────────────────────────

class TestFlowValuePolicy:
    def test_default_coefficient_for_unmatched(self):
        vp = FlowValuePolicy()
        pkt = _make_packet(dst_port=9999)
        assert vp.get_coefficient(pkt) == pytest.approx(1.0)

    def test_custom_default_coefficient(self):
        vp = FlowValuePolicy(default_coefficient=2.5)
        pkt = _make_packet(dst_port=9999)
        assert vp.get_coefficient(pkt) == pytest.approx(2.5)

    def test_rule_match_by_port(self):
        rule = PolicyRule(
            name="voip",
            priority=TrafficPriority.CRITICAL,
            ports=(5060,),
            protocols=("udp",),
            value_coefficient=100.0,
        )
        vp = FlowValuePolicy(rules=[rule])
        pkt = _make_packet(dst_port=5060, protocol="udp")
        assert vp.get_coefficient(pkt) == pytest.approx(100.0)

    def test_rule_no_match_wrong_protocol(self):
        rule = PolicyRule(
            name="voip",
            priority=TrafficPriority.CRITICAL,
            ports=(5060,),
            protocols=("udp",),
            value_coefficient=100.0,
        )
        vp = FlowValuePolicy(rules=[rule])
        pkt = _make_packet(dst_port=5060, protocol="tcp")
        assert vp.get_coefficient(pkt) == pytest.approx(1.0)

    def test_first_match_wins(self):
        r1 = PolicyRule(name="r1", priority=TrafficPriority.HIGH,
                        ports=(443,), protocols=("tcp",), value_coefficient=10.0)
        r2 = PolicyRule(name="r2", priority=TrafficPriority.HIGH,
                        ports=(443,), protocols=("tcp",), value_coefficient=99.0)
        vp = FlowValuePolicy(rules=[r1, r2])
        pkt = _make_packet(dst_port=443, protocol="tcp")
        assert vp.get_coefficient(pkt) == pytest.approx(10.0)

    def test_assign_mutates_packet(self):
        rule = PolicyRule(name="r", priority=TrafficPriority.HIGH,
                          ports=(443,), protocols=("tcp",), value_coefficient=7.5)
        vp = FlowValuePolicy(rules=[rule])
        pkt = _make_packet(dst_port=443, protocol="tcp")
        coeff = vp.assign(pkt)
        assert coeff == pytest.approx(7.5)
        assert pkt.value_coefficient == pytest.approx(7.5)

    def test_from_policy(self):
        policy = PolicyLoader.load_string(_VALUE_POLICY_YAML)
        vp = FlowValuePolicy.from_policy(policy)

        pkt_voip = _make_packet(dst_port=5060, protocol="udp")
        assert vp.get_coefficient(pkt_voip) == pytest.approx(100.0)

        pkt_https = _make_packet(dst_port=443, protocol="tcp")
        assert vp.get_coefficient(pkt_https) == pytest.approx(50.0)

        pkt_bt = _make_packet(dst_port=6881, protocol="tcp")
        assert vp.get_coefficient(pkt_bt) == pytest.approx(0.5)

    def test_from_policy_unknown_port_returns_default(self):
        policy = PolicyLoader.load_string(_VALUE_POLICY_YAML)
        vp = FlowValuePolicy.from_policy(policy)
        pkt = _make_packet(dst_port=9999, protocol="tcp")
        assert vp.get_coefficient(pkt) == pytest.approx(1.0)

    def test_source_port_match(self):
        rule = PolicyRule(name="r", priority=TrafficPriority.HIGH,
                          ports=(443,), protocols=("tcp",), value_coefficient=5.0)
        vp = FlowValuePolicy(rules=[rule])
        # src_port matches
        pkt = Packet(src_ip="1.2.3.4", dst_ip="5.6.7.8",
                     src_port=443, dst_port=9999,
                     protocol="tcp", payload=b"x" * 10)
        assert vp.get_coefficient(pkt) == pytest.approx(5.0)


# ─────────────────────────── ValueScheduler ──────────────────────────────────

class TestValueScheduler:
    def test_enqueue_dequeue_basic(self):
        sched = ValueScheduler(max_queue_size=10)
        pkt = _make_packet()
        assert sched.enqueue(pkt)
        assert sched.dequeue() is pkt

    def test_empty_dequeue_returns_none(self):
        sched = ValueScheduler()
        assert sched.dequeue() is None

    def test_higher_value_dequeues_first(self):
        sched = ValueScheduler(max_queue_size=10)
        low = _make_packet(priority=TrafficPriority.MEDIUM, value_coefficient=1.0)
        high = _make_packet(priority=TrafficPriority.MEDIUM, value_coefficient=50.0)
        sched.enqueue(low)
        sched.enqueue(high)
        first = sched.dequeue()
        assert first is high

    def test_same_priority_higher_coefficient_wins(self):
        sched = ValueScheduler(max_queue_size=10)
        p1 = _make_packet(priority=TrafficPriority.BACKGROUND, value_coefficient=1.0)
        p2 = _make_packet(priority=TrafficPriority.BACKGROUND, value_coefficient=100.0)
        sched.enqueue(p1)
        sched.enqueue(p2)
        assert sched.dequeue() is p2

    def test_default_coefficient_respects_priority_order(self):
        """At coefficient=1.0 the ordering must match the original priority."""
        sched = ValueScheduler(max_queue_size=10)
        bg = _make_packet(priority=TrafficPriority.BACKGROUND, value_coefficient=1.0)
        hi = _make_packet(priority=TrafficPriority.HIGH, value_coefficient=1.0)
        cr = _make_packet(priority=TrafficPriority.CRITICAL, value_coefficient=1.0)
        sched.enqueue(bg)
        sched.enqueue(hi)
        sched.enqueue(cr)
        order = [sched.dequeue().priority for _ in range(3)]
        assert order == [
            TrafficPriority.CRITICAL,
            TrafficPriority.HIGH,
            TrafficPriority.BACKGROUND,
        ]

    def test_high_value_background_beats_low_value_critical(self):
        """A BACKGROUND flow with coefficient=100 should beat CRITICAL with coefficient=0.1."""
        sched = ValueScheduler(max_queue_size=10)
        crit = _make_packet(priority=TrafficPriority.CRITICAL, value_coefficient=0.1)
        bg = _make_packet(priority=TrafficPriority.BACKGROUND, value_coefficient=100.0)
        sched.enqueue(crit)
        sched.enqueue(bg)
        # bg effective_value = 100 * 1.0 = 100.0
        # crit effective_value = 0.1 * 5.0 = 0.5
        assert sched.dequeue() is bg

    def test_overflow_drops_lowest_value(self):
        sched = ValueScheduler(max_queue_size=2)
        p_low = _make_packet(priority=TrafficPriority.BACKGROUND, value_coefficient=0.1)
        p_med = _make_packet(priority=TrafficPriority.MEDIUM, value_coefficient=1.0)
        p_high = _make_packet(priority=TrafficPriority.HIGH, value_coefficient=10.0)
        sched.enqueue(p_low)
        sched.enqueue(p_med)
        # queue full: next enqueue should evict lowest value (p_low)
        accepted = sched.enqueue(p_high)
        assert accepted
        assert len(sched) == 2
        out = [sched.dequeue(), sched.dequeue()]
        assert p_high in out
        assert p_med in out
        assert p_low not in out

    def test_enqueued_at_is_stamped(self):
        sched = ValueScheduler()
        pkt = _make_packet()
        before = time.monotonic()
        sched.enqueue(pkt)
        after = time.monotonic()
        assert pkt.enqueued_at is not None
        assert before <= pkt.enqueued_at <= after

    def test_stats_reports_counts(self):
        sched = ValueScheduler(max_queue_size=5)
        for _ in range(3):
            sched.enqueue(_make_packet())
        s = sched.stats()
        assert s["current_queue_size"] == 3
        assert s["max_queue_size"] == 5

    def test_drain_empties_queue(self):
        sched = ValueScheduler(max_queue_size=10)
        for _ in range(5):
            sched.enqueue(_make_packet())
        drained = list(sched.drain())
        assert len(drained) == 5
        assert sched.is_empty()

    def test_len(self):
        sched = ValueScheduler(max_queue_size=10)
        assert len(sched) == 0
        sched.enqueue(_make_packet())
        assert len(sched) == 1

    def test_is_full(self):
        sched = ValueScheduler(max_queue_size=2)
        sched.enqueue(_make_packet())
        sched.enqueue(_make_packet())
        assert sched.is_full()

    def test_reset_stats(self):
        sched = ValueScheduler()
        sched.enqueue(_make_packet())
        sched.dequeue()
        sched.reset_stats()
        s = sched.stats()
        for p in TrafficPriority:
            assert s["enqueue_counts"][p] == 0
            assert s["dequeue_counts"][p] == 0

    def test_peek_does_not_remove(self):
        sched = ValueScheduler(max_queue_size=5)
        pkt = _make_packet(value_coefficient=10.0)
        sched.enqueue(pkt)
        assert sched.peek() is pkt
        assert len(sched) == 1


# ─────────────────────────── ValueSLAContract ────────────────────────────────

class TestValueSLAContract:
    def test_not_violated_above_minimum(self):
        contract = ValueSLAContract("voip", min_value_rate_per_sec=100.0)
        assert not contract.is_violated(150.0)

    def test_not_violated_at_exact_minimum(self):
        contract = ValueSLAContract("voip", min_value_rate_per_sec=100.0)
        assert not contract.is_violated(100.0)

    def test_violated_below_minimum(self):
        contract = ValueSLAContract("voip", min_value_rate_per_sec=100.0)
        assert contract.is_violated(99.9)

    def test_violated_at_zero(self):
        contract = ValueSLAContract("voip", min_value_rate_per_sec=100.0)
        assert contract.is_violated(0.0)

    def test_to_dict_not_violated(self):
        contract = ValueSLAContract("tenant1", min_value_rate_per_sec=50.0,
                                    value_coefficient=2.0)
        d = contract.to_dict(current_rate=75.0)
        assert d["tenant_id"] == "tenant1"
        assert d["min_value_rate_per_sec"] == 50.0
        assert d["current_value_rate_per_sec"] == pytest.approx(75.0)
        assert d["violated"] is False

    def test_to_dict_violated(self):
        contract = ValueSLAContract("tenant1", min_value_rate_per_sec=50.0)
        d = contract.to_dict(current_rate=10.0)
        assert d["violated"] is True

    def test_to_dict_default_rate(self):
        contract = ValueSLAContract("x", min_value_rate_per_sec=10.0)
        d = contract.to_dict()
        assert d["current_value_rate_per_sec"] == pytest.approx(0.0)
        assert d["violated"] is True


# ─────────────────────────── ValueLossTracker ────────────────────────────────

class TestValueLossTracker:
    def test_initial_totals_zero(self):
        t = ValueLossTracker()
        assert t.value_delivered_total == 0.0
        assert t.value_lost_total == 0.0

    def test_initial_efficiency_100(self):
        t = ValueLossTracker()
        assert t.value_efficiency_pct == pytest.approx(100.0)

    def test_record_delivered_accumulates(self):
        t = ValueLossTracker()
        t.record_delivered(10.0)
        t.record_delivered(5.0)
        assert t.value_delivered_total == pytest.approx(15.0)

    def test_record_dropped_accumulates(self):
        t = ValueLossTracker()
        t.record_dropped(3.0)
        t.record_dropped(2.0)
        assert t.value_lost_total == pytest.approx(5.0)

    def test_efficiency_with_some_loss(self):
        t = ValueLossTracker(window_seconds=60.0)
        t.record_delivered(90.0)
        t.record_dropped(10.0)
        assert t.value_efficiency_pct == pytest.approx(90.0, abs=1.0)

    def test_per_sec_rates_in_window(self):
        t = ValueLossTracker(window_seconds=5.0)
        t.record_delivered(50.0)
        # 50 / 5 = 10.0 per second
        assert t.value_delivered_per_sec == pytest.approx(10.0, abs=0.1)

    def test_no_loss_100_efficiency(self):
        t = ValueLossTracker(window_seconds=5.0)
        t.record_delivered(100.0)
        assert t.value_efficiency_pct == pytest.approx(100.0)

    def test_reset_clears_everything(self):
        t = ValueLossTracker()
        t.record_delivered(100.0)
        t.record_dropped(50.0)
        t.reset()
        assert t.value_delivered_total == 0.0
        assert t.value_lost_total == 0.0

    def test_to_dict_keys(self):
        t = ValueLossTracker()
        t.record_delivered(10.0)
        d = t.to_dict()
        assert "value_delivered_total" in d
        assert "value_lost_total" in d
        assert "value_delivered_per_sec" in d
        assert "value_lost_per_sec" in d
        assert "value_efficiency_pct" in d
        assert "window_seconds" in d

    def test_to_dict_values_match_properties(self):
        t = ValueLossTracker()
        t.record_delivered(10.0)
        t.record_dropped(2.0)
        d = t.to_dict()
        assert d["value_delivered_total"] == pytest.approx(10.0)
        assert d["value_lost_total"] == pytest.approx(2.0)


# ─────────────────────────── BandwidthOptimizer PVM integration ──────────────

class TestOptimizerPVMIntegration:
    def _make_optimizer(self, with_pvm: bool = True) -> BandwidthOptimizer:
        cfg = OptimizerConfig(
            total_bandwidth_bps=10 * 1024 * 1024 * 1024,  # no drops
            max_queue_size=10_000,
        )
        if with_pvm:
            policy = PolicyLoader.load_string(_VALUE_POLICY_YAML)
            vp = FlowValuePolicy.from_policy(policy)
            return BandwidthOptimizer(config=cfg, flow_value_policy=vp)
        return BandwidthOptimizer(config=cfg)

    def test_value_tracker_present_when_pvm(self):
        opt = self._make_optimizer(with_pvm=True)
        assert opt.value_tracker is not None

    def test_value_tracker_absent_without_pvm(self):
        opt = self._make_optimizer(with_pvm=False)
        assert opt.value_tracker is None

    def test_flow_value_policy_property(self):
        opt = self._make_optimizer(with_pvm=True)
        assert opt.flow_value_policy is not None

    def test_process_assigns_value_coefficient_to_packet(self):
        opt = self._make_optimizer(with_pvm=True)
        pkt = Packet(src_ip="1.2.3.4", dst_ip="5.6.7.8",
                     src_port=1234, dst_port=5060,
                     protocol="udp", payload=b"x" * 50)
        opt.process(pkt)
        assert pkt.value_coefficient == pytest.approx(100.0)

    def test_process_assigns_value_coefficient_to_flow_record(self):
        opt = self._make_optimizer(with_pvm=True)
        pkt = Packet(src_ip="1.2.3.4", dst_ip="5.6.7.8",
                     src_port=1234, dst_port=5060,
                     protocol="udp", payload=b"x" * 50)
        result = opt.process(pkt)
        assert result.flow_record is not None
        assert result.flow_record.value_coefficient == pytest.approx(100.0)

    def test_delivered_value_tracked(self):
        opt = self._make_optimizer(with_pvm=True)
        pkt = Packet(src_ip="1.2.3.4", dst_ip="5.6.7.8",
                     src_port=1234, dst_port=443,
                     protocol="tcp", payload=b"x" * 50)
        opt.process(pkt)
        # value_coefficient for port 443 is 50.0
        assert opt.value_tracker.value_delivered_total == pytest.approx(50.0)

    def test_stats_includes_value_when_pvm(self):
        opt = self._make_optimizer(with_pvm=True)
        pkt = Packet(src_ip="1.2.3.4", dst_ip="5.6.7.8",
                     src_port=1234, dst_port=443,
                     protocol="tcp", payload=b"x" * 50)
        opt.process(pkt)
        s = opt.stats()
        assert "value" in s
        assert s["value"]["value_delivered_total"] == pytest.approx(50.0)

    def test_stats_no_value_key_without_pvm(self):
        opt = self._make_optimizer(with_pvm=False)
        pkt = Packet(src_ip="1.2.3.4", dst_ip="5.6.7.8",
                     src_port=1234, dst_port=443,
                     protocol="tcp", payload=b"x" * 50)
        opt.process(pkt)
        s = opt.stats()
        assert "value" not in s

    def test_reset_stats_clears_value_tracker(self):
        opt = self._make_optimizer(with_pvm=True)
        pkt = Packet(src_ip="1.2.3.4", dst_ip="5.6.7.8",
                     src_port=1234, dst_port=443,
                     protocol="tcp", payload=b"x" * 50)
        opt.process(pkt)
        opt.reset_stats()
        assert opt.value_tracker.value_delivered_total == 0.0

    def test_value_weighted_dequeue_order(self):
        """Verify that PVM mode schedules higher-value flows first."""
        opt = self._make_optimizer(with_pvm=True)
        # BACKGROUND (low value_coefficient=0.5) BitTorrent
        bt = Packet(src_ip="1.2.3.4", dst_ip="5.6.7.8",
                    src_port=1234, dst_port=6881,
                    protocol="tcp", payload=b"x" * 50)
        # CRITICAL VoIP (value_coefficient=100.0)
        voip = Packet(src_ip="1.2.3.4", dst_ip="5.6.7.8",
                      src_port=1234, dst_port=5060,
                      protocol="udp", payload=b"x" * 50)
        opt.process(bt)
        opt.process(voip)
        first = opt.dequeue()
        # voip should dequeue first (much higher effective value)
        assert first is voip


# ─────────────────────────── PolicyRule value_coefficient ────────────────────

class TestPolicyValueCoefficient:
    def test_policy_rule_default_coefficient(self):
        rule = PolicyRule(name="r", priority=TrafficPriority.MEDIUM)
        assert rule.value_coefficient == pytest.approx(1.0)

    def test_policy_loader_parses_value_coefficient(self):
        policy = PolicyLoader.load_string(_VALUE_POLICY_YAML)
        voip = policy.rules[0]
        assert voip.name == "voip"
        assert voip.value_coefficient == pytest.approx(100.0)

    def test_policy_loader_default_coefficient_when_absent(self):
        yaml = textwrap.dedent("""\
            rules:
              - name: simple
                match:
                  ports: [80]
                  protocols: [tcp]
                priority: HIGH
        """)
        policy = PolicyLoader.load_string(yaml)
        assert policy.rules[0].value_coefficient == pytest.approx(1.0)

    def test_negative_coefficient_raises(self):
        from bandwidth_optimizer.policy import PolicyLoadError
        yaml = textwrap.dedent("""\
            rules:
              - name: bad
                match:
                  ports: [80]
                  protocols: [tcp]
                priority: HIGH
                value_coefficient: -5.0
        """)
        with pytest.raises(PolicyLoadError, match="value_coefficient"):
            PolicyLoader.load_string(yaml)

    def test_zero_coefficient_is_valid(self):
        yaml = textwrap.dedent("""\
            rules:
              - name: zero
                match:
                  ports: [6881]
                  protocols: [tcp]
                priority: BACKGROUND
                value_coefficient: 0.0
        """)
        policy = PolicyLoader.load_string(yaml)
        assert policy.rules[0].value_coefficient == pytest.approx(0.0)

    def test_flow_record_default_coefficient(self):
        from bandwidth_optimizer.flow_tracker import FlowKey, FlowRecord
        key = FlowKey("1.2.3.4", "5.6.7.8", 1234, 80, "tcp")
        record = FlowRecord(key=key)
        assert record.value_coefficient == pytest.approx(1.0)

    def test_flow_record_to_dict_includes_coefficient(self):
        from bandwidth_optimizer.flow_tracker import FlowKey, FlowRecord
        key = FlowKey("1.2.3.4", "5.6.7.8", 1234, 80, "tcp")
        record = FlowRecord(key=key)
        record.value_coefficient = 42.5
        d = record.to_dict()
        assert "value_coefficient" in d
        assert d["value_coefficient"] == pytest.approx(42.5)

    def test_packet_default_value_coefficient(self):
        pkt = Packet(dst_port=80, protocol="tcp")
        assert pkt.value_coefficient == pytest.approx(1.0)

    def test_packet_value_coefficient_assignable(self):
        pkt = Packet(dst_port=80, protocol="tcp")
        pkt.value_coefficient = 25.0
        assert pkt.value_coefficient == pytest.approx(25.0)
