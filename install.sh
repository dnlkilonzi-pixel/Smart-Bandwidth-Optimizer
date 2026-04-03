#!/usr/bin/env bash
# install.sh – one-line installer for Smart Bandwidth Optimizer
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/dnlkilonzi-pixel/Smart-Bandwidth-Optimizer/main/install.sh | bash
#   # or locally:
#   bash install.sh [--dir /opt/bandwidth-optimizer] [--port 8000] [--no-service]
#
# What this script does:
#   1. Creates a dedicated system user 'bwopt'
#   2. Clones/copies the application to --dir (default /opt/bandwidth-optimizer)
#   3. Creates a Python virtual environment and installs dependencies
#   4. Installs and enables the systemd service (skip with --no-service)
#   5. Prints a summary with the dashboard URL

set -euo pipefail

# ── defaults ──────────────────────────────────────────────────────────────────
INSTALL_DIR="/opt/bandwidth-optimizer"
SERVICE_PORT=8000
INSTALL_SERVICE=true
REPO_URL="https://github.com/dnlkilonzi-pixel/Smart-Bandwidth-Optimizer.git"
SERVICE_FILE="deploy/bandwidth-optimizer.service"
SYSTEM_USER="bwopt"
PYTHON=${PYTHON:-python3}

# ── colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RESET='\033[0m'
info()  { echo -e "${GREEN}[INFO]${RESET}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${RESET}  $*"; }
error() { echo -e "${RED}[ERROR]${RESET} $*" >&2; exit 1; }

# ── parse args ────────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir)        INSTALL_DIR="$2"; shift 2 ;;
    --port)       SERVICE_PORT="$2"; shift 2 ;;
    --no-service) INSTALL_SERVICE=false; shift ;;
    --python)     PYTHON="$2"; shift 2 ;;
    -h|--help)
      echo "Usage: $0 [--dir PATH] [--port PORT] [--no-service] [--python PATH]"
      exit 0 ;;
    *) error "Unknown option: $1" ;;
  esac
done

# ── checks ────────────────────────────────────────────────────────────────────
[[ $EUID -ne 0 ]] && error "This installer must be run as root (use sudo)."

command -v "$PYTHON" >/dev/null 2>&1 || error "$PYTHON not found. Install Python 3.9+ first."
PY_VER=$("$PYTHON" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
info "Using $PYTHON $PY_VER"

# ── system user ───────────────────────────────────────────────────────────────
if ! id "$SYSTEM_USER" &>/dev/null; then
  info "Creating system user '$SYSTEM_USER'…"
  useradd --system --no-create-home --shell /sbin/nologin "$SYSTEM_USER"
fi

# ── install directory ─────────────────────────────────────────────────────────
info "Installing to $INSTALL_DIR…"
if [[ -d "$INSTALL_DIR/.git" ]]; then
  info "Updating existing clone…"
  git -C "$INSTALL_DIR" pull --ff-only
elif command -v git >/dev/null 2>&1 && [[ ! -d "$INSTALL_DIR" ]]; then
  git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
elif [[ -d "$(dirname "$0")/bandwidth_optimizer" ]]; then
  info "Copying from local checkout…"
  cp -r "$(dirname "$0")/." "$INSTALL_DIR/"
else
  error "Cannot find source. Run from the repo directory or ensure git is available."
fi

# ── virtual environment ───────────────────────────────────────────────────────
VENV="$INSTALL_DIR/venv"
if [[ ! -d "$VENV" ]]; then
  info "Creating virtual environment…"
  "$PYTHON" -m venv "$VENV"
fi

info "Installing Python dependencies…"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -e "$INSTALL_DIR"

# ── permissions ───────────────────────────────────────────────────────────────
chown -R "$SYSTEM_USER:$SYSTEM_USER" "$INSTALL_DIR"
mkdir -p /var/lib/bandwidth-optimizer
chown "$SYSTEM_USER:$SYSTEM_USER" /var/lib/bandwidth-optimizer

# ── systemd service ───────────────────────────────────────────────────────────
if $INSTALL_SERVICE && command -v systemctl >/dev/null 2>&1; then
  SVCFILE="/etc/systemd/system/bandwidth-optimizer.service"
  info "Installing systemd service to $SVCFILE…"
  sed "s|/opt/bandwidth-optimizer|$INSTALL_DIR|g; s|--port 8000|--port $SERVICE_PORT|g" \
      "$INSTALL_DIR/$SERVICE_FILE" > "$SVCFILE"

  systemctl daemon-reload
  systemctl enable --now bandwidth-optimizer
  info "Service started. Check status with: systemctl status bandwidth-optimizer"
elif $INSTALL_SERVICE; then
  warn "systemd not found – skipping service installation."
  warn "Start manually: $VENV/bin/python $INSTALL_DIR/main.py serve --host 0.0.0.0 --port $SERVICE_PORT"
fi

# ── summary ───────────────────────────────────────────────────────────────────
echo ""
info "Installation complete!"
echo ""
echo "  Dashboard  : http://$(hostname -I | awk '{print $1}' 2>/dev/null || echo localhost):$SERVICE_PORT/"
echo "  Stats API  : http://$(hostname -I | awk '{print $1}' 2>/dev/null || echo localhost):$SERVICE_PORT/stats"
echo "  CLI        : $VENV/bin/python $INSTALL_DIR/main.py --help"
echo ""
