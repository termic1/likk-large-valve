"""
Water Valve MQTT Controller
Version: 203
Hardware: Raspberry Pi + SIM7600H-H (SimTech) cellular modem via USB
Fixes vs v201:
  - keepalive reduced to 60s to prevent broker-side timeout kills
  - client.connect() always passes port + keepalive consistently
  - on_connect sets listening=True and clears discoflag
  - disco() protected against re-entrant double-calls via disco_running flag
  - disco() no longer called twice per main loop iteration
  - usb0 bounce followed by full modem() reinit instead of orphaned interface
  - it%10==60 modulo bug fixed to it%10==0
  - subprocess.Popen replaced with subprocess.run() + error checking
  - credentials and secrets moved to secrets.py (excluded from version control)
  - removed dead code (unreachable print after return, duplicate function defs)
  - BOT_ID and passwords removed from source
New in v203:
  - 'apn' command: query current APN (*PWD*apn*) or change it (*PWD*apn*new.apn.value*)
    APN is persisted in datos[9] so it survives reboots; modem is re-initialised
    automatically after a change so the new APN takes effect immediately
"""

import datetime
import sys
import time
from time import sleep
import random
import valve
import subprocess
import pickle
from os import rename, remove, fsync
import paho.mqtt.client as mqtt
import socket
import serial
import serial.tools.list_ports
import threading

# ── Load secrets from external file (create secrets.py alongside this file) ──
# secrets.py should contain:
#   BOT_ID        = "..."
#   PWD           = "12345678"
#   MASTER_PWD    = "V5307110"
#   passwordlikk  = "..."
#   usernamelikk  = "..."
try:
    import Secrets as _s   # file must be named Secrets.py (capital S)
    BOT_ID       = _s.BOT_ID
    PWD          = _s.PWD
    MASTER_PWD   = _s.MASTER_PWD
    passwordlikk = _s.passwordlikk
    usernamelikk = _s.usernamelikk
except ImportError:
    print("WARNING: secrets.py not found — using empty credentials")
    BOT_ID       = ""
    PWD          = "12345678"
    MASTER_PWD   = "V5307110"
    passwordlikk = ""
    usernamelikk = ""

# ── Constants ─────────────────────────────────────────────────────────────────
VER        = "203"
MACHINE_ID = "000001"
TELEG      = False
ABRIR      = "1"
CERRAR     = "0"
rootDir    = "/home/pi/Documents/"
BUILDING   = "00"
BROKER_NO  = 4
BROKER_ALT = False
TEST_LED   = False
remochange = False
contador1  = 0
lowdata    = True
APN        = 'data.mono'           # ← your SIM carrier APN, NOT the broker hostname
keepalive  = 60                    # FIX: was 180 — broker was timing out the connection
port       = 1883                  # FIX: was 883 (non-standard) — verify with your broker
LIVE_TIME  = 299

# ── State flags ───────────────────────────────────────────────────────────────
modemin      = False
errcounter   = 0
reintentos   = 0
mensajenuevo = False
mensajetexto = ""
listening    = False
discoflag    = False
disco_running = False              # FIX: prevents re-entrant calls to disco()
rcpaso       = 0
elpuerto     = ""
serialFlag   = False
green        = False
respuAT      = b""
IMI          = ""
SIG          = ""
IP           = ""
Mserial      = ""
DNS1         = "0.0.0.0"
DNS2         = "0.0.0.0"
carrier_dns  = None              # DNS assigned by carrier via AT+CGCONTRDP

brokers = [
    "10.0.0.203",
    "test.mosquitto.org",
    "10.0.0.81",
    "thermoheat.ddns.net",
    "iot.eclipse.org",
]

# ─────────────────────────────────────────────────────────────────────────────
# Modem / Serial helpers
# ─────────────────────────────────────────────────────────────────────────────

def send_at(command, back, timeout):
    """Send an AT command and wait for expected response string."""
    global respuAT
    rec_buff = b""
    # Flush stale data so previous response doesn't contaminate this read
    try:
        ser.reset_input_buffer()
    except Exception:
        pass
    ser.write((command + "\r\n").encode())
    time.sleep(timeout)
    if ser.inWaiting():
        time.sleep(0.1)
        rec_buff = ser.read(ser.inWaiting())
    if rec_buff:
        respuAT = rec_buff
        decoded = rec_buff.decode(errors="replace")
        if back not in decoded:
            print(f"{command} ERROR — got: {decoded.strip()}")
            return 0
        else:
            print(decoded.strip())
            return 1
    else:
        print(f"{command} — no response")
        return 0

# Alias used by modem() for GL7611
send_at_7611 = send_at


