#!/usr/bin/env bash
# headroom-ollama portable installer
#
# Detects distro (Fedora/RHEL, Ubuntu/Debian, Arch), installs required system
# packages via the native package manager, sets up a persistent Python venv at
# ~/.local/venvs/headroom, installs headroom-ai[proxy], configures systemd --user
# services + timer for the 5-min watchdog + 30s failsafe, and starts everything.
#
# Idempotent: safe to re-run. Already-installed components are detected and skipped.
#
# Requires: bash 4+, systemd (user bus). Needs sudo for system packages.

set -euo pipefail

# ----------------------------------------------------------------------------- 
# Configurable defaults
# -----------------------------------------------------------------------------
HEADROOM_HOME="${HEADROOM_HOME:-$HOME/.headroom}"
VENV_DIR="${VENV_DIR:-$HOME/.local/venvs/headroom}"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
BIN_DIR="$REPO_DIR/bin"
SYSTEMD_SRC="$REPO_DIR/systemd"

# -----------------------------------------------------------------------------
# Logging helpers
# -----------------------------------------------------------------------------
log() { printf '\033[1;34m[install]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[install:WARN]\033[0m %s\n' "$*" >&2; }
err() { printf '\033[1;31m[install:ERR]\033[0m %s\n' "$*" >&2; }
die() { err "$*"; exit 1; }

# -----------------------------------------------------------------------------
# Detect distro
# -----------------------------------------------------------------------------
detect_distro() {
    if [[ -f /etc/os-release ]]; then
        . /etc/os-release
        case "${ID:-unknown}" in
            fedora|rhel|centos|rocky|almalinux|ol) FAMILY="rpm" ;;
            ubuntu|debian|linuxmint|pop)            FAMILY="deb" ;;
            arch|manjaro|endeavouros)               FAMILY="arch" ;;
            *)
                if [[ "${ID_LIKE:-}" == *debian* ]]; then FAMILY="deb"
                elif [[ "${ID_LIKE:-}" == *rhel* || "${ID_LIKE:-}" == *fedora* ]]; then FAMILY="rpm"
                else FAMILY="unknown"
                fi
                ;;
        esac
        DISTRO_NAME="${PRETTY_NAME:-$ID}"
    elif command -v lsb_release >/dev/null 2>&1; then
        ID="$(lsb_release -si | tr '[:upper:]' '[:lower:]')"
        case "$ID" in
            ubuntu|debian) FAMILY="deb" ;;
            fedora|centos|rhel) FAMILY="rpm" ;;
            arch) FAMILY="arch" ;;
            *) FAMILY="unknown" ;;
        esac
        DISTRO_NAME="$(lsb_release -sd)"
    else
        FAMILY="unknown"
        DISTRO_NAME="unknown"
    fi
    log "Detected distro: $DISTRO_NAME (family: $FAMILY)"
}

# -----------------------------------------------------------------------------
# Install system packages
# -----------------------------------------------------------------------------
install_system_packages() {
    local pkgs_pkgr="python3 python3-pip python3-venv git curl ca-certificates"
    local pkgs_rpm="python3 python3-pip python3-virtualenv git curl ca-certificates"
    local pkgs_arch="python python-pip git curl ca-certificates"

    case "$FAMILY" in
        deb)
            log "Installing packages via apt (may need sudo)"
            if [[ $EUID -ne 0 ]]; then SUDO="sudo"; else SUDO=""; fi
            $SUDO apt-get update
            $SUDO apt-get install -y --no-install-recommends $pkgs_pkgr
            ;;
        rpm)
            log "Installing packages via dnf/yum (may need sudo)"
            if [[ $EUID -ne 0 ]]; then SUDO="sudo"; else SUDO=""; fi
            if command -v dnf >/dev/null 2>&1; then PM="dnf"
            elif command -v yum >/dev/null 2>&1; then PM="yum"
            else die "No dnf/yum found on RPM-based system"
            fi
            $SUDO "$PM" install -y $pkgs_rpm
            ;;
        arch)
            log "Installing packages via pacman (may need sudo)"
            if [[ $EUID -ne 0 ]]; then SUDO="sudo"; else SUDO=""; fi
            $SUDO pacman -Sy --noconfirm --needed $pkgs_arch
            ;;
        *)
            warn "Unknown distro ($DISTRO_NAME). Skipping package install."
            warn "You'll need at minimum: python3 (>=3.10), pip, git, curl, systemd"
            return 0
            ;;
    esac
}

