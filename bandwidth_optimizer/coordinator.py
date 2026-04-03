"""
Agent coordinator – aggregates stats from multiple ``NodeAgent`` instances.

The coordinator maintains a live registry of connected agents.  Each agent
periodically POSTs its stats to ``POST /agent/<node_id>/stats``.  The
coordinator exposes an aggregate view at ``GET /agents``.

The coordinator is embedded in the FastAPI server (``api/server.py``) and
can also be used standalone in tests or in-process multi-node scenarios::

    from bandwidth_optimizer.coordinator import AgentCoordinator

    coord = AgentCoordinator(agent_ttl=30.0)
    coord.ingest("node-01", {"packets_received": 1000, ...})
    print(coord.all_agents())

Authenticated mode::

    coord = AgentCoordinator(require_auth=True, auth_secret="my-shared-secret")
    # The API endpoint will call coord.ingest_authenticated(node_id, body, sig)

Multi-node value coordination
------------------------------
When PVM is active, each node includes a ``value`` sub-dict in its stats
heartbeat.  The coordinator aggregates these into a fleet-wide value view::

    coord.fleet_value_summary()
    # → {
    #     "fleet_value_efficiency_pct": 94.2,
    #     "fleet_value_delivered_per_sec": 1234.5,
    #     "fleet_value_lost_per_sec": 72.1,
    #     "best_node": "node-02",
    #     "worst_node": "node-05",
    #     "nodes": { "node-01": {...}, ... },
    # }
"""

from __future__ import annotations

import threading
import time
from typing import Dict, List, Optional

from .trust import verify_payload, SIGNATURE_HEADER