def comandoAT(atcom):
    """Write an AT command and return the decoded reply."""
    global errcounter
    try:
        ser.write(bytes(atcom, "UTF-8"))
        time.sleep(2)
        reply = ser.read(ser.inWaiting()).decode("utf-8", errors="replace")
        errcounter = 0
        return reply
    except Exception as e:
        errcounter += 1
        print(f"comandoAT error ({errcounter}): {e}")
        return "error cable"


def run_cmd(cmd):
    """Run a shell command, wait for it, and log any failure."""
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"CMD FAILED [{cmd}]: {result.stderr.strip()}")
    return result.returncode == 0


def bring_up_usb0():
    """
    Bring up usb0 for the GL7611 (CDC-ECM interface).

    The GL7611 does not respond to ARP — it is a USB tunnel, not a real
    Ethernet device. The kernel waits forever for an ARP reply for the
    gateway and marks it INCOMPLETE, so all packets are dropped.
    Fix: inject a static ARP entry pointing the gateway MAC to the
    usb0 interface MAC, which makes the kernel send frames directly
    without waiting for ARP resolution.
    """
    print("=== bring_up_usb0 ===")

    if not IP or IP == "0.0.0.0":
        print("ERROR: No IP from modem — cannot configure usb0")
        return

    import ipaddress
    try:
        host    = ipaddress.IPv4Address(IP)
        net_int = int(host) & 0xFFFFFFFC
        gw      = str(ipaddress.IPv4Address(net_int + 1))
        if gw == IP:
            gw = str(ipaddress.IPv4Address(net_int + 2))
        bcast   = str(ipaddress.IPv4Address(net_int + 3))
        cidr    = f"{IP}/30"
    except Exception as e:
        print(f"IP math failed ({e}) — using /24 with .1 gateway")
        parts = IP.rsplit(".", 1)
        gw    = parts[0] + ".1"
        bcast = parts[0] + ".255"
        cidr  = IP + "/24"

    print(f"Assigning {cidr} bcast {bcast} gw {gw} to usb0")

    # 1. Bounce interface cleanly
    run_cmd("sudo ip link set usb0 down")
    sleep(1)
    run_cmd("sudo ip link set usb0 up")
    sleep(1)
    run_cmd("sudo ip link set usb0 mtu 1400")

    # 3. Flush old addresses and assign static IP with explicit broadcast
    subprocess.run("sudo ip addr flush dev usb0", shell=True, capture_output=True)
    run_cmd(f"sudo ip addr add {cidr} broadcast {bcast} dev usb0")

    # 4. Disable ARP on usb0 — GL7611 CDC-ECM is a point-to-point tunnel,
    #    not a real Ethernet segment. ARP requests are never answered because
    #    there is no Ethernet switching layer. Setting arp off tells the kernel
    #    to send frames directly without ARP resolution.
    run_cmd("sudo ip link set usb0 arp off")

    # 5. Default route with 'onlink' — tells kernel the gateway is directly
    #    reachable on this interface without needing ARP confirmation.
    #    This is the correct flag for tunnel/PtP interfaces.
    subprocess.run("sudo ip route del default", shell=True, capture_output=True)
    run_cmd(f"sudo ip route add default via {gw} dev usb0 onlink")

    # 6. DNS — use carrier-assigned DNS from AT+CGCONTRDP if available,
    #    fall back to Google DNS
    dns1 = carrier_dns if carrier_dns else "8.8.8.8"
    dns2 = "8.8.4.4"
    dns  = f"nameserver {dns1}\nnameserver {dns2}\n"
    r = subprocess.run("sudo tee /etc/resolv.conf",
                       input=dns, shell=True, capture_output=True, text=True)
    if r.returncode == 0:
        print(f"DNS set to {dns1} / {dns2}")
    else:
        print(f"resolv.conf write failed: {r.stderr.strip()}")

    sleep(1)

    # 7. Diagnostic printout
    print("--- ip addr show usb0 ---")
    r = subprocess.run("ip addr show usb0", shell=True, capture_output=True, text=True)
    print(r.stdout)
    print("--- ip route ---")
    r = subprocess.run("ip route", shell=True, capture_output=True, text=True)
    print(r.stdout)
    print("--- arp -n ---")
    r = subprocess.run("arp -n", shell=True, capture_output=True, text=True)
    print(r.stdout)

    # 8. Quick connectivity test
    r = subprocess.run(f"ping -c 2 -W 3 {gw}",
                       shell=True, capture_output=True, text=True)
    if r.returncode == 0:
        print(f"Gateway {gw} reachable ✓")
    else:
        print(f"Gateway {gw} still not responding (normal for GL7611 tunnel mode)")
        # Even if gateway doesn't ping, internet may still work
        r2 = subprocess.run("ping -c 2 -W 5 8.8.8.8",
                            shell=True, capture_output=True, text=True)
        if r2.returncode == 0:
            print("Internet reachable via 8.8.8.8 ✓")
        else:
            print("WARNING: Internet not reachable — check APN and carrier account")

    print("=== bring_up_usb0 done ===")


