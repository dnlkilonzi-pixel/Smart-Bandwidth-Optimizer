"""
Tests for bandwidth_optimizer.config
"""

import pytest

from bandwidth_optimizer.config import (
    DEFAULT_BANDWIDTH_BUDGET,
    DeploymentMode,
    OptimizerConfig,
    TrafficPriority,
)


class TestDeploymentMode:
    def test_all_modes_exist(self):
        assert DeploymentMode.ROUTER.value == "router"
        assert DeploymentMode.LOCAL_SERVER.value == "local_server"
        assert DeploymentMode.ISP_EDGE.value == "isp_edge"


class TestTrafficPriority:
    def test_critical_is_lowest_number(self):
        assert TrafficPriority.CRITICAL.value < TrafficPriority.BACKGROUND.value

    def test_ordering(self):
        priorities = list(TrafficPriority)
        values = [p.value for p in priorities]
        assert values == sorted(values)


class TestDefaultBandwidthBudget:
    def test_budget_total_leq_one(self):
        total = sum(DEFAULT_BANDWIDTH_BUDGET.values())
        assert total <= 1.0, f"Budget sums to {total}, expected ≤ 1.0"

    def test_all_priorities_have_budget(self):
        for p in TrafficPriority:
            assert p in DEFAULT_BANDWIDTH_BUDGET


class TestOptimizerConfig:
    def test_defaults(self):
        cfg = OptimizerConfig()
        assert cfg.mode == DeploymentMode.LOCAL_SERVER
        assert cfg.total_bandwidth_bps > 0
        assert cfg.max_queue_size > 0
        assert 1 <= cfg.compression_level <= 9

    def test_effective_refill_rate_from_budget(self):
        cfg = OptimizerConfig(total_bandwidth_bps=1000)
        rate = cfg.effective_refill_rate(TrafficPriority.CRITICAL)
        expected = DEFAULT_BANDWIDTH_BUDGET[TrafficPriority.CRITICAL] * 1000
        assert rate == pytest.approx(expected)

    def test_effective_refill_rate_override(self):
        cfg = OptimizerConfig(
            total_bandwidth_bps=1000,
            token_refill_rate={TrafficPriority.CRITICAL: 999.0},
        )
        assert cfg.effective_refill_rate(TrafficPriority.CRITICAL) == 999.0

    def test_custom_mode(self):
        cfg = OptimizerConfig(mode=DeploymentMode.ROUTER)
        assert cfg.mode == DeploymentMode.ROUTER
