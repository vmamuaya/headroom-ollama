#!/usr/bin/env bash
# headroom-ollama clone-setup installer
# Bootstrap a fresh system to run headroom-ai proxy + Ollama Cloud backend
# with a 2-layer failsafe stack (5-min watchdog + 30s kill-switch daemon).
#
# Usage:
#   git clone https://github.com/vmamuaya/headroom-ollama.git
#   cd headroom-ollama
#   # Edit templates/headroom.env.template and put your real OLLAMA_API_KEY
#   cp templates/headroom.env.template ~/.headroom/headroom.env
#   chmod 600 ~/.headroom/headroom.env
#   ./scripts/install.sh
#
# What it does:
#   1. Verifies prerequisites (python3, uv, systemd, hermes)
#   2. Creates ~/.headroom/ state dir
#   3. Creates ~/.local/venvs/headroom venv with uv
#   4. Installs headroom-ai[proxy] + any-llm-sdk[all]
#   5. Copies bin/ scripts to ~/.local/bin
#   6. Copies systemd/ units to ~/.config/systemd/user
#   7. daemon-reload + enables headroom-proxy + watchdog timer + failsafe
#   8. Starts everything
#   9. Runs scripts/verify.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HEADROOM_HOME="${HEADROOM_HOME:-$HOME/.headroom}"
HEADROOM_VENV="${HEADROOM_VENV:-$HOME/.local/venvs/headroom}"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"

# Colors
RED='\033[0;31m'
GRN='\033[0;32m'
YEL='\033[1;33m'
NC='\033[0m'

say() { printf "${GRN}[+]${NC} %s\n" "$*"; }
warn() { printf "${YEL}[!]${NC} %s\n" "$*" >&2; }
die() { printf "${RED}[x]${NC} %s\n" "$*" >&2; exit 1; }

# ----- 1. Prereqs -----
say "Checking prerequisites..."
command -v python3 >/dev/null || die "python3 not found"
command -v uv >/dev/null || die "uv not found (install from https://docs.astral.sh/uv/)"
command -v systemctl >/dev/null || die "systemctl not found (systemd required)"
[[ -d "$HERMES_HOME" ]] || die "Hermes not found at $HERMES_HOME (install Hermes first)"
command -v hermes >/dev/null || warn "hermes CLI not in PATH; continuing anyway"

# ----- 2. State dir -----
say "Creating state directory: $HEADROOM_HOME"
mkdir -p "$HEADROOM_HOME"

# ----- 3. Env file -----
if [[ ! -f "$HEADROOM_HOME/headroom.env" ]]; then
    if [[ -f "$REPO_ROOT/templates/headroom.env.template" ]]; then
        cp "$REPO_ROOT/templates/headroom.env.template" "$HEADROOM_HOME/headroom.env"
        chmod 600 "$HEADROOM_HOME/headroom.env"
        warn "Created $HEADROOM_HOME/headroom.env from template."
        warn "  >>> EDIT THIS FILE and replace ***your-ollama-cloud-api-key-here*** with your real OLLAMA_API_KEY <<<"
        warn "  >>> Then run: systemctl --user restart headroom-proxy.service <<<"
        die "Set OLLAMA_API_KEY first, then re-run install.sh"
    else
        die "Env template missing at $REPO_ROOT/templates/headroom.env.template"
    fi
fi
chmod 600 "$HEADROOM_HOME/headroom.env"

# ----- 4. Venv -----
say "Setting up venv at $HEADROOM_VENV"
mkdir -p "$(dirname "$HEADROOM_VENV")"
if [[ ! -d "$HEADROOM_VENV" ]]; then
    uv venv "$HEADROOM_VENV" --python 3.14
fi
# Install proxy extras — DO NOT install bare 'headroom', it's an impostor package that shadows the namespace
uv pip install --python "$HEADROOM_VENV/bin/python" 'headroom-ai[proxy]' 'any-llm-sdk[all]' --force-reinstall

# ----- 5. Bin scripts -----
say "Installing scripts to ~/.local/bin/"
mkdir -p "$HOME/.local/bin"
for s in "$REPO_ROOT"/bin/*.py; do
    name="$(basename "$s")"
    cp "$s" "$HOME/.local/bin/$name"
    chmod 0755 "$HOME/.local/bin/$name"
done

# ----- 6. Systemd units -----
say "Installing systemd units to ~/.config/systemd/user/"
mkdir -p "$HOME/.config/systemd/user"
for u in "$REPO_ROOT"/systemd/*.service "$REPO_ROOT"/systemd/*.timer; do
    name="$(basename "$u")"
    cp "$u" "$HOME/.config/systemd/user/$name"
done

# ----- 7. Reload + enable -----
say "Reloading systemd user daemon..."
systemctl --user daemon-reload

say "Enabling headroom-proxy + watchdog timer + failsafe..."
systemctl --user enable --now headroom-proxy.service
systemctl --user enable --now headroom-watchdog.timer
systemctl --user enable --now headroom-failsafe.service

# ----- 8. Start proxy -----
say "Starting headroom-proxy..."
systemctl --user restart headroom-proxy.service

# ----- 9. Verify -----
say "Running verify.sh..."
sleep 3
exec "$REPO_ROOT/scripts/verify.sh"