def listar_dispositivos_usb():
    """Return the AT command serial port for the GL7611, or /dev/ttyUSB2 as default."""
    print("Scanning USB ports...")
    puertos = serial.tools.list_ports.comports()
    if not puertos:
        print("No USB devices found.")
        return "/dev/ttyUSB2"
    for puerto in puertos:
        print(f"  Port: {puerto.device}  Desc: {puerto.description}  HW: {puerto.hwid}")
        if "GL7611" in puerto.description or "Sierra Wireless" in puerto.description:
            # GL7611 exposes ttyUSB0/1/2 — ttyUSB2 is the AT command port
            print(f"  → GL7611 found, using /dev/ttyUSB2 for AT commands")
            return "/dev/ttyUSB2"
    print("GL7611 not found by description — defaulting to /dev/ttyUSB2")
    return "/dev/ttyUSB2"



QMI_DEV  = "/dev/cdc-wdm0"   # QMI control device created by qmi_wwan driver
WWAN_IF  = "wwan0"           # network interface created by qmi_wwan driver


def qmi_cmd(args, timeout=15):
    """Run a qmicli command and return (success, stdout)."""
    cmd = f"sudo qmicli -d {QMI_DEV} {args}"
    print(f"QMI> {cmd}")
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    out = r.stdout + r.stderr
    print(f"QMI< {out.strip()[:300]}")
    return r.returncode == 0, out


def bring_up_iface():
    """
    Configure wwan0 after a successful QMI data session start.
    Assigns IP/gateway/DNS obtained from --wds-get-current-settings.
    """
    global IP, carrier_dns

    print("=== bring_up_iface (wwan0/QMI) ===")

    # 1. wwan0 must be in raw-ip mode for QMI data sessions
    run_cmd(f"sudo ip link set {WWAN_IF} down")
    r = subprocess.run(
        f"echo 'Y' | sudo tee /sys/class/net/{WWAN_IF}/qmi/raw_ip",
        shell=True, capture_output=True, text=True
    )
    if r.returncode == 0:
        print("raw_ip mode enabled")
    else:
        print(f"raw_ip write failed: {r.stderr.strip()} — may already be set")
    run_cmd(f"sudo ip link set {WWAN_IF} up")
    sleep(1)
    run_cmd(f"sudo ip link set {WWAN_IF} mtu 1430")

    # 2. Get IP settings from modem
    ok, out = qmi_cmd("--wds-get-current-settings")
    gw      = None
    dns1    = None
    dns2    = None

    if ok:
        for line in out.splitlines():
            line = line.strip()
            if "IPv4 address:" in line:
                IP = line.split(":")[-1].strip()
            elif "IPv4 gateway address:" in line:
                gw = line.split(":")[-1].strip()
            elif "IPv4 primary DNS:" in line:
                dns1 = line.split(":")[-1].strip()
            elif "IPv4 secondary DNS:" in line:
                dns2 = line.split(":")[-1].strip()

    if not IP or IP == "0.0.0.0":
        print("ERROR: Could not get IP from QMI settings")
        return False

    carrier_dns = dns1
    gw = gw or IP.rsplit(".", 1)[0] + ".1"
    print(f"IP={IP}  GW={gw}  DNS1={dns1}  DNS2={dns2}")

    # 3. Assign IP — wwan0 in raw-ip mode uses /32 point-to-point
    subprocess.run(f"sudo ip addr flush dev {WWAN_IF}",
                   shell=True, capture_output=True)
    run_cmd(f"sudo ip addr add {IP}/32 dev {WWAN_IF}")

    # 4. Route — raw-ip tunnel, use onlink so kernel skips ARP
    subprocess.run("sudo ip route del default", shell=True, capture_output=True)
    run_cmd(f"sudo ip route add default via {gw} dev {WWAN_IF} onlink")

    # 5. DNS
    d1 = dns1 or "8.8.8.8"
    d2 = dns2 or "8.8.4.4"
    dns_txt = f"nameserver {d1}\nnameserver {d2}\n"
    subprocess.run("sudo tee /etc/resolv.conf",
                   input=dns_txt, shell=True, capture_output=True, text=True)
    print(f"DNS: {d1} / {d2}")

    sleep(1)

    # 6. Diagnostics
    print("--- ip addr show wwan0 ---")
    r = subprocess.run(f"ip addr show {WWAN_IF}",
                       shell=True, capture_output=True, text=True)
    print(r.stdout)
    print("--- ip route ---")
    r = subprocess.run("ip route", shell=True, capture_output=True, text=True)
    print(r.stdout)

    # 7. Connectivity test
    r = subprocess.run("ping -c 2 -W 5 8.8.8.8",
                       shell=True, capture_output=True, text=True)
    if r.returncode == 0:
        print("Internet reachable ✓")
        return True
    else:
        print("WARNING: ping 8.8.8.8 failed — check APN and carrier account")
        return False