# -----------------------------------------------------------------------------
# Install uv if missing
# -----------------------------------------------------------------------------
install_uv() {
    if command -v uv >/dev/null 2>&1; then
        log "uv already present: $(uv --version)"
        return 0
    fi
    log "Installing uv (Python package manager) from astral-sh/uv install script"
    # The official installer handles both pip-based and standalone installs
    if ! curl -fsSL https://astral.sh/uv/install.sh | sh; then
        die "uv install failed. Get it from https://docs.astral.sh/uv/getting-started/installation/"
    fi
    export PATH="$HOME/.local/bin:$PATH"
    command -v uv >/dev/null || die "uv not found on PATH after install"
    log "uv installed: $(uv --version)"
}

# -----------------------------------------------------------------------------
# Create venv + install headroom-ai
# -----------------------------------------------------------------------------
setup_venv() {
    if [[ ! -d "$VENV_DIR" ]]; then
        log "Creating venv at $VENV_DIR"
        mkdir -p "$(dirname "$VENV_DIR")"
        uv venv "$VENV_DIR" --python python3
    else
        log "Venv already exists at $VENV_DIR (skipping creation)"
    fi
    log "Installing/verifying headroom-ai[proxy] and any-llm-sdk"
    # shellcheck disable=SC1091
    source "$VENV_DIR/bin/activate"
    uv pip install --upgrade 'headroom-ai[proxy]' any-llm-sdk
    log "Venv ready"
}

# -----------------------------------------------------------------------------
# Patch venv headroom binary shebang (defensive — protects against rot)
# -----------------------------------------------------------------------------
patch_shebang() {
    local bin="$VENV_DIR/bin/headroom"
    if [[ ! -f "$bin" ]]; then
        warn "No headroom binary at $bin — skipping shebang patch"
        return 0
    fi
    local python_path
    python_path="$VENV_DIR/bin/python"
    local current
    current="$(head -1 "$bin")"
    if [[ "$current" != "#!$python_path" ]]; then
        log "Patching shebang of $bin to $python_path"
        sed -i "1s|.*|#!$python_path|" "$bin"
        chmod +x "$bin"
    else
        log "Shebang already correct"
    fi
}

# -----------------------------------------------------------------------------
# Setup env file
# -----------------------------------------------------------------------------
setup_env_file() {
    mkdir -p "$HEADROOM_HOME"
    if [[ ! -f "$HEADROOM_HOME/headroom.env" ]]; then
        log "Creating env file at $HEADROOM_HOME/headroom.env with placeholder key"
        cp "$REPO_DIR/templates/headroom.env.template" "$HEADROOM_HOME/headroom.env"
        chmod 600 "$HEADROOM_HOME/headroom.env"
        warn "Edit $HEADROOM_HOME/headroom.env and replace ***your-ollama-cloud-api-key-here*** with your real OLLAMA_API_KEY before traffic will succeed."
        warn "Re-run installer OR systemctl --user restart headroom-proxy.service after editing."
    else
        log "Env file already present at $HEADROOM_HOME/headroom.env (mode $(stat -c '%a' "$HEADROOM_HOME/headroom.env" 2>/dev/null || stat -f '%p' "$HEADROOM_HOME/headroom.env"))"
    fi
}

