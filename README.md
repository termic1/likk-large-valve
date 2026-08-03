# Likk — Water Valve MQTT Controller

Remote control system for a motorized water valve (riser or main supply line) in a large building. Runs on a Raspberry Pi with a Sierra Wireless GL7611 LTE cellular modem, communicates via MQTT, and survives reboots and network outages automatically.

---

## Hardware

| Component | Details |
|---|---|
| Controller | Raspberry Pi (any model with USB) |
| Modem | Sierra Wireless GL7611 (WP7611) — LTE Cat-1 |
| SIM | Monogoto IoT eSIM (AT&T IoT network, APN: `data.mono`) |
| Valve | Motorized ball valve, controlled via GPIO through `valve.py` |
| Interface | USB — modem exposes `ttyUSB0/1/2` (AT) + `wwan0` (QMI data) |

### How the GL7611 connects to Linux

The GL7611 exposes two network interfaces on USB:

- **`usb0`** — CDC-Ethernet (not connected to cellular, ignore this)
- **`wwan0`** — QMI data interface via `/dev/cdc-wdm0` ← **this is the one used**

Data sessions are managed via `qmicli` (part of `libqmi-utils`). ModemManager is installed as a dependency but disabled — our script manages the modem directly.

---

## One-line Install

On a fresh Raspberry Pi OS (Bookworm/Bullseye/Buster), run:

```bash
wget -qO- https://raw.githubusercontent.com/termic1/likk-large-valve/main/scripts/install.sh | bash
```

This will:
1. Install all system and Python dependencies
2. Clone the repository to `~/likk-large-valve`
3. Generate the pickle config files from JSON templates
4. Install udev rules for the GL7611
5. Install and enable the `valve` systemd service
6. Prompt you for MQTT credentials and APN

Then **reboot** to apply group membership and udev rules:

```bash
sudo reboot
```

After reboot, plug in the GL7611 and the service starts automatically.

---

## Manual Install (step by step)

```bash
# 1. Dependencies
sudo apt-get update
sudo apt-get install -y git python3-pip libqmi-utils modemmanager
pip3 install paho-mqtt==1.6.1

# 2. Clone
git clone https://github.com/termic1/likk-large-valve.git ~/likk-large-valve
cd ~/likk-large-valve

# 3. Run installer
bash scripts/install.sh
```

---

## Repository Structure

```
likk-large-valve/
├── valve_controller.py      # Main application
├── valve.py                 # Hardware abstraction (GPIO, LEDs, buttons)
├── Secrets.py               # Credentials — NOT in git (see .gitignore)
├── README.md
├── .gitignore
│
├── config/
│   ├── config_template.json   # Default settings (converted to pickle on install)
│   ├── brokers_template.json  # MQTT broker list
│   └── 99-gl7611.rules        # udev rules for GL7611 modem
│
├── scripts/
│   ├── install.sh             # One-line installer
│   ├── modem_setup.sh         # Standalone modem diagnostic + setup
│   └── uninstall.sh           # Remove service and rules
│
└── systemd/
    └── valve.service          # systemd unit file
```

---

## Configuration

### Secrets.py

Create `Secrets.py` in the install directory (never commit this file):

```python
BOT_ID       = ""                    # Telegram bot token (optional)
PWD          = "12345678"            # 8-character operator password
MASTER_PWD   = "YOUR_MASTER_PWD"    # Master override password
passwordlikk = "YOUR_MQTT_PASSWORD" # MQTT broker password
usernamelikk = "YOUR_MQTT_USERNAME" # MQTT broker username
```

### Runtime config (comandos.dat)

Stored as a Python pickle file, editable via MQTT commands at runtime:

| Key | Name | Default | Description |
|---|---|---|---|
| 1 | PWD | 12345678 | Operator password |
| 2 | MASTER_PWD | V5307110 | Master password |
| 3 | MACHINE_ID | 000001 | Unique valve ID |
| 4 | LIVE_TIME | 299 | Heartbeat interval (seconds) |
| 5 | LIVE_ACT | 0 | Heartbeat target chat ID |
| 6 | BUILDING | 00 | Building/client ID |
| 7 | BROKER_NO | 4 | Index into broker list |
| 8 | STATUS_PREOFF | 1 | Valve state on power-on (1=open, 0=closed) |
| 9 | APN | data.mono | Cellular APN |

---

## MQTT Topics

| Topic | Direction | Description |
|---|---|---|
| `stat/{BUILDING}/{MACHINE_ID}` | Publish | Status and responses |
| `push/{BUILDING}/{MACHINE_ID}` | Subscribe | Incoming commands |

### Command Format

All commands use `*` as delimiter:

```
*{PASSWORD}*{command}*{param1}*{param2}
```

### Command Reference