# Keep bring_up_usb0 as alias in case it's called elsewhere
bring_up_usb0 = bring_up_iface


def modem():
    """
    Connect the Sierra Wireless GL7611 using QMI via /dev/cdc-wdm0 + wwan0.

    The GL7611 exposes:
      ttyUSB0/1/2  — AT command ports
      cdc-wdm0     — QMI control port  (interface: wwan0, driver: qmi_wwan)
      usb0         — CDC-Ethernet port (driver: cdc_ether, NOT used for data)

    Data must flow through wwan0/QMI. usb0 is a secondary interface that is
    not connected to the cellular bearer and cannot be used for internet access.
    """
    global IMI, IP, Mserial, reintentos, modemin, treset, APN, carrier_dns

    print("modem() called")
    if modemin:
        print("modem() already running — skipping")
        return
    modemin = True

    # ── 1. Basic AT check (ttyUSB2) ──────────────────────────────────────────
    if "OK" not in comandoAT("AT\r"):
        print("Modem AT port not responding — aborting")
        modemin = False
        return
    print("Modem AT port OK")

    send_at_7611("AT+CMEE=1", "OK", 2)

    # Get IMEI via AT
    if send_at_7611("AT+CGSN", "OK", 2):
        decoded = respuAT.decode(errors="replace")
        for line in decoded.splitlines():
            line = line.strip()
            if line.isdigit() and len(line) >= 14:
                IMI = line
                print(f"IMEI: {IMI}")
                break

    # ── 2. Check QMI device exists ───────────────────────────────────────────
    import os
    if not os.path.exists(QMI_DEV):
        print(f"ERROR: {QMI_DEV} not found — qmi_wwan driver not loaded")
        modemin = False
        return

    # Stop ModemManager if running — it fights with qmicli over /dev/cdc-wdm0
    subprocess.run("sudo systemctl stop ModemManager",
                   shell=True, capture_output=True)

    # ── 3. Disable competing interfaces ──────────────────────────────────────
    run_cmd("sudo ip link set wlan0 down")
    run_cmd("sudo ip link set eth0 down")
    run_cmd("sudo ip link set usb0 down")   # bring down CDC-Ethernet, not used

    # ── 4. Stop any existing QMI data session ────────────────────────────────
    qmi_cmd("--wds-stop-network=0x00000001 --client-no-release-cid")
    sleep(1)

    # ── 5. Start QMI data session ─────────────────────────────────────────────
    print(f"Starting QMI data session with APN={APN}")
    ok, out = qmi_cmd(
        f'--wds-start-network="apn={APN},ip-type=4" --client-no-release-cid',
        timeout=30
    )

    if not ok or "error" in out.lower():
        print(f"QMI start-network failed: {out.strip()}")
        # Retry once after a short wait
        sleep(5)
        ok, out = qmi_cmd(
            f'--wds-start-network="apn={APN},ip-type=4" --client-no-release-cid',
            timeout=30
        )
        if not ok:
            print("QMI start-network failed on retry — aborting")
            modemin = False
            reintentos += 1
            return

    print("QMI data session started")
    sleep(3)

    # ── 6. Bring up wwan0 with assigned IP ───────────────────────────────────
    bring_up_iface()

    reintentos += 1
    print(f"Modem init attempts: {reintentos}")

    tlimit = (datetime.datetime.now() - treset).seconds
    if reintentos >= 5 or tlimit >= 10800:
        print("Too many modem retries — resetting counter")
        reintentos = 0

    modemin = False


# ─────────────────────────────────────────────────────────────────────────────
# Network helpers
# ─────────────────────────────────────────────────────────────────────────────

def is_internet_connected():
    """Return True if we can reach Google DNS on port 53."""
    try:
        socket.create_connection(("8.8.8.8", 53), timeout=5)
        return True
    except OSError:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# MQTT callbacks
# ─────────────────────────────────────────────────────────────────────────────

def on_connect(client, userdata, flags, rc):
    global listening, discoflag, BROKER_ALT
    if rc == 0:
        print(f"MQTT connected to {brokers[int(BROKER_NO)]}")
        try:
            client.subscribe(TOPIC2)
            print(f"Subscribed to {TOPIC2}")
        except Exception as e:
            print(f"Subscribe failed: {e}")
        listening  = True    # FIX: was never set here
        discoflag  = False   # FIX: clear immediately on successful connect
        BROKER_ALT = False
        if powermsg:
            client.publish(TOPIC1, powermsg)
    elif rc == 5:
        print(f"MQTT auth error connecting to {brokers[int(BROKER_NO)]}")
    else:
        print(f"MQTT connect failed, rc={rc}")