class AgentCoordinator:
    """
    Thread-safe in-memory registry of connected ``NodeAgent`` stats.

    Agents are expired automatically when no heartbeat has been received
    within ``agent_ttl`` seconds.

    Attributes
    ----------
    agent_ttl:
        Seconds after which a silent agent is considered offline and
        removed from the registry.
    require_auth:
        When ``True``, ``ingest_authenticated()`` must be used; plain
        ``ingest()`` calls are still accepted but the API endpoint will
        enforce signature verification.
    auth_secret:
        Shared secret used to verify HMAC-SHA256 signatures on incoming
        heartbeat payloads.  Only relevant when ``require_auth=True``.
    """

    def __init__(
        self,
        agent_ttl: float = 60.0,
        require_auth: bool = False,
        auth_secret: str = "",
    ) -> None:
        self._ttl = agent_ttl
        self._require_auth = require_auth
        self._auth_secret = auth_secret
        self._lock = threading.Lock()
        # node_id → {"stats": {...}, "last_seen": float}
        self._registry: Dict[str, dict] = {}

    # ── public API ─────────────────────────────────────────────────────────

    def ingest(self, node_id: str, stats: dict) -> None:
        """
        Register or update the stats for *node_id*.

        Called by the FastAPI endpoint when an agent POSTs a heartbeat.
        """
        with self._lock:
            self._registry[node_id] = {
                "stats": stats,
                "last_seen": time.time(),
            }

    def ingest_authenticated(
        self,
        node_id: str,
        raw_body: bytes,
        signature: str,
        stats: dict,
    ) -> bool:
        """
        Verify *signature* over *raw_body* then ingest *stats*.

        :returns: ``True`` if accepted, ``False`` if signature verification
                  failed (caller should return HTTP 401).
        """
        if self._require_auth and self._auth_secret:
            if not verify_payload(self._auth_secret, raw_body, signature):
                return False
        self.ingest(node_id, stats)
        return True

    @property
    def require_auth(self) -> bool:
        return self._require_auth

    def all_agents(self) -> dict:
        """
        Return a summary of all live agents.

        Expired agents (silent for > ``agent_ttl`` s) are evicted first.
        """
        with self._lock:
            self._evict_stale()
            return {
                "agent_count": len(self._registry),
                "agents": {
                    node_id: {
                        "node_id": node_id,
                        "last_seen": entry["last_seen"],
                        "stats": entry["stats"],
                    }
                    for node_id, entry in self._registry.items()
                },
            }

    def get_agent(self, node_id: str) -> Optional[dict]:
        """Return the latest stats for a specific agent, or None."""
        with self._lock:
            entry = self._registry.get(node_id)
            return entry["stats"] if entry else None

    def agent_count(self) -> int:
        with self._lock:
            self._evict_stale()
            return len(self._registry)

    def remove(self, node_id: str) -> None:
        """Explicitly remove an agent from the registry."""
        with self._lock:
            self._registry.pop(node_id, None)

    # ── multi-node value coordination ──────────────────────────────────────

    def fleet_value_summary(self) -> dict:
        """
        Aggregate PVM value metrics across all live nodes.

        Nodes that include a ``"value"`` sub-dict in their stats heartbeat
        (i.e., nodes running in PVM mode) are included in the fleet-wide
        value summary.  Non-PVM nodes are listed in ``"non_pvm_nodes"``.

        Returns
        -------
        dict with keys:

        * ``fleet_value_efficiency_pct``  — weighted average efficiency
        * ``fleet_value_delivered_per_sec`` — sum of delivered value/s
        * ``fleet_value_lost_per_sec``    — sum of lost value/s
        * ``best_node``                   — node_id with highest efficiency
        * ``worst_node``                  — node_id with lowest efficiency
        * ``pvm_node_count``              — nodes reporting value metrics
        * ``non_pvm_nodes``               — list of node IDs without PVM data
        * ``nodes``                       — per-node value sub-dict

        Usage::

            summary = coord.fleet_value_summary()
            if summary["fleet_value_efficiency_pct"] < 90.0:
                alert("Fleet value efficiency below 90%!")
        """
        with self._lock:
            self._evict_stale()
            nodes_value: Dict[str, dict] = {}
            non_pvm: List[str] = []
            for node_id, entry in self._registry.items():
                v = entry["stats"].get("value")
                if v:
                    nodes_value[node_id] = v
                else:
                    non_pvm.append(node_id)

        if not nodes_value:
            return {
                "fleet_value_efficiency_pct": None,
                "fleet_value_delivered_per_sec": 0.0,
                "fleet_value_lost_per_sec": 0.0,
                "best_node": None,
                "worst_node": None,
                "pvm_node_count": 0,
                "non_pvm_nodes": non_pvm,
                "nodes": {},
                "note": "No PVM-enabled nodes found.  Enable PVM mode on at least one node.",
            }

        total_delivered = sum(
            v.get("value_delivered_per_sec", 0.0) for v in nodes_value.values()
        )
        total_lost = sum(
            v.get("value_lost_per_sec", 0.0) for v in nodes_value.values()
        )
        total_flow = total_delivered + total_lost
        fleet_efficiency = (
            100.0 * total_delivered / total_flow if total_flow > 0 else 100.0
        )

        efficiencies = {
            nid: v.get("value_efficiency_pct", 100.0)
            for nid, v in nodes_value.items()
        }
        best_node = max(efficiencies, key=efficiencies.__getitem__)
        worst_node = min(efficiencies, key=efficiencies.__getitem__)

        return {
            "fleet_value_efficiency_pct": round(fleet_efficiency, 2),
            "fleet_value_delivered_per_sec": round(total_delivered, 4),
            "fleet_value_lost_per_sec": round(total_lost, 4),
            "best_node": best_node,
            "worst_node": worst_node,
            "pvm_node_count": len(nodes_value),
            "non_pvm_nodes": non_pvm,
            "nodes": {
                nid: {
                    "value_efficiency_pct": round(v.get("value_efficiency_pct", 100.0), 2),
                    "value_delivered_per_sec": round(v.get("value_delivered_per_sec", 0.0), 4),
                    "value_lost_per_sec": round(v.get("value_lost_per_sec", 0.0), 4),
                }
                for nid, v in nodes_value.items()
            },
        }

    # ── internal ──────────────────────────────────────────────────────────

    def _evict_stale(self) -> None:
        """Remove agents that haven't reported within TTL (must hold lock)."""
        cutoff = time.time() - self._ttl
        stale = [nid for nid, e in self._registry.items()
                 if e["last_seen"] < cutoff]
        for nid in stale:
            del self._registry[nid]
