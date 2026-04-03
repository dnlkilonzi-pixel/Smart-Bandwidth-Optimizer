# Smart-Bandwidth-Optimizer

A production-grade Python system that optimises bandwidth by **prioritising important traffic**, **compressing data**, **dropping unnecessary packets**, **tracking flows**, and exposing **real-time telemetry** — now with a built-in **Packet Value Model (PVM)** that maximises the *business value* delivered per bit.

Designed to run on a **router**, **local server**, or **ISP edge node**.

---

## Features

| Feature | Description |
|---|---|
| 🚦 Traffic Prioritization | Rule-based classifier (VoIP → CRITICAL, HTTPS → HIGH, BitTorrent → BACKGROUND); strict priority-queue forwarding |
| 📦 Data Compression | zlib/DEFLATE payload compression; skips already-compressed or too-small payloads |
| 🗑️ Packet Dropping | Per-priority token-bucket rate limiting + RED (Random Early Detection) |
| 🔀 Flow Intelligence | 5-tuple flow tracking with latency/bandwidth/burst scoring; auto-adjusts priority per flow behaviour |
| 📋 YAML Policy DSL | Define classification rules in YAML – no Python required; supports ports, protocols, regex payload patterns, bandwidth hints, and `value_coefficient` |
| 📡 Real-time Telemetry | FastAPI backend with REST + WebSocket streaming; built-in live dashboard |
| 🔌 Capture Abstraction | Pluggable backends: `SimulatedCapture`, `NFQueueCapture` (Linux NFQUEUE), `LibpcapCapture` (scapy/libpcap) |
| 💰 **Packet Value Model** | Value-weighted scheduling via `FlowValuePolicy` + `ValueScheduler`; `ValueLossTracker` reports $/s lost; `ValueSLAContract` guarantees minimum delivered-value-rate per tenant |
| 🔑 **License / Feature Gating** | HMAC-signed license keys unlock Pro/Enterprise features (PVM, SLA enforcement, multi-node); trial keys generated in one line |

---

## Architecture

```
Incoming packet
       │
       ▼
┌──────────────────┐
│   FlowTracker    │  → 5-tuple flow table; latency/bandwidth/burst scoring
└────────┬─────────┘
         │
         ▼
┌─────────────────────┐
│  TrafficClassifier  │  → rule-based priority (CRITICAL/HIGH/MEDIUM/LOW/BACKGROUND)
│  (+ PolicyLoader)   │    rules can come from YAML policy file
└────────┬────────────┘
         │
         ▼  flow score adjusts priority (boost latency-sensitive / demote bulk)
         │
         ▼
┌─────────────────┐
│   PacketFilter  │  → token-bucket + RED; drop if over rate or queue pressure
└────────┬────────┘
         │ (not dropped)
         ▼
┌─────────────────────┐
│ PayloadCompressor   │  → zlib compress if large enough and compressible
└────────┬────────────┘
         │
         ▼
┌──────────────────────┐
│  PriorityScheduler   │  → priority heap; CRITICAL-first forwarding
└──────────────────────┘
         │
         ▼
  FastAPI /ws (WebSocket telemetry) → live dashboard
```

### Modules

| Module | Responsibility |
|---|---|
| `bandwidth_optimizer/config.py` | `DeploymentMode`, `TrafficPriority`, `OptimizerConfig` |
| `bandwidth_optimizer/classifier.py` | `Packet`, `TrafficClassifier`, `ClassificationRule` |
| `bandwidth_optimizer/compressor.py` | `PayloadCompressor` – zlib-based compression |
| `bandwidth_optimizer/packet_filter.py` | `TokenBucket`, `PacketFilter` – rate limiting + RED |
| `bandwidth_optimizer/scheduler.py` | `PriorityScheduler` – thread-safe priority queue |
| `bandwidth_optimizer/flow_tracker.py` | `FlowKey`, `FlowRecord`, `FlowTracker` – 5-tuple flow intelligence |
| `bandwidth_optimizer/policy.py` | `PolicyLoader` – YAML policy DSL → `ClassificationRule` objects |
| `bandwidth_optimizer/optimizer.py` | `BandwidthOptimizer` – orchestrates all stages |
| `bandwidth_optimizer/capture/` | Capture backends: `SimulatedCapture`, `NFQueueCapture`, `LibpcapCapture` |
| `api/server.py` | FastAPI REST + WebSocket telemetry server |
| `api/static/index.html` | Live dashboard (vanilla JS, no build step) |