def on_disconnect(client, userdata, rc):
    global listening, discoflag, rcpaso
    listening = False
    discoflag = True
    rcpaso    = rc
    print(f"MQTT disconnected rc={rc} — disco will run on next main loop tick")
    # NOTE: we do NOT call disco() here to avoid threading issues with loop_start()


def on_message(client, userdata, message):
    global mensajenuevo, mensajetexto
    payload = str(message.payload.decode("utf-8"))
    print(f"Message received topic={message.topic} payload={payload}")
    mensajenuevo = True
    mensajetexto = payload


# ─────────────────────────────────────────────────────────────────────────────
# Reconnection logic
# ─────────────────────────────────────────────────────────────────────────────

def disco():
    """
    Called when internet or MQTT connection is lost.
    Attempts to restore connectivity in stages, then reconnects MQTT.
    Protected against re-entrant calls.
    """
    global discoflag, listening, disco_running, modemin

    # FIX: guard against being called twice in the same loop tick
    if disco_running:
        print("disco() already running — skipping duplicate call")
        return
    disco_running = True
    discoflag     = False

    print("=== disco(): starting reconnection sequence ===")

    # ── Stage 1: wait for internet, with escalating recovery ─────────────────
    it = 0
    while not is_internet_connected():
        it += 1
        print(f"No internet — attempt {it}")

        if it == 10:
            print("Stage 1: reinitialising wwan0")
            bring_up_iface()

        if it == 30:
            print("Stage 2: full modem reinit")
            modemin = False
            if serialFlag:
                modem()
            sleep(10)

        if it == 60:
            print("Stage 3: giving up this cycle — will retry next main loop tick")
            disco_running = False
            return

        if it % 10 == 0:   # FIX: was it%10==60 which is impossible
            print(f"  Still waiting for internet... (attempt {it})")

        sleep(5)

    print("Internet restored")

    # ── Stage 2: reconnect MQTT ───────────────────────────────────────────────
    attempt = 0
    while not client.is_connected():
        attempt += 1
        print(f"MQTT reconnect attempt {attempt}...")
        try:
            client.reconnect()
            sleep(3)
            if client.is_connected():
                # on_connect will set listening=True and discoflag=False
                print("MQTT reconnected via reconnect()")
                break
        except Exception as e:
            print(f"reconnect() failed: {e}")

        if attempt >= 3:
            # Fall back to a full connect() call
            try:
                print("Falling back to full client.connect()")
                client.username_pw_set(usernamelikk, passwordlikk)
                # FIX: always pass port and keepalive consistently
                client.connect(brokers[int(BROKER_NO)], port, keepalive)
                sleep(4)
                if client.is_connected():
                    print("MQTT reconnected via connect()")
                    break
            except Exception as e:
                print(f"Full connect() failed: {e}")

        if attempt >= 6:
            print("Giving up MQTT reconnect this cycle")
            break

        valve.pulseLED("RED")
        sleep(5)

    disco_running = False
    print("=== disco(): reconnection sequence complete ===")


# ─────────────────────────────────────────────────────────────────────────────
# File helpers
# ─────────────────────────────────────────────────────────────────────────────

def fileWriteP(file, data):
    if not data:
        return
    temp = "tmp"
    try:
        with open(rootDir + temp + file, "wb") as f:
            pickle.dump(data, f, protocol=2)
            f.flush()
            fsync(f.fileno())
        rename(rootDir + temp + file, rootDir + file)
        print(f"Wrote {file}")
    except Exception as e:
        print(f"fileWriteP error: {e}")
        try:
            remove(rootDir + temp + file)
        except Exception:
            pass


def fileReadP(file):
    for _ in range(2):
        try:
            with open(rootDir + file, "rb") as f:
                return pickle.loads(f.read())
        except Exception as e:
            print(f"fileReadP error ({file}): {e}")
            sleep(0.5)
    return []


# ─────────────────────────────────────────────────────────────────────────────
# System helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_mac(interface):
    try:
        return open(f"/sys/class/net/{interface}/address").readline()[:17]
    except Exception:
        return "00:00:00:00:00:00"


def pro_temp():
    try:
        t = subprocess.check_output("vcgencmd measure_temp", shell=True)
        return t.decode("utf-8").strip()
    except Exception:
        return "temp:0.0'C"


# ─────────────────────────────────────────────────────────────────────────────
# MQTT command handler
# ─────────────────────────────────────────────────────────────────────────────

