"""
Tests for api.server (Upgrade 4 – Real-time telemetry API)
"""

import json
import threading
import time

import pytest

try:
    from fastapi.testclient import TestClient
    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False

pytestmark = pytest.mark.skipif(not HAS_FASTAPI, reason="fastapi not installed")


def _make_test_app():
    from bandwidth_optimizer import BandwidthOptimizer, OptimizerConfig, Packet
    from api.server import create_app

    optimizer = BandwidthOptimizer(OptimizerConfig(compression_threshold_bytes=10))
    # Seed some packets so stats are interesting
    for port in [443, 80, 5060, 6881]:
        optimizer.process(
            Packet(dst_port=port, protocol="tcp",
                   payload=b"X" * 200, size_bytes=200)
        )
    return create_app(optimizer=optimizer), optimizer


class TestRestEndpoints:
    def setup_method(self):
        self.app, self.optimizer = _make_test_app()
        self.client = TestClient(self.app)

    def test_health_endpoint(self):
        resp = self.client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"

    def test_stats_endpoint_returns_expected_keys(self):
        resp = self.client.get("/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert "packets_received" in data
        assert "packets_dropped" in data
        assert "drop_rate" in data
        assert "bytes_saved_compression" in data
        assert "queue" in data
        assert "drops_by_priority" in data

    def test_stats_packets_received(self):
        resp = self.client.get("/stats")
        data = resp.json()
        assert data["packets_received"] >= 4

    def test_flows_endpoint(self):
        resp = self.client.get("/flows")
        assert resp.status_code == 200
        data = resp.json()
        assert "flow_count" in data
        assert "flows" in data
        assert isinstance(data["flows"], list)

    def test_flows_contain_scoring(self):
        resp = self.client.get("/flows")
        flows = resp.json()["flows"]
        if flows:
            f = flows[0]
            assert "latency_score" in f
            assert "bandwidth_score" in f
            assert "burst_score" in f

    def test_dashboard_returns_html(self):
        resp = self.client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "Smart Bandwidth Optimizer" in resp.text

    def test_stats_drops_by_priority_serialisable(self):
        """drops_by_priority keys must be plain strings, not enum reprs."""
        resp = self.client.get("/stats")
        data = resp.json()
        for key in data["drops_by_priority"]:
            assert isinstance(key, str)
            assert "<" not in key   # no Python repr leakage

    def test_enqueue_counts_serialisable(self):
        """enqueue_counts keys must be plain strings."""
        resp = self.client.get("/stats")
        data = resp.json()
        for key in data["queue"]["enqueue_counts"]:
            assert isinstance(key, str)


class TestWebSocket:
    def setup_method(self):
        self.app, self.optimizer = _make_test_app()
        self.client = TestClient(self.app)

    def test_websocket_sends_json(self):
        with self.client.websocket_connect("/ws") as ws:
            data = json.loads(ws.receive_text())
            assert "packets_received" in data
            assert "drop_rate" in data
            assert "queue_size" in data
            assert "active_flows" in data

    def test_websocket_ts_field(self):
        with self.client.websocket_connect("/ws") as ws:
            data = json.loads(ws.receive_text())
            assert "ts" in data
            assert isinstance(data["ts"], float)

    def test_websocket_enqueue_counts(self):
        with self.client.websocket_connect("/ws") as ws:
            data = json.loads(ws.receive_text())
            assert "enqueue_counts" in data
            counts = data["enqueue_counts"]
            # All priority class names should be plain strings
            for key in counts:
                assert isinstance(key, str)