---

## Quick Start

```bash
# Install runtime dependencies
pip install -e .

# Run demo packets through the optimizer
python main.py demo

# Use a custom YAML policy file
python main.py --policy policy_example.yaml demo

# Specify deployment mode and bandwidth limit (5 MB/s, router mode)
python main.py --mode router --bw 5242880 demo

# Live simulation (Ctrl-C to stop)
python main.py simulate

# Start the telemetry server + dashboard (http://localhost:8000/)
python main.py serve

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
    forward(result.packet)   # payload may be compressed

# Inspect flow information
print(result.flow_record.latency_score)   # 0.0–1.0
print(result.flow_record.bandwidth_score) # 0.0–1.0

print(optimizer.stats())
# → includes flows.active count
```

### Using a YAML policy file

```yaml
# my_policy.yaml
version: "1"
defaults:
  priority: MEDIUM
rules:
  - name: zoom_video
    description: "Zoom video conferencing"
    match:
      ports: [8801, 8802]
      protocols: [udp]
    priority: CRITICAL
    bandwidth_min_pct: 30
  - name: bittorrent
    match:
      ports: [6881, 6882, 6883]
      protocols: [tcp, udp]
    priority: BACKGROUND
```

```bash
python main.py --policy my_policy.yaml serve
```

### Real packet capture (Linux)

```bash
# 1. Install NFQUEUE binding
pip install netfilterqueue

# 2. Redirect traffic to queue 0
sudo iptables -I INPUT   -j NFQUEUE --queue-num 0
sudo iptables -I OUTPUT  -j NFQUEUE --queue-num 0
sudo iptables -I FORWARD -j NFQUEUE --queue-num 0

# 3. Run the optimizer (must be root)
sudo python -c "
from bandwidth_optimizer import BandwidthOptimizer
from bandwidth_optimizer.capture import NFQueueCapture

optimizer = BandwidthOptimizer()
with NFQueueCapture(queue_num=0) as cap:
    for captured in cap.packets():
        result = optimizer.process(captured.packet)
        if captured.nfqueue_handle:
            if result.dropped:
                captured.nfqueue_handle.drop()
            else:
                captured.nfqueue_handle.accept()
"
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

Default bandwidth budgets: CRITICAL 30%, HIGH 30%, MEDIUM 20%, LOW 10%, BACKGROUND 5%.

---

## Flow Intelligence

Each unique 5-tuple (src_ip, dst_ip, src_port, dst_port, protocol) is tracked as a **flow**. Three scores are computed from flow history:

| Score | Range | High score means… |
|---|---|---|
| `latency_score` | 0–1 | Small, frequent packets → VoIP/gaming pattern → boost priority |
| `bandwidth_score` | 0–1 | High byte rate → bulk transfer → demote priority |
| `burst_score` | 0–1 | Packets arrive in bursts rather than at steady rate |

`FlowTracker.priority_hint()` automatically adjusts priority by one level up (latency-sensitive) or down (bandwidth-heavy) based on configured thresholds.

---

## Deployment Modes

| Mode | Target |
|---|---|
| `router` | Home / enterprise router |
| `local_server` | Local server acting as a traffic proxy |
| `isp_edge` | ISP edge node for large-scale traffic shaping |

---

## Telemetry API

Start the server:
```bash
python main.py serve --host 0.0.0.0 --port 8000
```

| Endpoint | Method | Description |
|---|---|---|
| `/` | GET | Live dashboard (HTML) |
| `/stats` | GET | JSON statistics snapshot |
| `/flows` | GET | All active flows with scoring |
| `/value` | GET | **PVM metrics** – value delivered/lost per second, per-flow coefficients, SLA contract status |
| `/sla` | GET | SLA violation counts (requires `SLAMonitor`) |
| `/backpressure` | GET | Backpressure signal (none / soft / hard) with `recommended_throttle_pct` |
| `/health` | GET | Health check |
| `/ws` | WebSocket | Push stats every second (includes `value_efficiency_pct` when PVM is active) |

---

## Packet Value Model (PVM)

The PVM reframes network scheduling from *"which priority tier is this?"* to *"how much business value does this flow deliver per bit?"*.

A BACKGROUND BitTorrent flow from a paying enterprise customer with `value_coefficient: 200` will be scheduled **ahead** of a default CRITICAL flow — because it is worth more to the business.

### Enable PVM in 3 lines

```python
from bandwidth_optimizer.policy import PolicyLoader
from bandwidth_optimizer.value import FlowValuePolicy
from bandwidth_optimizer import BandwidthOptimizer