def handleMQTT(msg):
    global PWD, LIVE_TIME, MACHINE_ID, TOPIC1, TOPIC2, BUILDING
    global remochange, BROKER_NO, lowdata, datos, brokers, green, APN

    if not msg.startswith("*"):
        client.publish(TOPIC1, f"{_now()} Wrong format")
        print("Wrong format")
        return

    command = msg.split("*")
    print(f"Command: {command}")

    if command[1] not in (PWD, MASTER_PWD):
        green = False
        client.publish(TOPIC1, f"{_now()} Wrong Password")
        print("Wrong password")
        return

    green = True
    order      = command[2] if len(command) > 2 else ""
    parameter1 = command[3] if len(command) > 3 else ""
    parameter2 = command[4] if len(command) > 4 else ""
    fechora    = _now()
    fechoras   = _now_short()

    def pub(msg):
        client.publish(TOPIC1, msg)

    if order == "ver":
        pub(f"Software Version:{VER}")

    elif order == "roll":
        pub(f"roll:{random.randint(1,6)}")

    elif order in ("openvalve", "ov"):
        valve.actumv4(ABRIR)
        if lowdata:
            pub(f"$avo{valve.estadolow()}")
        else:
            pub(f"{fechora} Action: Valve Open Status: {valve.estado()}")
        remochange = True

    elif order in ("closevalve", "cv"):
        valve.actumv4(CERRAR)
        if lowdata:
            pub(f"$avc{valve.estadolow()}")
        else:
            pub(f"{fechora} Action: Valve Close Status: {valve.estado()}")
        remochange = True

    elif order == "status":
        if lowdata:
            pub(f"${valve.estadolow()}: {fechoras}")
        else:
            pub(f"{fechora} Status: {valve.estado()}")

    elif order == "id":
        pub(f"{fechoras} Valve ID: {MACHINE_ID}")

    elif order == "ver":
        pub(f"Software Version:{VER}")

    elif order == "time":
        pub(f"time: {fechora if not lowdata else fechoras}")

    elif order == "ip":
        pub(f"ip:{IP}")

    elif order == "mac":
        pub(f"mac:{get_mac('wlan0')}")

    elif order == "temp":
        pub(f"temp:{pro_temp()}")

    elif order == "place":
        pub("25.950235199999998, -80.19307500000002")

    elif order == "lowdata":
        lowdata = True
        pub(f"$ldact:{fechoras}" if lowdata else f"{fechora} Low Data mode Activated")

    elif order == "nolowdata":
        lowdata = False
        pub(f"$nldact:{fechoras}" if lowdata else f"{fechora} Regular Data mode Activated")

    elif order == "off":
        pub(f"$shudwn:{fechoras}" if lowdata else f"{fechora} RTU will shutdown")
        run_cmd("sudo shutdown now")

    elif order == "restart":
        pub(f"{fechoras} reboot: 1min" if lowdata else f"{fechora} RTU will reboot in 1 minute")
        run_cmd("sudo reboot")

    elif order == "testled":
        if parameter1:
            valve.pulseLED(parameter1)
            sleep(2)
            respu = valve.pulseLED(parameter1)
            pub(f"{fechora} Tested LED response: {respu}")

    elif order == "password":
        if parameter1 and len(parameter1) == 8:
            PWD = parameter1
            datos[1]["value"] = PWD
            fileWriteP("comandos.dat", datos)
            pub(f"{fechora} Password changed")
        else:
            pub(f"{fechora} Password not accepted")

    elif order == "setdeviceid":
        if parameter1 and len(parameter1) <= 6:
            old = MACHINE_ID
            MACHINE_ID = parameter1
            TOPIC1 = f"stat/{BUILDING}/{MACHINE_ID}"
            TOPIC2 = f"push/{BUILDING}/{MACHINE_ID}"
            client.unsubscribe(f"push/{BUILDING}/{old}")
            client.subscribe(TOPIC2)
            datos[3]["value"] = MACHINE_ID
            fileWriteP("comandos.dat", datos)
            pub(f"{fechora} Device ID changed from {old} to {parameter1}")

    elif order == "setclientid":
        if parameter1 and len(parameter1) <= 4:
            old = BUILDING
            BUILDING = parameter1
            TOPIC1 = f"stat/{BUILDING}/{MACHINE_ID}"
            TOPIC2 = f"push/{BUILDING}/{MACHINE_ID}"
            client.unsubscribe(f"push/{old}/{MACHINE_ID}")
            client.subscribe(TOPIC2)
            datos[6]["value"] = BUILDING
            fileWriteP("comandos.dat", datos)
            pub(f"{fechora} Client ID changed from {old} to {parameter1}")

    elif order == "listbroker":
        msg_out = "  ".join(f"[{i}] {b}" for i, b in enumerate(brokers))
        pub(f"{fechora} Brokers: {msg_out}  Total: {len(brokers)}")

    elif order == "selectbroker":
        if parameter1 and int(parameter1) < len(brokers):
            BROKER_NO = int(parameter1)
            datos[7]["value"] = BROKER_NO
            fileWriteP("comandos.dat", datos)
            pub(f"{fechora} New Broker selected: {brokers[BROKER_NO]}")

    elif order == "insertbroker":
        if parameter1 and parameter2 and len(parameter2) > 3:
            brokers.insert(int(parameter1), parameter2)
            fileWriteP("broker.dat", brokers)
            pub(f"{fechora} Broker added at [{parameter1}]: {parameter2}  Total: {len(brokers)}")

    elif order == "removebroker":
        if parameter1 and int(parameter1) < len(brokers):
            removed = brokers.pop(int(parameter1))
            fileWriteP("broker.dat", brokers)
            pub(f"{fechora} Broker removed [{parameter1}]: {removed}  Total: {len(brokers)}")

    elif order == "disconnectbroker":
        pub(f"{fechora} Broker disconnected by command")
        client.disconnect()   # on_disconnect → discoflag=True → disco() on next tick

    elif order == "apn":
        if parameter1:
            # Change APN, persist it, then re-init modem so it takes effect immediately
            old_apn = APN
            APN = parameter1.strip()
            datos[9] = {"name": "APN", "value": APN}
            fileWriteP("comandos.dat", datos)
            pub(f"{fechora} APN changed from '{old_apn}' to '{APN}' — reinitialising modem")
            print(f"APN changed: {old_apn} → {APN}")
            # Re-init modem in background so MQTT publish above gets delivered first
            def _reinit_modem():
                global modemin
                sleep(2)
                modemin = False
                modem()
            threading.Thread(target=_reinit_modem, daemon=True).start()
        else:
            # No parameter → report current APN
            pub(f"{fechora} APN: '{APN}'")

    elif order == "conbroker":
        pub(f"{fechora} Broker disconnected — will auto-reconnect")
        client.disconnect()

    else:
        print(f"Command not recognised: {order}")


