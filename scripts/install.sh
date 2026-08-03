#!/usr/bin/env bash
# =============================================================================
# GeyserSteam Valve Controller — One-line installer
# Usage:
#   wget -qO- https://raw.githubusercontent.com/YOUR_ORG/geysersteam-valve/main/scripts/install.sh | bash
#
# What this script does:
#   1. Installs system dependencies (Python packages, ModemManager, qmicli)
#   2. Clones the repository to ~/geysersteam-valve
#   3. Converts JSON config templates to pickle files the controller reads
#   4. Installs udev rules for the GL7611 modem
#   5. Installs and enables the systemd service
#   6. Guides you through creating Secrets.py with your credentials
# =============================================================================

set -e  # exit on any error

REPO_URL="https://github.com/YOUR_ORG/geysersteam-valve.git"
INSTALL_DIR="$HOME/geysersteam-valve"
SERVICE_NAME="valve"
PYTHON="python3"

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'

info()    { echo -e "${BLUE}[INFO]${NC} $*"; }
success() { echo -e "${GREEN}[OK]${NC}   $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }
error()   { echo -e "${RED}[ERR]${NC}  $*"; exit 1; }
section() { echo -e "\n${BOLD}── $* ──${NC}"; }

# ── Detect username (works whether run as pi, hg, or any other user) ─────────
WHOAMI=$(whoami)
if [ "$WHOAMI" = "root" ]; then
    error "Do not run as root. Run as your normal Pi user (e.g. pi or hg)."
fi

echo -e "${BOLD}"
echo "╔══════════════════════════════════════════════════════╗"
echo "║   GeyserSteam Water Valve Controller — Installer    ║"
echo "╚══════════════════════════════════════════════════════╝"
echo -e "${NC}"
info "Installing for user: $WHOAMI"
info "Install directory:   $INSTALL_DIR"
echo ""

# ── 1. System update and dependencies ────────────────────────────────────────
section "Step 1/6: Installing system dependencies"

sudo apt-get update -qq
sudo apt-get install -y \
    git \
    python3-pip \
    python3-serial \
    libqmi-utils \
    modemmanager \
    udhcpc 2>/dev/null || true

# paho-mqtt — pin to 1.6.1 for Python 3.7 compatibility
$PYTHON -m pip install --break-system-packages "paho-mqtt==1.6.1" 2>/dev/null \
    || $PYTHON -m pip install "paho-mqtt==1.6.1"

# RPi.GPIO for valve hardware control
$PYTHON -m pip install --break-system-packages RPi.GPIO 2>/dev/null \
    || $PYTHON -m pip install RPi.GPIO

# Add user to dialout group for serial port access without sudo
sudo usermod -aG dialout "$WHOAMI"

success "Dependencies installed"

# ── 2. Clone repository ───────────────────────────────────────────────────────
section "Step 2/6: Cloning repository"

if [ -d "$INSTALL_DIR" ]; then
    warn "Directory $INSTALL_DIR already exists — pulling latest changes"
    cd "$INSTALL_DIR"
    git pull
else
    git clone "$REPO_URL" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
fi

success "Repository ready at $INSTALL_DIR"

# ── 3. Generate pickle config files from JSON templates ───────────────────────
section "Step 3/6: Generating configuration files"

$PYTHON - << 'PYEOF'
import json, pickle, os

install_dir = os.path.expanduser("~/geysersteam-valve")

# comandos.dat
with open(f"{install_dir}/config/config_template.json") as f:
    raw = json.load(f)
# Convert string keys to int keys as the controller expects
datos = {int(k): v for k, v in raw.items()}
for path in [f"{install_dir}/comandos.dat", f"{install_dir}/comandosbac.dat"]:
    with open(path, "wb") as f:
        pickle.dump(datos, f, protocol=2)
print(f"  Written: comandos.dat + comandosbac.dat")

# broker.dat
with open(f"{install_dir}/config/brokers_template.json") as f:
    brokers = json.load(f)
with open(f"{install_dir}/broker.dat", "wb") as f:
    pickle.dump(brokers, f, protocol=2)
print(f"  Written: broker.dat ({len(brokers)} brokers)")
print("  Config files generated successfully")
PYEOF

success "Config files generated"

# ── 4. udev rules for GL7611 modem ───────────────────────────────────────────
section "Step 4/6: Installing modem udev rules"

sudo cp "$INSTALL_DIR/config/99-gl7611.rules" /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger

# Disable ModemManager — our script manages the modem directly via qmicli
sudo systemctl stop ModemManager  2>/dev/null || true
sudo systemctl disable ModemManager 2>/dev/null || true

success "udev rules installed, ModemManager disabled"

# ── 5. Systemd service ────────────────────────────────────────────────────────
section "Step 5/6: Installing systemd service"

# Patch the service file with the actual username and install dir
sed "s|User=pi|User=$WHOAMI|g; s|Group=pi|Group=$WHOAMI|g; \
     s|/home/pi/geysersteam-valve|$INSTALL_DIR|g" \
    "$INSTALL_DIR/systemd/valve.service" \
    | sudo tee /etc/systemd/system/${SERVICE_NAME}.service > /dev/null

sudo systemctl daemon-reload
sudo systemctl enable ${SERVICE_NAME}.service

success "Service installed and enabled (will start on next boot)"

# ── 6. Secrets setup ─────────────────────────────────────────────────────────
section "Step 6/6: Credentials setup"

SECRETS_FILE="$INSTALL_DIR/Secrets.py"

if [ -f "$SECRETS_FILE" ] && grep -q "YOUR_MQTT_PASSWORD" "$SECRETS_FILE"; then
    warn "Secrets.py contains placeholder values — please fill them in now."
fi

if [ ! -f "$SECRETS_FILE" ] || grep -q "YOUR_MQTT_PASSWORD" "$SECRETS_FILE"; then
    echo ""
    echo "Enter your credentials (press Enter to skip and edit Secrets.py manually later):"
    echo ""

    read -p "  MQTT username   : " MQTT_USER
    read -s -p "  MQTT password   : " MQTT_PASS; echo ""
    read -p "  Operator password (8 chars, default 12345678): " OP_PWD
    read -p "  Master password  : " MASTER_PWD
    read -p "  APN              (default: data.mono): " APN_VAL
    read -p "  MQTT Broker index (0-6, default 4): " BROKER_IDX

    OP_PWD="${OP_PWD:-12345678}"
    MASTER_PWD="${MASTER_PWD:-V5307110}"
    APN_VAL="${APN_VAL:-data.mono}"
    BROKER_IDX="${BROKER_IDX:-4}"

    cat > "$SECRETS_FILE" << SECRETSEOF
# Secrets.py — DO NOT commit to git
BOT_ID       = ""
PWD          = "${OP_PWD}"
MASTER_PWD   = "${MASTER_PWD}"
passwordlikk = "${MQTT_PASS}"
usernamelikk = "${MQTT_USER}"
SECRETSEOF

    # Update APN and broker index in pickle config
    $PYTHON - << PYEOF2
import pickle, os
d = os.path.expanduser("~/geysersteam-valve")
for fname in ["comandos.dat", "comandosbac.dat"]:
    with open(f"{d}/{fname}", "rb") as f:
        datos = pickle.load(f)
    datos[9]["value"] = "${APN_VAL}"
    datos[7]["value"] = "${BROKER_IDX}"
    with open(f"{d}/{fname}", "wb") as f:
        pickle.dump(datos, f, protocol=2)
print("  APN and broker index updated in config")
PYEOF2

    success "Secrets.py written"
else
    success "Secrets.py already configured"
fi

# ── Final summary ─────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${GREEN}╔══════════════════════════════════════════════════════╗"
echo    "║              Installation Complete!                 ║"
echo -e "╚══════════════════════════════════════════════════════╝${NC}"
echo ""
echo "  Install dir : $INSTALL_DIR"
echo "  Service     : sudo systemctl status $SERVICE_NAME"
echo "  Logs        : journalctl -u $SERVICE_NAME -f"
echo "  Start now   : sudo systemctl start $SERVICE_NAME"
echo ""
echo -e "${YELLOW}IMPORTANT:${NC}"
echo "  1. Plug in the Sierra Wireless GL7611 modem via USB"
echo "  2. Edit $SECRETS_FILE if credentials need updating"
echo "  3. Reboot to apply group membership (dialout) and udev rules:"
echo ""
echo -e "     ${BOLD}sudo reboot${NC}"
echo ""
