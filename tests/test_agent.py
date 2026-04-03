"""
Tests for bandwidth_optimizer.agent + bandwidth_optimizer.coordinator
(Multi-node Agent Vision)
"""

import json
import threading
import time

import pytest

from bandwidth_optimizer import BandwidthOptimizer, Packet
from bandwidth_optimizer.agent import AgentConfig, NodeAgent
from bandwidth_optimizer.coordinator import AgentCoordinator


def _make_packet() -> Packet:
    return Packet(dst_port=443, protocol="tcp",
                  payload=b"X" * 100, size_bytes=100)


# ── AgentConfig ───────────────────────────────────────────────────────────────

class TestAgentConfig:
    def test_required_node_id(self):
        cfg = AgentConfig(node_id="test-01")
        assert cfg.node_id == "test-01"

    def test_defaults(self):
        cfg = AgentConfig(node_id="n1")
        assert cfg.coordinator_url == ""
        assert cfg.heartbeat_interval == 5.0
        assert cfg.tags == {}

    def test_custom_tags(self):
        cfg = AgentConfig(node_id="n1", tags={"region": "us-east"})
        assert cfg.tags["region"] == "us-east"


# ── NodeAgent ─────────────────────────────────────────────────────────────────

class TestNodeAgent:
    def test_process_delegates_to_optimizer(self):
        opt = BandwidthOptimizer()
        cfg = AgentConfig(node_id="edge-01")
        agent = NodeAgent(opt, cfg)
        result = agent.process(_make_packet())
        assert result is not None

    def test_stats_includes_node_id(self):
        opt = BandwidthOptimizer()
        cfg = AgentConfig(node_id="edge-42")
        agent = NodeAgent(opt, cfg)
        s = agent.stats()
        assert s["node_id"] == "edge-42"

    def test_stats_includes_tags(self):
        opt = BandwidthOptimizer()
        cfg = AgentConfig(node_id="n1", tags={"dc": "london"})
        agent = NodeAgent(opt, cfg)
        s = agent.stats()
        assert s["tags"]["dc"] == "london"

    def test_stats_includes_heartbeat_info(self):
        opt = BandwidthOptimizer()
        cfg = AgentConfig(node_id="n1")
        agent = NodeAgent(opt, cfg)
        s = agent.stats()
        assert "heartbeat" in s
        assert s["heartbeat"]["count"] == 0

    def test_no_heartbeat_thread_without_coordinator_url(self):
        opt = BandwidthOptimizer()
        cfg = AgentConfig(node_id="n1", coordinator_url="")
        agent = NodeAgent(opt, cfg)
        agent.start()
        time.sleep(0.05)
        # No thread should be running for heartbeats
        assert agent._thread is None
        agent.stop()

    def test_context_manager(self):
        opt = BandwidthOptimizer()
        cfg = AgentConfig(node_id="n1")
        with NodeAgent(opt, cfg) as agent:
            assert agent._running
        assert not agent._running

    def test_double_start_is_idempotent(self):
        opt = BandwidthOptimizer()
        cfg = AgentConfig(node_id="n1")
        agent = NodeAgent(opt, cfg)
        agent.start()
        agent.start()   # second call is no-op
        assert agent._running
        agent.stop()

    def test_optimizer_property(self):
        opt = BandwidthOptimizer()
        cfg = AgentConfig(node_id="n1")
        agent = NodeAgent(opt, cfg)
        assert agent.optimizer is opt

    def test_config_property(self):
        opt = BandwidthOptimizer()
        cfg = AgentConfig(node_id="n1", tags={"env": "prod"})
        agent = NodeAgent(opt, cfg)
        assert agent.config is cfg

    def test_dequeue_delegates(self):
        opt = BandwidthOptimizer()
        cfg = AgentConfig(node_id="n1")
        agent = NodeAgent(opt, cfg)
        agent.process(_make_packet())
        pkt = agent.dequeue()
        # Either a Packet or None (if dropped), never raises
        assert pkt is None or isinstance(pkt, Packet)

    def test_heartbeat_error_counted_on_bad_url(self):
        """Agent with an unreachable coordinator records heartbeat errors."""
        opt = BandwidthOptimizer()
        cfg = AgentConfig(
            node_id="n1",
            coordinator_url="http://127.0.0.1:19999",  # nothing listening
            heartbeat_interval=10.0,  # long interval so thread doesn't fire
        )
        agent = NodeAgent(opt, cfg)
        agent.start()
        # Directly invoke _send_heartbeat to avoid timing dependence
        agent._send_heartbeat()
        agent.stop()
        s = agent.stats()
        assert s["heartbeat"]["errors"] == 1