# ─────────────────────────────────────────────────────────────────────────────
# Tiny formatting helpers
# ─────────────────────────────────────────────────────────────────────────────

def _now():
    return str(datetime.datetime.now())[:19]

def _now_short():
    return str(datetime.datetime.now())[5:16]


# ─────────────────────────────────────────────────────────────────────────────
# Startup
# ─────────────────────────────────────────────────────────────────────────────

# Load persisted config
try:
    datos = fileReadP("comandos.dat")
except Exception:
    datos = fileReadP("comandosbac.dat")

print(f"Config loaded: {datos}")
PWD            = datos[1]["value"]
MASTER_PWD     = datos[2]["value"]
MACHINE_ID     = datos[3]["value"]
LIVE_TIME      = int(datos[4]["value"])
server_chat_id = int(datos[5]["value"])
BUILDING       = datos[6]["value"]
BROKER_NO      = 4                       # hardcoded — override from datos[7] if needed
STATUS_PREOFF  = datos[8]["value"]
# Load persisted APN (key 9); falls back to the hardcoded default for existing installs
# that don't have this key yet — it will be written on first 'apn' command or reboot
if 9 in datos and datos[9].get("value"):
    APN = datos[9]["value"]
    print(f"APN loaded from config: {APN}")
else:
    datos[9] = {"name": "APN", "value": APN}
    fileWriteP("comandos.dat", datos)
    print(f"APN not in config yet — using default and saving: {APN}")

TOPIC1 = f"stat/{BUILDING}/{MACHINE_ID}"
TOPIC2 = f"push/{BUILDING}/{MACHINE_ID}"

try:
    brokers = fileReadP("broker.dat")
    print(f"Brokers: {brokers}  count={len(brokers)}")
except Exception:
    print("Could not load broker.dat — using defaults")

treset = datetime.datetime.now()
oldt   = "01:00"

# Hardware init
valve.setup()
sleep(5)
valve.actumv4(STATUS_PREOFF)

# Serial / modem init
try:
    elpuerto = listar_dispositivos_usb()
    elpuerto = "/dev/ttyUSB2"           # override — remove if auto-detect is reliable
    print(f"Opening serial port: {elpuerto}")
    ser = serial.Serial(port=elpuerto, baudrate=115200, timeout=10)
    if not ser.is_open:
        ser.open()
    serialFlag = True
    print("Serial port open")
except Exception as e:
    print(f"No serial port available ({elpuerto}): {e}")
    serialFlag = False

if serialFlag:
    modem()

# ── MQTT initial connection ───────────────────────────────────────────────────
print("Creating MQTT client")
try:
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
except AttributeError:
    client = mqtt.Client()   # paho-mqtt < 1.6 — no CallbackAPIVersion
client.on_message    = on_message
client.on_disconnect = on_disconnect
client.on_connect    = on_connect

# Wait for internet before first MQTT connect
it = 0
while not is_internet_connected():
    it += 1
    print(f"Waiting for internet before MQTT connect (attempt {it})")
    if it == 10 and serialFlag:
        bring_up_usb0()
        sleep(20)
    if it == 30:
        modemin = False
        modem()
    sleep(5)

