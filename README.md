# Smart-Bandwidth-Optimizer

A lightweight, pure-Python system that optimises bandwidth by **prioritising important traffic**, **compressing data**, and **dropping unnecessary packets**.

Designed to run on a **router**, **local server**, or **ISP edge node**.

---

## Features

| Feature | Description |
|---|---|
| 🚦 Traffic Prioritization | Classifies packets (VoIP → CRITICAL, HTTPS → HIGH, BitTorrent → BACKGROUND) and serves them in strict priority order |
| 📦 Data Compression | Compresses large payloads with zlib/DEFLATE; skips already-compressed or tiny payloads automatically |
| 🗑️ Packet Dropping | Token-bucket rate limiting per priority class + RED (Random Early Detection) for queue-pressure drop |
| ⚙️ Configurable | Tune bandwidth limits, queue depth, compression level, RED thresholds, and per-class budgets |
| 📊 Statistics | Live counters for packets received/dropped, bytes saved, queue fill, and per-priority drop counts |

---

## Architecture

```
Incoming packet
       │
       ▼
┌─────────────────┐
│  TrafficClassifier │  → assigns TrafficPriority (CRITICAL/HIGH/MEDIUM/LOW/BACKGROUND)
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│   PacketFilter  │  → token-bucket + RED; drop if over rate or queue pressure
└────────┬────────┘
         │ (not dropped)
         ▼
┌─────────────────┐
│ PayloadCompressor│  → zlib compress if payload is large enough and compressible
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ PriorityScheduler│  → priority queue; dequeue in CRITICAL-first order
└─────────────────┘
```

### Modules

| Module | Responsibility |
|---|---|
| `bandwidth_optimizer/config.py` | `DeploymentMode`, `TrafficPriority`, `OptimizerConfig` |
| `bandwidth_optimizer/classifier.py` | `Packet`, `TrafficClassifier`, `ClassificationRule` |
| `bandwidth_optimizer/compressor.py` | `PayloadCompressor` – zlib-based compression/decompression |
| `bandwidth_optimizer/packet_filter.py` | `TokenBucket`, `PacketFilter` – rate limiting + RED |
| `bandwidth_optimizer/scheduler.py` | `PriorityScheduler` – thread-safe priority queue |
| `bandwidth_optimizer/optimizer.py` | `BandwidthOptimizer` – orchestrates all stages |

---

## Quick Start

```bash
# Install (no external dependencies needed at runtime)
pip install -e .

# Run demo packets through the optimizer
python main.py demo

# Specify deployment mode and bandwidth limit (5 MB/s, router mode)
python main.py --mode router --bw 5242880 demo

# Live simulation (Ctrl-C to stop)
python main.py simulate

# JSON stats snapshot
python main.py stats
```

### Programmatic usage

```python
from bandwidth_optimizer import (
    BandwidthOptimizer, OptimizerConfig, DeploymentMode, Packet
)

cfg = OptimizerConfig(
    mode=DeploymentMode.ROUTER,
    total_bandwidth_bps=5 * 1024 * 1024,  # 5 MB/s
)
optimizer = BandwidthOptimizer(config=cfg)

# Process a packet
packet = Packet(dst_port=443, protocol="tcp", payload=b"..." * 100)
result = optimizer.process(packet)

if not result.dropped:
    # Forward the (possibly compressed) packet
    forward(result.packet)

print(optimizer.stats())
```

---

## Traffic Priority Classes

| Priority | Value | Example protocols/ports |
|---|---|---|
| CRITICAL | 1 | VoIP SIP/RTP (5060, 5004), ICMP |
| HIGH | 2 | DNS (53), HTTPS (443), SSH (22), HTTP (80) |
| MEDIUM | 3 | SMTP/IMAP/POP3, XMPP |
| LOW | 4 | FTP (21), NTP (123) |
| BACKGROUND | 5 | BitTorrent (6881–6889) |

Default bandwidth budgets: CRITICAL 30 %, HIGH 30 %, MEDIUM 20 %, LOW 10 %, BACKGROUND 5 %.

---

## Deployment Modes

| Mode | Target |
|---|---|
| `router` | Home / enterprise router |
| `local_server` | Local server acting as a traffic proxy |
| `isp_edge` | ISP edge node for large-scale traffic shaping |

---

## Running Tests

```bash
pip install pytest
pytest tests/ -v
```

82 tests covering config, classification, compression, packet filtering (token bucket + RED), the priority scheduler, and the end-to-end optimizer pipeline.

---

## How Dropping Works

1. **Token Bucket** – each priority class has its own bucket that refills at a rate proportional to its bandwidth budget. A packet may only pass if the bucket has enough tokens (bytes). CRITICAL packets are never dropped by the rate limiter.
2. **RED (Random Early Detection)** – once the queue reaches `red_min_threshold` (default 50 % full), incoming non-CRITICAL packets are probabilistically dropped. Above `red_max_threshold` (default 90 %) all are dropped. This provides early backpressure before the queue fills completely.