policy = PolicyLoader.load_file("my_policy.yaml")
optimizer = BandwidthOptimizer(flow_value_policy=FlowValuePolicy.from_policy(policy))
```

### YAML policy with value coefficients

```yaml
version: "1"
defaults:
  priority: MEDIUM
rules:
  - name: enterprise_voip
    description: "Enterprise VoIP (worth 100× default)"
    match:
      ports: [5060, 5061]
      protocols: [udp]
    priority: CRITICAL
    value_coefficient: 100.0     # ← PVM key; higher = more scheduling weight

  - name: paid_streaming
    match:
      ports: [443]
      protocols: [tcp]
    priority: HIGH
    value_coefficient: 50.0

  - name: bulk_backup
    match:
      ports: [6881]
      protocols: [tcp, udp]
    priority: BACKGROUND
    value_coefficient: 0.5       # low value → yields to everything else
```

### Value metrics in stats

```python
stats = optimizer.stats()
# stats["value"]["value_efficiency_pct"]  → 97.3
# stats["value"]["value_lost_per_sec"]    → 4.2
# stats["value"]["value_delivered_total"] → 12345.6
```

### ValueSLAContract – per-tenant guarantees

```python
from bandwidth_optimizer.value import ValueSLAContract, ValueLossTracker

contract = ValueSLAContract("voip_tenant", min_value_rate_per_sec=100.0)
if contract.is_violated(optimizer.value_tracker.value_delivered_per_sec):
    alert("SLA breach: VoIP tenant below guaranteed value rate")
```

---

## License Keys

Three product tiers unlocked via HMAC-signed license keys:

| Tier | Features | Price |
|---|---|---|
| Community (OSS) | All base features | Free |
| Pro | + PVM, SLA enforcement, multi-node | $2k–$15k/site/yr |
| Enterprise / ISP | + value federation, BGP hooks | $50k–$500k/cluster/yr |

```bash
# Generate a trial key (all Pro features, no expiry)
python -c "from bandwidth_optimizer.license import LicenseKey; print(LicenseKey.generate_trial())"

# Use the key
python main.py --license-key <KEY> --policy my_policy.yaml serve
```

The license key format is `<base64url_payload>.<HMAC-SHA256>`.  The development
key (`bandwidthos-dev-key`) is built in and enables evaluation without a
licensing server.  Production keys are signed with a private secret set via
`BANDWIDTHOS_LICENSE_SECRET`.



```bash
pip install pytest httpx
pytest tests/ -v
```

394 tests covering all modules end-to-end.

---

## How Dropping Works

1. **Token Bucket** – each priority class has its own bucket refilling at a rate proportional to its bandwidth budget. CRITICAL packets are never rate-limited.
2. **RED (Random Early Detection)** – once the queue reaches `red_min_threshold` (default 50% full), incoming non-CRITICAL packets are probabilistically dropped. Above `red_max_threshold` (default 90%) all are dropped.

---

## Capture Backends

| Backend | Platform | Requires |
|---|---|---|
| `SimulatedCapture` | Any | Nothing (built-in) |
| `NFQueueCapture` | Linux only | `pip install netfilterqueue` + root + iptables rule |
| `LibpcapCapture` | Cross-platform | `pip install scapy` + libpcap + root (live capture) |