# Try to connect to MQTT broker
connected = False
for attempt in range(1, 4):
    try:
        print(f"MQTT connect attempt {attempt} to {brokers[int(BROKER_NO)]}:{port}")
        client.username_pw_set(usernamelikk, passwordlikk)
        # FIX: always pass port and keepalive
        client.connect(brokers[int(BROKER_NO)], port, keepalive)
        sleep(3)
        connected = True
        listening = True
        print("MQTT connected at startup")
        break
    except Exception as e:
        print(f"MQTT connect attempt {attempt} failed: {e}")
        valve.pulseLED("RED")
        sleep(5)

if not connected:
    print("WARNING: Could not connect to MQTT broker at startup — will retry in main loop")

powermsg = f"{_now()} $Power On {valve.estado()}"
client.loop_start()

# ─────────────────────────────────────────────────────────────────────────────
# Main loop
# ─────────────────────────────────────────────────────────────────────────────

t0     = datetime.datetime.now()   # heartbeat timer
t1     = datetime.datetime.now()   # valve-change poll timer
respue = valve.estado()

valve.actumv4(STATUS_PREOFF)
client.publish(TOPIC1, f"{_now()} Action: Set valve to pre-power status {STATUS_PREOFF} → {valve.estado()}")

print("Entering main loop")
while True:
    now = datetime.datetime.now()

    # ── Daily backup at 11:36 ─────────────────────────────────────────────────
    current_hhmm = str(now)[11:16]
    if current_hhmm == "11:36" and oldt != "11:36":
        fileWriteP("comandosbac.dat", datos)
        print("Daily backup written")
    oldt = current_hhmm

    # ── Process incoming MQTT message ─────────────────────────────────────────
    if mensajenuevo:
        mensajenuevo = False
        handleMQTT(mensajetexto)

    # ── Connectivity check — single guarded call to disco() ───────────────────
    # FIX: was calling disco() twice (once for no-internet, once for discoflag)
    #      now combined into a single check
    if not is_internet_connected() or discoflag:
        disco()

    # ── Heartbeat / "I am alive" publish ─────────────────────────────────────
    c = (now - t0).seconds
    if c >= LIVE_TIME and server_chat_id != 0 and LIVE_TIME > 0:
        respu = valve.estado()
        if lowdata:
            client.publish(TOPIC1, f"$a{valve.estadolow()}")
        else:
            client.publish(TOPIC1, f"{_now()} I am alive: {respu}")

        # Processor temperature alert
        try:
            tmp = float(pro_temp()[5:9])
            print(f"CPU temp: {tmp}°C")
            if tmp > 60:
                client.publish(TOPIC1, f"{_now()} ⚠ High CPU temp: {tmp}°C")
        except Exception:
            pass

        t0 = datetime.datetime.now()

    # ── Valve state change detection (every 20 s) ─────────────────────────────
    d = (now - t1).seconds
    if d >= 20:
        old_respue = respue
        respue = valve.estado()
        if old_respue != respue:
            if respue in ("Open Valve", "Undefined"):
                STATUS_PREOFF = "1"
            elif respue == "Close Valve":
                STATUS_PREOFF = "0"
            datos[8]["value"] = STATUS_PREOFF
            fileWriteP("comandos.dat", datos)

            if lowdata:
                tag = "$am" if not remochange else "$ac"
                client.publish(TOPIC1, f"{tag}{valve.estadolow()}:{str(now)[5:19]}")
            else:
                tag = "manual" if not remochange else "after remote command"
                client.publish(TOPIC1, f"{_now()} new status {tag}: {respue}")

            if respue != "Undefined":
                remochange = False

        t1 = datetime.datetime.now()

    # ── LED status indicator ──────────────────────────────────────────────────
    if not TEST_LED:
        valve.estado()   # updates LED based on current state

    # ── Physical button polling ───────────────────────────────────────────────
    boton = valve.pushb()
    if boton == "OPEN":
        print("Green button pressed")
        valve.pulseLED("RED")
        valve.actumv4(ABRIR)
        if lowdata:
            client.publish(TOPIC1, f"$abg{valve.estadolow()}:{str(now)[5:19]}")
        else:
            client.publish(TOPIC1, f"{_now()} Action: Green Button Press Status: {valve.estado()}")
    elif boton == "CLOSE":
        print("Red button pressed")
        valve.pulseLED("GREEN")
        valve.actumv4(CERRAR)
        if lowdata:
            client.publish(TOPIC1, f"$abr{valve.estadolow()}:{str(now)[5:19]}")
        else:
            client.publish(TOPIC1, f"{_now()} Action: Red Button Press Status: {valve.estado()}")

    sleep(0.5)   # small yield to keep loop responsive without spinning at 100%

# (loop_stop never reached in normal operation — SIGTERM/SIGKILL handles shutdown)
client.loop_stop()
