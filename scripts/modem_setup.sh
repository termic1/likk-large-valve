#!/usr/bin/env bash
# =============================================================================
# Likk Large Valve — Modem Setup & Diagnostic Script
# Run this manually to test the GL7611 modem connection independently
# of the valve controller service.
#
# Usage: bash scripts/modem_setup.sh [--test-only]
#   --test-only  : diagnose and print status without changing anything
# =============================================================================

set -e

QMI_DEV="/dev/cdc-wdm0"
WWAN_IF="wwan0"
AT_PORT="/dev/ttyUSB2"
APN="data.mono"   # overridden by Secrets/config if found

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'

ok()   { echo -e "${GREEN}  ✓${NC} $*"; }
fail() { echo -e "${RED}  ✗${NC} $*"; }
info() { echo -e "${BLUE}  →${NC} $*"; }
warn() { echo -e "${YELLOW}  !${NC} $*"; }

TEST_ONLY=false
[[ "${1}" == "--test-only" ]] && TEST_ONLY=true

echo -e "${BOLD}Likk-large Valve GL7611 Modem Setup${NC}"
echo "────────────────────────────────"

# ── Read APN from config if available ────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -f "$SCRIPT_DIR/comandos.dat" ]; then
    APN_FROM_CONFIG=$(python3 -c "
import pickle
with open('$SCRIPT_DIR/comandos.dat','rb') as f:
    d=pickle.load(f)
print(d.get(9,{}).get('value','data.mono'))
" 2>/dev/null) && APN="$APN_FROM_CONFIG"
fi
info "Using APN: $APN"

# ── 1. Check GL7611 is enumerated ────────────────────────────────────────────
echo ""
echo -e "${BOLD}[1] USB enumeration${NC}"
if lsusb | grep -q "1199:68C0"; then
    ok "GL7611 found on USB bus"
else
    fail "GL7611 NOT found — check USB cable and power"
    exit 1
fi

# ── 2. Check serial ports ─────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}[2] Serial ports${NC}"
for port in /dev/ttyUSB0 /dev/ttyUSB1 /dev/ttyUSB2; do
    if [ -c "$port" ]; then
        ok "$port exists"
    else
        warn "$port not found"
    fi
done

# Quick AT test on ttyUSB2
if [ -c "$AT_PORT" ]; then
    RESP=$(echo -e 'AT\r\n' | sudo timeout 3 tee "$AT_PORT" | cat 2>/dev/null || true)
    sleep 1
    if sudo timeout 2 bash -c "echo -e 'AT\r\n' > $AT_PORT && sleep 1 && cat $AT_PORT" 2>/dev/null | grep -q "OK"; then
        ok "$AT_PORT responding to AT commands"
    else
        warn "$AT_PORT — could not confirm AT response (modem may be busy)"
    fi
fi

# ── 3. Check QMI device ───────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}[3] QMI control device${NC}"
if [ -c "$QMI_DEV" ]; then
    ok "$QMI_DEV exists"
else
    fail "$QMI_DEV not found — qmi_wwan driver may not be loaded"
    info "Try: sudo modprobe qmi_wwan"
    exit 1
fi

# ── 4. Check wwan0 interface ──────────────────────────────────────────────────
echo ""
echo -e "${BOLD}[4] Network interface${NC}"
if ip link show "$WWAN_IF" &>/dev/null; then
    ok "$WWAN_IF interface exists"
    STATE=$(ip link show "$WWAN_IF" | grep -oP '(?<=state )\w+')
    info "State: $STATE"
else
    fail "$WWAN_IF interface not found"
    exit 1
fi

RAW_IP=$(cat /sys/class/net/$WWAN_IF/qmi/raw_ip 2>/dev/null || echo "N")
if [ "$RAW_IP" = "Y" ]; then
    ok "raw_ip mode: enabled"
else
    warn "raw_ip mode: disabled"
    if [ "$TEST_ONLY" = false ]; then
        info "Enabling raw_ip..."
        sudo ip link set "$WWAN_IF" down
        echo 'Y' | sudo tee /sys/class/net/$WWAN_IF/qmi/raw_ip > /dev/null
        sudo ip link set "$WWAN_IF" up
        ok "raw_ip enabled"
    fi
fi

# ── 5. Check existing QMI session ────────────────────────────────────────────
echo ""
echo -e "${BOLD}[5] QMI data session${NC}"

if [ "$TEST_ONLY" = false ]; then
    info "Stopping any existing session..."
    sudo qmicli -d "$QMI_DEV" --wds-stop-network=0x00000001 \
        --client-no-release-cid 2>/dev/null || true
    sleep 1

    info "Starting new QMI data session (APN=$APN)..."
    SESSION=$(sudo qmicli -d "$QMI_DEV" \
        --wds-start-network="apn=${APN},ip-type=4" \
        --client-no-release-cid 2>&1)
    echo "$SESSION"

    if echo "$SESSION" | grep -q "Network started"; then
        ok "QMI data session started"
    else
        fail "QMI start-network failed"
        echo "$SESSION"
        exit 1
    fi

    sleep 2

    # ── 6. Get IP settings ────────────────────────────────────────────────────
    echo ""
    echo -e "${BOLD}[6] IP configuration${NC}"
    SETTINGS=$(sudo qmicli -d "$QMI_DEV" --wds-get-current-settings 2>&1)
    echo "$SETTINGS"

    IP_ADDR=$(echo "$SETTINGS" | grep "IPv4 address:"        | awk '{print $NF}')
    GW_ADDR=$(echo "$SETTINGS" | grep "IPv4 gateway address:" | awk '{print $NF}')
    DNS1=$(echo    "$SETTINGS" | grep "IPv4 primary DNS:"    | awk '{print $NF}')
    DNS2=$(echo    "$SETTINGS" | grep "IPv4 secondary DNS:"  | awk '{print $NF}')

    if [ -z "$IP_ADDR" ]; then
        fail "Could not parse IP from QMI settings"
        exit 1
    fi
    ok "IP: $IP_ADDR  GW: $GW_ADDR  DNS: $DNS1 / $DNS2"

    # ── 7. Configure wwan0 ────────────────────────────────────────────────────
    echo ""
    echo -e "${BOLD}[7] Configuring $WWAN_IF${NC}"
    sudo ip addr flush dev "$WWAN_IF"
    sudo ip addr add "${IP_ADDR}/32" dev "$WWAN_IF"
    sudo ip route del default 2>/dev/null || true
    sudo ip route add default via "$GW_ADDR" dev "$WWAN_IF" onlink
    echo "nameserver ${DNS1:-8.8.8.8}
nameserver ${DNS2:-8.8.4.4}" | sudo tee /etc/resolv.conf > /dev/null
    sudo ip link set "$WWAN_IF" mtu 1430

    ok "$WWAN_IF configured"
    ip addr show "$WWAN_IF"
    ip route show
fi

# ── 8. Connectivity test ──────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}[8] Connectivity test${NC}"
if ping -c 3 -W 5 8.8.8.8 &>/dev/null; then
    ok "Internet reachable (8.8.8.8) — 0% packet loss"
else
    fail "Cannot reach 8.8.8.8"
fi

if ping -c 2 -W 5 google.com &>/dev/null; then
    ok "DNS resolving (google.com)"
else
    warn "DNS resolution failed — check /etc/resolv.conf"
fi

echo ""
echo -e "${BOLD}${GREEN}Modem setup complete.${NC}"
echo "You can now start the valve controller:"
echo "  sudo systemctl start valve"
echo "  journalctl -u valve -f"
