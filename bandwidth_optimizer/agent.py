"""
Multi-node agent layer.

Turns the optimizer into a distributed SD-WAN-lite node that can:

* Run as a standalone agent with its own optimizer pipeline
* Report live stats to a central ``AgentCoordinator`` via HTTP heartbeats
* Be queried independently via the telemetry API

Architecture::

    ┌──────────────┐        HTTP POST /agent/<node_id>/stats
    │  NodeAgent A │ ──────────────────────────────────────────►┐
    └──────────────┘                                             │
    ┌──────────────┐        HTTP POST /agent/<node_id>/stats     │  AgentCoordinator
    │  NodeAgent B │ ──────────────────────────────────────────►─┤  (aggregates all
    └──────────────┘                                             │   node stats, exposes
    ┌──────────────┐        HTTP POST /agent/<node_id>/stats     │   /agents endpoint)
    │  NodeAgent C │ ──────────────────────────────────────────►┘
    └──────────────┘

Usage – standalone (no coordinator)::

    from bandwidth_optimizer.agent import NodeAgent, AgentConfig

    cfg = AgentConfig(node_id="edge-01")
    agent = NodeAgent(optimizer, cfg)
    agent.start()

Usage – with coordinator::

    # On the coordinator host:
    from bandwidth_optimizer.coordinator import AgentCoordinator
    coordinator = AgentCoordinator()

    # On each edge node:
    cfg = AgentConfig(node_id="edge-01",
                      coordinator_url="http://coordinator:8000")
    agent = NodeAgent(optimizer, cfg)
    agent.start()

The coordinator is also embedded in the FastAPI app when ``serve`` is started
with ``--coordinator`` flag or when multiple agents push stats to the same
server.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional
from urllib.request import Request, urlopen
from urllib.error import URLError


# ─────────────────────────── serialisation helper ────────────────────────────

def _make_serialisable(obj: Any) -> Any:
    """
    Recursively convert *obj* to a JSON-serialisable structure.

    Handles dicts with non-string keys (e.g. ``TrafficPriority`` enums)
    by converting them to their ``name`` or ``str()`` representation.
    """
    if isinstance(obj, dict):
        return {
            (k.name if isinstance(k, Enum) else str(k) if not isinstance(k, (str, int, float, bool, type(None))) else k):
            _make_serialisable(v)
            for k, v in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        return [_make_serialisable(i) for i in obj]
    if isinstance(obj, Enum):
        return obj.value
    return obj


# ─────────────────────────── agent config ────────────────────────────────────

@dataclass
class AgentConfig:
    """
    Configuration for a ``NodeAgent``.

    Attributes
    ----------
    node_id:
        Unique identifier for this node (e.g., ``"edge-01"`` or hostname).
    coordinator_url:
        Base URL of the ``AgentCoordinator`` (e.g., ``"http://10.0.0.1:8000"``).
        Empty string = standalone mode (no heartbeat sent).
    heartbeat_interval:
        Seconds between heartbeat POSTs to the coordinator.
    tags:
        Arbitrary key/value metadata attached to every heartbeat.
    auth_secret:
        Shared secret used to sign heartbeat payloads (HMAC-SHA256).
        Empty string = unsigned (no authentication).
        Must match the ``auth_secret`` configured on the coordinator.
    """
    node_id: str
    coordinator_url: str = ""
    heartbeat_interval: float = 5.0
    tags: dict = field(default_factory=dict)
    auth_secret: str = ""


# ─────────────────────────── node agent ──────────────────────────────────────

class NodeAgent:
    """
    Wraps a ``BandwidthOptimizer`` with node identity and optional heartbeat.

    The agent adds a ``node_id`` to all stats reports and, when
    ``coordinator_url`` is set, periodically POSTs stats to the coordinator.

    The heartbeat thread is a daemon so it does not block process exit.
    """

    def __init__(self, optimizer, config: AgentConfig) -> None:
        self._optimizer = optimizer
        self._config = config
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._heartbeat_count: int = 0
        self._heartbeat_errors: int = 0
        self._last_heartbeat_time: Optional[float] = None

    # ── lifecycle ─────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the heartbeat thread (no-op if already running)."""
        with self._lock:
            if self._running:
                return
            self._running = True

        if self._config.coordinator_url:
            self._thread = threading.Thread(
                target=self._heartbeat_loop,
                daemon=True,
                name=f"agent-{self._config.node_id}",
            )
            self._thread.start()

    def stop(self) -> None:
        """Stop the heartbeat thread."""
        with self._lock:
            self._running = False

    def __enter__(self) -> "NodeAgent":
        self.start()
        return self

    def __exit__(self, *_) -> None:
        self.stop()

    # ── stats ─────────────────────────────────────────────────────────────

    def stats(self) -> dict:
        """Return optimizer stats enriched with node identity."""
        base = self._optimizer.stats()
        base["node_id"] = self._config.node_id
        base["tags"] = self._config.tags
        base["coordinator_url"] = self._config.coordinator_url or None
        with self._lock:
            base["heartbeat"] = {
                "count": self._heartbeat_count,
                "errors": self._heartbeat_errors,
                "last_sent": self._last_heartbeat_time,
            }
        return base

    # ── delegate optimizer interface ──────────────────────────────────────

    def process(self, packet):
        return self._optimizer.process(packet)

    def dequeue(self):
        return self._optimizer.dequeue()

    @property
    def optimizer(self):
        return self._optimizer

    @property
    def config(self) -> AgentConfig:
        return self._config

    # ── heartbeat ─────────────────────────────────────────────────────────

    def _heartbeat_loop(self) -> None:
        while True:
            with self._lock:
                if not self._running:
                    break
            time.sleep(self._config.heartbeat_interval)
            with self._lock:
                if not self._running:
                    break
            self._send_heartbeat()

    def _send_heartbeat(self) -> None:
        """POST current stats to the coordinator, optionally signed."""
        from .trust import sign_payload, SIGNATURE_HEADER

        url = (
            f"{self._config.coordinator_url.rstrip('/')}"
            f"/agent/{self._config.node_id}/stats"
        )
        payload = json.dumps(
            _make_serialisable(self.stats())
        ).encode("utf-8")

        headers = {"Content-Type": "application/json"}
        if self._config.auth_secret:
            headers[SIGNATURE_HEADER] = sign_payload(
                self._config.auth_secret, payload
            )

        req = Request(url, data=payload, headers=headers, method="POST")
        try:
            with urlopen(req, timeout=5) as _:
                pass
            with self._lock:
                self._heartbeat_count += 1
                self._last_heartbeat_time = time.time()
        except (URLError, OSError):
            with self._lock:
                self._heartbeat_errors += 1