# -----------------------------------------------------------------------------
# Install systemd --user units
# -----------------------------------------------------------------------------
install_systemd_units() {
    if ! command -v systemctl >/dev/null 2>&1; then
        warn "systemctl not found on this system. Skipping systemd unit registration."
        warn "Headroom will still work; you'll need to start scripts manually:"
        warn "  $VENV_DIR/bin/headroom --backend litellm-openai &"
        warn "  python3 $BIN_DIR/headroom-watchdog.py (5-min loop, in another shell)"
        warn "  python3 $BIN_DIR/headroom-failsafe.py (continuous loop, third shell)"
        warn "Or install systemd + run inside a session with 'loginctl enable-linger USERNAME'"
        return 0
    fi

    mkdir -p "$SYSTEMD_USER_DIR"
    cp "$SYSTEMD_SRC/headroom-proxy.service" "$SYSTEMD_USER_DIR/"
    cp "$SYSTEMD_SRC/headroom-watchdog.service" "$SYSTEMD_USER_DIR/"
    cp "$SYSTEMD_SRC/headroom-watchdog.timer"  "$SYSTEMD_USER_DIR/"
    cp "$SYSTEMD_SRC/headroom-failsafe.service" "$SYSTEMD_USER_DIR/"
    log "Systemd units copied to $SYSTEMD_USER_DIR"

    # Make sure --user services persist across logout on Ubuntu and others
    local cur_user="${USER:-$(id -un)}"
    if command -v loginctl >/dev/null 2>&1; then
        if ! loginctl show-user "$cur_user" 2>/dev/null | grep -q 'Linger=yes'; then
            warn "User $cur_user is not set to linger. --user services will stop on logout."
            warn "Fix: sudo loginctl enable-linger $cur_user"
        else
            log "User $cur_user has linger enabled (good)"
        fi
    fi

    log "Reloading systemd --user daemon"
    if ! systemctl --user daemon-reload 2>&1; then
        warn "systemctl --user daemon-reload failed (DBus may be unavailable)"
        warn "Try running inside an interactive --user session"
    fi
}

# -----------------------------------------------------------------------------
# Enable + start services
# -----------------------------------------------------------------------------
start_services() {
    if ! command -v systemctl >/dev/null 2>&1; then
        warn "systemctl not found. Skipping service start."
        warn "Start manually: see notes in install_systemd_units() above"
        return 0
    fi
    if ! systemctl --user status >/dev/null 2>&1; then
        warn "systemctl --user status failed. DBus session bus is unavailable."
        warn "Try:  systemctl --user (in a graphical/login session)"
        warn "Or:   systemctl --user --machine=\"\${USER:-$(id -un)}\"@.host ... (headless systems)"
        return 0
    fi
    log "Enabling and starting headroom-proxy.service"
    systemctl --user enable --now headroom-proxy.service || warn "Failed to enable headroom-proxy.service"

    log "Enabling and starting headroom-watchdog.timer (5-min cadence)"
    systemctl --user enable --now headroom-watchdog.timer || warn "Failed to enable headroom-watchdog.timer"

    log "Enabling and starting headroom-failsafe.service (30s kill-switch loop)"
    systemctl --user enable --now headroom-failsafe.service || warn "Failed to enable headroom-failsafe.service"
}

# -----------------------------------------------------------------------------
# Copy scripts to ~/.local/bin so they're on PATH for manual invocation
# -----------------------------------------------------------------------------
install_local_scripts() {
    mkdir -p "$HOME/.local/bin"
    for f in "$BIN_DIR"/*.py "$REPO_DIR/scripts/"*.sh; do
        [[ -f "$f" ]] || continue
        cp "$f" "$HOME/.local/bin/"
        chmod +x "$HOME/.local/bin/$(basename "$f")"
    done
    log "Scripts installed to $HOME/.local/bin"
}

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
main() {
    log "headroom-ollama installer starting"
    detect_distro
    install_system_packages
    install_uv
    setup_venv
    patch_shebang
    setup_env_file
    install_systemd_units
    install_local_scripts
    start_services

    echo
    log "==============================================="
    log "Install complete. Next steps:"
    log "  1. Edit ~/.headroom/headroom.env — replace placeholder OLLAMA_API_KEY"
    log "  2. systemctl --user restart headroom-proxy.service"
    log "  3. Run scripts/verify.sh to confirm health"
    log "==============================================="
}

main "$@"
