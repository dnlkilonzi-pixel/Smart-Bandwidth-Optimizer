#!/usr/bin/env python3
"""
Smart Bandwidth Optimizer – CLI entry point.

Examples
--------
Run an interactive simulation with default settings::

    python main.py simulate

Run with a specific deployment mode and bandwidth limit::

    python main.py simulate --mode router --bw 5242880

Show a one-shot stats summary after processing built-in demo packets::

    python main.py demo
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import List

from bandwidth_optimizer import (
    BandwidthOptimizer,
    DeploymentMode,
    OptimizerConfig,
    Packet,
    TrafficPriority,
)
from bandwidth_optimizer.classifier import TrafficClassifier
from bandwidth_optimizer.policy import PolicyLoadError, PolicyLoader


# ─────────────────────────── helpers ─────────────────────────────────────────

def _build_optimizer(args: argparse.Namespace, **cfg_kwargs) -> BandwidthOptimizer:
    """Build an optimizer, optionally loading a YAML policy file."""
    mode = DeploymentMode(args.mode)
    cfg = OptimizerConfig(mode=mode, total_bandwidth_bps=args.bw, **cfg_kwargs)
    optimizer = BandwidthOptimizer(config=cfg)

    policy_path = getattr(args, "policy", None)
    if policy_path:
        try:
            policy = PolicyLoader.load_file(policy_path)
        except (OSError, PolicyLoadError) as exc:
            print(f"Error loading policy file: {exc}", file=sys.stderr)
            sys.exit(1)
        optimizer.classifier._rules = policy.to_classification_rules()
        optimizer.classifier._default_priority = policy.default_priority
        print(f"  Loaded {len(policy.rules)} rules from {policy_path}")

    return optimizer


# ─────────────────────────── demo helpers ────────────────────────────────────

def _make_demo_packets() -> List[Packet]:
    """Return a small set of representative packets for demo/testing."""
    return [
        # VoIP
        Packet(src_ip="192.168.1.10", dst_ip="10.0.0.1",
               src_port=5004, dst_port=5004, protocol="udp",
               payload=b"\x80\x60" + b"\x00" * 160),
        # HTTPS web request
        Packet(src_ip="192.168.1.20", dst_ip="93.184.216.34",
               src_port=54321, dst_port=443, protocol="tcp",
               payload=b"GET / HTTP/1.1\r\nHost: example.com\r\n\r\n" * 5),
        # DNS query
        Packet(src_ip="192.168.1.5", dst_ip="8.8.8.8",
               src_port=32000, dst_port=53, protocol="udp",
               payload=b"\x12\x34\x01\x00" + b"\x00" * 28),
        # Email (SMTP)
        Packet(src_ip="192.168.1.30", dst_ip="10.0.0.5",
               src_port=45000, dst_port=25, protocol="tcp",
               payload=(b"EHLO localhost\r\nMAIL FROM:<user@example.com>\r\n"
                        b"DATA\r\n" + b"X" * 400)),
        # Software update (FTP-data)
        Packet(src_ip="10.0.0.2", dst_ip="192.168.1.50",
               src_port=20, dst_port=54000, protocol="tcp",
               payload=b"PKG_UPDATE_v3.2.1_" + b"\xAB\xCD" * 200),
        # BitTorrent
        Packet(src_ip="192.168.1.99", dst_ip="45.67.89.0",
               src_port=6881, dst_port=6881, protocol="tcp",
               payload=b"d1:ad2:id20:" + b"\x00" * 200),
        # Unknown traffic → default MEDIUM
        Packet(src_ip="192.168.1.77", dst_ip="172.16.0.1",
               src_port=9999, dst_port=9999, protocol="tcp",
               payload=b"SOME_CUSTOM_PROTOCOL_DATA" * 20),
    ]


# ─────────────────────────── commands ────────────────────────────────────────

def cmd_demo(args: argparse.Namespace) -> None:
    """Process demo packets and print a stats report."""
    mode = DeploymentMode(args.mode)
    optimizer = _build_optimizer(
        args, max_queue_size=64, compression_threshold_bytes=64
    )

    packets = _make_demo_packets()
    print(f"\n{'=' * 60}")
    print(f"  Smart Bandwidth Optimizer – Demo")
    print(f"  Mode: {mode.value}  |  Bandwidth: {args.bw:,} B/s")
    print(f"{'=' * 60}\n")

    for pkt in packets:
        result = optimizer.process(pkt)
        prio = pkt.priority.name if pkt.priority else "?"
        status = "DROPPED" if result.dropped else "QUEUED "
        comp = f" [compressed {result.bytes_saved:+d}B]" if result.compressed else ""
        reason = f" ({result.drop_reason})" if result.dropped else ""
        print(
            f"  [{status}] {pkt.src_ip}:{pkt.src_port} → "
            f"{pkt.dst_ip}:{pkt.dst_port}/{pkt.protocol.upper()}"
            f"  priority={prio}{comp}{reason}"
        )

    # Drain and display forwarding order
    print(f"\n  Forwarding order (priority queue drain):")
    idx = 1
    for pkt in optimizer.drain():
        print(f"    {idx}. {pkt.priority.name:10s}  "
              f"{pkt.src_ip}:{pkt.src_port} → "
              f"{pkt.dst_ip}:{pkt.dst_port}")
        idx += 1

    # Stats
    print(f"\n  Statistics:")
    stats = optimizer.stats()
    for key, val in stats.items():
        if isinstance(val, dict):
            print(f"    {key}:")
            for k2, v2 in val.items():
                print(f"      {k2}: {v2}")
        else:
            print(f"    {key}: {val}")
    print()


def cmd_simulate(args: argparse.Namespace) -> None:
    """
    Run a continuous simulation that prints live queue/drop statistics.
    Press Ctrl-C to stop.
    """
    import random

    optimizer = _build_optimizer(
        args, max_queue_size=128, compression_threshold_bytes=128
    )

    # Traffic mix: (dst_port, protocol, weight)
    traffic_templates = [
        (5060, "udp", 5),    # VoIP
        (443,  "tcp", 30),   # HTTPS
        (53,   "udp", 15),   # DNS
        (25,   "tcp", 10),   # Email
        (21,   "tcp", 5),    # FTP
        (6881, "tcp", 20),   # BitTorrent
        (9999, "tcp", 15),   # Unknown
    ]
    ports   = [t[0] for t in traffic_templates]
    protos  = [t[1] for t in traffic_templates]
    weights = [t[2] for t in traffic_templates]

    print(f"\nSimulating traffic  (mode={mode.value}, bw={args.bw:,} B/s)")
    print("Press Ctrl-C to stop.\n")

    iteration = 0
    try:
        while True:
            # Generate a random burst of packets
            burst = random.randint(5, 20)
            for _ in range(burst):
                idx = random.choices(range(len(ports)), weights=weights)[0]
                size = random.randint(64, 1500)
                pkt = Packet(
                    src_ip=f"10.0.{random.randint(0,255)}.{random.randint(1,254)}",
                    dst_ip="93.184.216.34",
                    src_port=random.randint(1024, 65535),
                    dst_port=ports[idx],
                    protocol=protos[idx],
                    payload=bytes(random.getrandbits(8) for _ in range(size)),
                    size_bytes=size,
                )
                optimizer.process(pkt)

            # Drain half the queue each iteration (simulate forwarding)
            for _ in range(burst // 2):
                optimizer.dequeue()

            iteration += 1
            if iteration % 10 == 0:
                s = optimizer.stats()
                print(
                    f"  iter={iteration:4d}  "
                    f"recv={s['packets_received']:5d}  "
                    f"drop={s['packets_dropped']:4d} "
                    f"({s['drop_rate']:.1%})  "
                    f"queue={s['queue']['current_queue_size']:3d}  "
                    f"saved={s['bytes_saved_compression']:,}B"
                )

            time.sleep(args.interval)

    except KeyboardInterrupt:
        print("\nSimulation stopped.")
        print(json.dumps(optimizer.stats(), indent=2, default=str))


def cmd_stats(args: argparse.Namespace) -> None:
    """Print a JSON stats snapshot after running demo packets."""
    optimizer = _build_optimizer(args)
    for pkt in _make_demo_packets():
        optimizer.process(pkt)
    print(json.dumps(optimizer.stats(), indent=2, default=str))


def cmd_serve(args: argparse.Namespace) -> None:
    """Start the FastAPI telemetry server with a live dashboard."""
    try:
        import uvicorn
    except ImportError:
        print(
            "uvicorn is required to run the server.\n"
            "Install it with: pip install uvicorn[standard]",
            file=sys.stderr,
        )
        sys.exit(1)

    from api.server import create_app

    optimizer = _build_optimizer(args)
    app = create_app(optimizer=optimizer)

    print(f"\n  Smart Bandwidth Optimizer – Telemetry Server")
    print(f"  Dashboard: http://{args.host}:{args.port}/")
    print(f"  Stats API: http://{args.host}:{args.port}/stats")
    print(f"  Flows API: http://{args.host}:{args.port}/flows")
    print(f"  WebSocket: ws://{args.host}:{args.port}/ws")
    print(f"  Press Ctrl-C to stop.\n")

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


# ─────────────────────────── CLI parser ──────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bandwidth-optimizer",
        description="Smart Bandwidth Optimizer – prioritize, compress, and drop packets.",
    )
    parser.add_argument(
        "--mode",
        choices=[m.value for m in DeploymentMode],
        default=DeploymentMode.LOCAL_SERVER.value,
        help="Deployment mode (default: local_server)",
    )
    parser.add_argument(
        "--bw",
        type=int,
        default=10 * 1024 * 1024,
        metavar="BYTES_PER_SEC",
        help="Total bandwidth limit in bytes/second (default: 10485760 = 10 MB/s)",
    )
    parser.add_argument(
        "--policy",
        metavar="FILE",
        default=None,
        help="Path to a YAML policy file (overrides built-in classification rules)",
    )

    sub = parser.add_subparsers(dest="command")

    sub.add_parser("demo", help="Process built-in demo packets and print results.")
    sub.add_parser("stats", help="Print JSON stats after demo run.")

    sim_p = sub.add_parser("simulate", help="Run a continuous traffic simulation.")
    sim_p.add_argument(
        "--interval",
        type=float,
        default=0.1,
        metavar="SECONDS",
        help="Seconds between simulation iterations (default: 0.1)",
    )

    srv_p = sub.add_parser(
        "serve",
        help="Start the FastAPI telemetry server with a live dashboard.",
    )
    srv_p.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    srv_p.add_argument("--port", type=int, default=8000, help="Bind port (default: 8000)")

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "demo":
        cmd_demo(args)
    elif args.command == "simulate":
        cmd_simulate(args)
    elif args.command == "stats":
        cmd_stats(args)
    elif args.command == "serve":
        cmd_serve(args)
    else:
        parser.print_help()

    return 0


if __name__ == "__main__":
    sys.exit(main())
