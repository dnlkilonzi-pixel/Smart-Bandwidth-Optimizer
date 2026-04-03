# ── Stage 1: build dependencies ──────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# Install build tools
RUN pip install --upgrade pip

# Copy only dependency manifests first (layer-cache friendly)
COPY requirements.txt setup.py ./
COPY bandwidth_optimizer/__init__.py bandwidth_optimizer/

# Install runtime dependencies into a prefix we can copy
RUN pip install --prefix=/install --no-cache-dir \
    fastapi \
    "uvicorn[standard]" \
    pyyaml

# ── Stage 2: runtime image ────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="Smart Bandwidth Optimizer" \
      org.opencontainers.image.description="Production-grade traffic shaping agent" \
      org.opencontainers.image.source="https://github.com/dnlkilonzi-pixel/Smart-Bandwidth-Optimizer"

# Non-root user for security
RUN groupadd --gid 1001 bwopt && \
    useradd  --uid 1001 --gid bwopt --no-create-home --shell /sbin/nologin bwopt

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application source
COPY --chown=bwopt:bwopt bandwidth_optimizer/ ./bandwidth_optimizer/
COPY --chown=bwopt:bwopt api/                 ./api/
COPY --chown=bwopt:bwopt main.py              ./
COPY --chown=bwopt:bwopt policy_example.yaml  ./

USER bwopt

# Expose telemetry port
EXPOSE 8000

# Health check – hits the /health REST endpoint
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c \
        "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" \
        || exit 1

# Default: start the telemetry server on 0.0.0.0:8000
# Override CMD to run 'bench', 'demo', 'simulate', etc.
CMD ["python", "main.py", "serve", "--host", "0.0.0.0", "--port", "8000"]
