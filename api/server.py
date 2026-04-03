"""
Real-time telemetry API for the Smart Bandwidth Optimizer.

Provides:
* GET /stats              – one-shot JSON snapshot of optimizer statistics
* GET /flows              – list of all active flows with scoring
* WebSocket /ws           – push live stats every second to connected clients
* GET /                   – serve the built-in HTML dashboard
* GET /static/{file}      – serve static assets

Run with::

    uvicorn api.server:app
    # or via the CLI:
    python main.py serve

The FastAPI app is created by :func:`create_app` which accepts an optional
``BandwidthOptimizer`` instance.  If not provided it creates a default one
and runs a background simulation thread so the dashboard shows live data.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Header, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from bandwidth_optimizer import (
    BandwidthOptimizer,
    OptimizerConfig,
    Packet,
    TrafficPriority,
)
from bandwidth_optimizer.coordinator import AgentCoordinator
from bandwidth_optimizer.safety import HealthStatus, SafetyGuard
from bandwidth_optimizer.sla import BackpressureMonitor, SLAMonitor
from bandwidth_optimizer.trust import SIGNATURE_HEADER

_STATIC_DIR = Path(__file__).parent / "static"


# ─────────────────────────── connection manager ───────────────────────────────

class _ConnectionManager:
    """Manage active WebSocket connections and broadcast messages."""

    def __init__(self) -> None:
        self._connections: list[WebSocket] = []
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._connections.append(ws)

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._connections = [c for c in self._connections if c is not ws]

    async def broadcast(self, message: str) -> None:
        async with self._lock:
            dead = []
            for ws in self._connections:
                try:
                    await ws.send_text(message)
                except Exception:
                    dead.append(ws)
            self._connections = [c for c in self._connections if c not in dead]

    def connection_count(self) -> int:
        return len(self._connections)


# ─────────────────────────── background simulator ────────────────────────────

def _run_simulation(optimizer: BandwidthOptimizer, stop_event: threading.Event) -> None:
    """
    Background thread that feeds synthetic traffic into *optimizer*.

    Runs until *stop_event* is set.  This ensures the telemetry dashboard
    shows interesting live data even without a real packet capture backend.
    """
    traffic_mix = [
        (5060, "udp", 4),    # VoIP
        (443,  "tcp", 30),   # HTTPS
        (53,   "udp", 15),   # DNS
        (25,   "tcp", 8),    # Email
        (21,   "tcp", 4),    # FTP
        (6881, "tcp", 20),   # BitTorrent
        (9999, "tcp", 19),   # Unknown
    ]
    ports   = [t[0] for t in traffic_mix]
    protos  = [t[1] for t in traffic_mix]
    weights = [t[2] for t in traffic_mix]

    while not stop_event.is_set():
        burst = random.randint(3, 12)
        for _ in range(burst):
            idx = random.choices(range(len(ports)), weights=weights)[0]
            size = random.randint(64, 1500)
            pkt = Packet(
                src_ip=f"10.0.{random.randint(0, 255)}.{random.randint(1, 254)}",
                dst_ip="93.184.216.34",
                src_port=random.randint(1024, 65535),
                dst_port=ports[idx],
                protocol=protos[idx],
                payload=random.randbytes(size),
                size_bytes=size,
            )
            optimizer.process(pkt)

        # Drain some packets (simulate forwarding)
        for _ in range(burst // 2):
            optimizer.dequeue()

        time.sleep(0.05)


# ─────────────────────────── app factory ─────────────────────────────────────

def create_app(
    optimizer: Optional[BandwidthOptimizer] = None,
    coordinator: Optional[AgentCoordinator] = None,
) -> FastAPI:
    """
    Create and return the FastAPI application.

    Parameters
    ----------
    optimizer:
        An existing ``BandwidthOptimizer`` instance to expose via the API.
        If ``None`` a default instance is created and a background simulation
        thread is started automatically.
    coordinator:
        An optional ``AgentCoordinator`` instance.  When provided, the
        ``POST /agent/{node_id}/stats`` and ``GET /agents`` endpoints are
        activated so remote ``NodeAgent`` instances can register.
    """
    _optimizer = optimizer or BandwidthOptimizer(
        OptimizerConfig(compression_threshold_bytes=128)
    )
    _manager = _ConnectionManager()
    _stop_sim = threading.Event()
    _run_sim = optimizer is None
    _coordinator = coordinator  # may be None

    # SLA monitor (wraps optimizer if it's a bare BandwidthOptimizer or SLAMonitor)
    _sla_monitor: Optional[SLAMonitor] = (
        optimizer if isinstance(optimizer, SLAMonitor) else None
    )
    # Backpressure monitor always available
    _bp_monitor = BackpressureMonitor(_optimizer)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        sim_thread: Optional[threading.Thread] = None
        if _run_sim:
            _stop_sim.clear()
            sim_thread = threading.Thread(
                target=_run_simulation,
                args=(_optimizer, _stop_sim),
                daemon=True,
            )
            sim_thread.start()
        yield
        _stop_sim.set()

    app = FastAPI(
        title="Smart Bandwidth Optimizer",
        description="Real-time telemetry for the Smart Bandwidth Optimizer.",
        version="1.0.0",
        lifespan=lifespan,
    )

    # ── REST endpoints ────────────────────────────────────────────────────

    @app.get("/stats", summary="Get optimizer statistics")
    async def get_stats() -> dict:
        """Return a JSON snapshot of current optimizer statistics."""
        raw = _optimizer.stats()
        # Make enum keys serialisable
        raw["drops_by_priority"] = {
            k: v for k, v in raw["drops_by_priority"].items()
        }
        sched = raw["queue"]
        sched["enqueue_counts"] = {
            p.name: c for p, c in sched["enqueue_counts"].items()
        }
        sched["dequeue_counts"] = {
            p.name: c for p, c in sched["dequeue_counts"].items()
        }
        return raw

    @app.get("/flows", summary="List active flows")
    async def get_flows() -> dict:
        """Return a list of all active flows with scoring information."""
        flows = _optimizer.flow_tracker.all_flows()
        return {"flow_count": len(flows), "flows": flows}

    @app.get("/health", summary="Health check")
    async def health() -> dict:
        base: dict = {"status": "ok", "ws_clients": _manager.connection_count()}
        # If optimizer is wrapped in a SafetyGuard, include safety health
        if isinstance(_optimizer, SafetyGuard):
            base["safety"] = _optimizer.health().to_dict()
        return base

    @app.get("/sla", summary="SLA violation statistics")
    async def get_sla() -> dict:
        """
        Return SLA violation counts and recent breaches.

        Only populated when the optimizer is wrapped in an ``SLAMonitor``.
        Returns a zero-filled snapshot otherwise.
        """
        if _sla_monitor is not None:
            return _sla_monitor.sla_stats().to_dict()
        # No SLA monitor attached – return an informative empty response
        return {
            "total_violations": 0,
            "pipeline_violations_by_priority": {},
            "sojourn_violations_by_priority": {},
            "packets_expired": 0,
            "recent_violations": [],
            "note": "Attach an SLAMonitor to enable SLA tracking.",
        }

    @app.get("/backpressure", summary="Backpressure signal")
    async def get_backpressure() -> dict:
        """
        Return the current backpressure state.

        ``level`` is one of ``none`` / ``soft`` / ``hard``.
        ``recommended_throttle_pct`` tells upstream senders how much to slow down.
        """
        state = _bp_monitor.update()
        return state.to_dict()

    # ── multi-node agent endpoints ────────────────────────────────────────

    @app.post(
        "/agent/{node_id}/stats",
        summary="Receive heartbeat stats from a NodeAgent",
        include_in_schema=_coordinator is not None,
    )
    async def receive_agent_stats(
        node_id: str,
        request: Request,
        stats: dict,
    ) -> dict:
        """
        Accept a stats heartbeat from a remote ``NodeAgent``.

        When the coordinator is configured with ``require_auth=True``, the
        request must include a valid ``X-Agent-Signature`` header containing
        the HMAC-SHA256 of the raw request body.  Requests with a missing or
        invalid signature are rejected with HTTP 401.
        """
        if _coordinator is None:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="Coordinator not configured")

        if _coordinator.require_auth:
            signature = request.headers.get(SIGNATURE_HEADER, "")
            raw_body = await request.body()
            accepted = _coordinator.ingest_authenticated(
                node_id, raw_body, signature, stats
            )
            if not accepted:
                from fastapi import HTTPException
                raise HTTPException(
                    status_code=401,
                    detail="Invalid or missing agent signature",
                )
            return {"ok": True, "node_id": node_id}

        _coordinator.ingest(node_id, stats)
        return {"ok": True, "node_id": node_id}

    @app.get(
        "/agents",
        summary="List all registered NodeAgents",
        include_in_schema=_coordinator is not None,
    )
    async def get_agents() -> dict:
        """
        Return all live agents registered with the coordinator.

        Agents that haven't sent a heartbeat within the TTL are excluded.
        """
        if _coordinator is None:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="Coordinator not configured")
        return _coordinator.all_agents()

    # ── WebSocket streaming ───────────────────────────────────────────────

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket) -> None:
        """
        Stream live stats to the client every second.

        The server pushes a JSON payload each second; the client can also
        send ``"ping"`` to receive an immediate ``"pong"``.
        """
        await _manager.connect(ws)
        try:
            while True:
                raw = _optimizer.stats()
                payload = {
                    "ts": time.time(),
                    "packets_received": raw["packets_received"],
                    "packets_dropped": raw["packets_dropped"],
                    "drop_rate": round(raw["drop_rate"] * 100, 1),
                    "bytes_saved": raw["bytes_saved_compression"],
                    "queue_size": raw["queue"]["current_queue_size"],
                    "queue_fill_pct": round(raw["queue"]["fill_ratio"] * 100, 1),
                    "active_flows": raw["flows"]["active"],
                    "drops_by_priority": raw["drops_by_priority"],
                    "enqueue_counts": {
                        p.name: c
                        for p, c in raw["queue"]["enqueue_counts"].items()
                    },
                }
                await ws.send_text(json.dumps(payload))
                await asyncio.sleep(1.0)
        except WebSocketDisconnect:
            pass
        finally:
            await _manager.disconnect(ws)

    # ── static files / dashboard ──────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def dashboard() -> FileResponse:
        return FileResponse(_STATIC_DIR / "index.html")

    if _STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    return app


# Module-level app instance (used by uvicorn directly)
app = create_app()