| Command | Parameters | Description |
|---|---|---|
| `status` | — | Report current valve state |
| `openvalve` / `ov` | — | Open the valve |
| `closevalve` / `cv` | — | Close the valve |
| `ver` | — | Report software version |
| `time` | — | Report Pi date/time |
| `ip` | — | Report current IP address |
| `temp` | — | Report CPU temperature |
| `apn` | — | Report current APN |
| `apn` | `new.apn.value` | Change APN and reinit modem |
| `restart` | — | Reboot the Pi |
| `off` | — | Shutdown the Pi |
| `lowdata` | — | Enable compact message format |
| `nolowdata` | — | Enable verbose message format |
| `listbroker` | — | List all configured brokers |
| `selectbroker` | `index` | Switch to broker at index |
| `insertbroker` | `index` `hostname` | Add broker to list |
| `removebroker` | `index` | Remove broker from list |
| `setdeviceid` | `id` | Change MACHINE_ID |
| `setclientid` | `id` | Change BUILDING ID |
| `password` | `newpwd` | Change operator password |
| `testled` | `RED\|GREEN` | Test indicator LEDs |

### Examples

```
*12345678*status
*12345678*openvalve
*12345678*closevalve
*12345678*apn
*12345678*apn*go.mono
*12345678*selectbroker*4
*12345678*restart
```

---

## Service Management

```bash
# Start / stop / restart
sudo systemctl start valve
sudo systemctl stop valve
sudo systemctl restart valve

# Check status
sudo systemctl status valve

# Follow live logs
journalctl -u valve -f

# View last 100 log lines
journalctl -u valve -n 100
```

---

## Modem Troubleshooting

Run the standalone modem diagnostic script:

```bash
bash ~/likk-large-valve/scripts/modem_setup.sh
```

This will check enumeration, serial ports, QMI device, raw_ip mode, start a data session, configure `wwan0`, and test internet connectivity independently of the valve controller.

### Common issues

**`No modems were found` (mmcli)**
ModemManager is disabled intentionally. The script manages the modem directly via `qmicli`. This is expected.

**`Permission denied: /sys/class/net/wwan0/qmi/raw_ip`**
The udev rule should handle this automatically. If not:
```bash
echo 'Y' | sudo tee /sys/class/net/wwan0/qmi/raw_ip
```

**`QMI start-network failed: Operating mode not online`**
```bash
sudo qmicli -d /dev/cdc-wdm0 --dms-set-operating-mode=online
```

**`Client IDs exhausted`**
A stale QMI session is open. Unplug and replug the modem USB cable, then restart the service.

**No internet after QMI session starts**
The GL7611 uses `wwan0` (QMI) for data, not `usb0` (CDC-Ethernet). Confirm:
```bash
ip addr show wwan0    # should show inet 10.x.x.x/32
ip route show         # should show default via 10.x.x.69 dev wwan0 onlink
```

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Raspberry Pi                         │
│                                                         │
│  valve_controller.py                                    │
│    ├── modem()          QMI session via qmicli          │
│    ├── bring_up_iface() Configure wwan0 (/32 + onlink)  │
│    ├── disco()          Reconnect on drop               │
│    ├── handleMQTT()     Process remote commands         │
│    └── main loop        Heartbeat, valve poll, buttons  │
│                                                         │
│  valve.py               GPIO → motor driver → valve     │
└────────────┬────────────────────────────────────────────┘
             │ USB
┌────────────▼────────────┐
│  Sierra Wireless GL7611  │
│  ttyUSB2  → AT commands  │
│  cdc-wdm0 → QMI control  │
│  wwan0    → data path    │
└────────────┬────────────┘
             │ LTE (AT&T IoT, APN: data.mono)
             │
┌────────────▼────────────┐
│   MQTT Broker            │
│   kmqtt55.likk-h2o.com  │
│   port 1883              │
└────────────┬────────────┘
             │
┌────────────▼────────────┐
│   Alarm / Control App    │
│   (water leak detector,  │
│    mobile app, etc.)     │
└─────────────────────────┘
```

---

## Version History

| Version | Changes |
|---|---|
| 201 | Initial release — SIM7600H-H modem, paho-mqtt, basic MQTT commands |
| 202 | Fixed keepalive (180→60s), fixed port (883→1883), fixed disco() double-call, added subprocess error checking, moved credentials to Secrets.py |
| 203 | Added `apn` command with persistence; migrated from SIM7600 AT commands to GL7611 QMI/wwan0; fixed ARP tunnel issue; added carrier DNS from AT+CGCONTRDP |

---

## License

MIT License — see LICENSE file.

---

## Contributing

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/my-change`
3. Commit your changes: `git commit -m "Add my change"`
4. Push and open a Pull Request

**Never commit `Secrets.py` or `*.dat` files.**
