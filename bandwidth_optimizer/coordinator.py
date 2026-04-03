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
"""

from __future__ import annotations

import threading
import time
from typing import Dict, Optional

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

    # ── internal ──────────────────────────────────────────────────────────

    def _evict_stale(self) -> None:
        """Remove agents that haven't reported within TTL (must hold lock)."""
        cutoff = time.time() - self._ttl
        stale = [nid for nid, e in self._registry.items()
                 if e["last_seen"] < cutoff]
        for nid in stale:
            del self._registry[nid]