# ── AgentCoordinator ─────────────────────────────────────────────────────────

class TestAgentCoordinator:
    def test_ingest_and_retrieve(self):
        coord = AgentCoordinator()
        coord.ingest("node-01", {"packets_received": 100})
        result = coord.all_agents()
        assert result["agent_count"] == 1
        assert "node-01" in result["agents"]

    def test_ingest_updates_existing(self):
        coord = AgentCoordinator()
        coord.ingest("node-01", {"packets_received": 100})
        coord.ingest("node-01", {"packets_received": 200})
        agent_stats = coord.get_agent("node-01")
        assert agent_stats["packets_received"] == 200

    def test_multiple_agents(self):
        coord = AgentCoordinator()
        for i in range(5):
            coord.ingest(f"node-{i:02d}", {"idx": i})
        assert coord.agent_count() == 5

    def test_get_agent_returns_none_for_unknown(self):
        coord = AgentCoordinator()
        assert coord.get_agent("ghost") is None

    def test_remove_agent(self):
        coord = AgentCoordinator()
        coord.ingest("n1", {})
        coord.remove("n1")
        assert coord.agent_count() == 0
        assert coord.get_agent("n1") is None

    def test_ttl_eviction(self):
        coord = AgentCoordinator(agent_ttl=0.05)
        coord.ingest("n1", {})
        assert coord.agent_count() == 1
        time.sleep(0.15)
        # Next call should trigger eviction
        assert coord.agent_count() == 0

    def test_all_agents_keys(self):
        coord = AgentCoordinator()
        coord.ingest("n1", {"val": 1})
        result = coord.all_agents()
        assert "agent_count" in result
        assert "agents" in result
        entry = result["agents"]["n1"]
        assert "node_id" in entry
        assert "last_seen" in entry
        assert "stats" in entry

    def test_thread_safe_concurrent_ingest(self):
        coord = AgentCoordinator()
        errors = []

        def worker(node_id: str):
            for i in range(50):
                try:
                    coord.ingest(node_id, {"i": i})
                except Exception as e:  # noqa: BLE001
                    errors.append(e)

        threads = [threading.Thread(target=worker, args=(f"n{j}",))
                   for j in range(10)]
        for t in threads: t.start()
        for t in threads: t.join()

        assert errors == []
        assert coord.agent_count() == 10


# ── Integration: agent stats reach coordinator via API ──────────────────────

class TestAgentCoordinatorApiIntegration:
    def test_coordinator_api_endpoint(self):
        """The /agents API endpoint returns registered agents."""
        from fastapi.testclient import TestClient
        from api.server import create_app

        opt = BandwidthOptimizer()
        coord = AgentCoordinator()
        coord.ingest("node-01", {"packets_received": 42})
        coord.ingest("node-02", {"packets_received": 7})

        app = create_app(optimizer=opt, coordinator=coord)
        client = TestClient(app)

        resp = client.get("/agents")
        assert resp.status_code == 200
        data = resp.json()
        assert data["agent_count"] == 2
        assert "node-01" in data["agents"]

    def test_agent_heartbeat_endpoint(self):
        """POST /agent/<id>/stats stores the stats in the coordinator."""
        from fastapi.testclient import TestClient
        from api.server import create_app

        opt = BandwidthOptimizer()
        coord = AgentCoordinator()
        app = create_app(optimizer=opt, coordinator=coord)
        client = TestClient(app)

        payload = {"node_id": "edge-01", "packets_received": 999}
        resp = client.post("/agent/edge-01/stats", json=payload)
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        # Check it was stored
        resp2 = client.get("/agents")
        assert "edge-01" in resp2.json()["agents"]

    def test_agents_404_without_coordinator(self):
        """Without a coordinator the /agents endpoint returns 404."""
        from fastapi.testclient import TestClient
        from api.server import create_app

        app = create_app()   # no coordinator
        client = TestClient(app)
        resp = client.get("/agents")
        assert resp.status_code == 404
