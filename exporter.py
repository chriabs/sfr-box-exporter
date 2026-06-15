#!/usr/bin/env python3
"""
Prometheus exporter for SFR NB6VAC box.

Exposes:
  - WAN IPv4/IPv6 status and uptime
  - Box uptime, temperature, supply voltage
  - Switch port TX/RX bytes (LAN1-4 + FIBRE)
  - WiFi clients count and signal (dBm) per MAC

Configuration via environment variables:
  BOX_URL   (default: http://192.168.1.1)
  BOX_USER  (default: admin)
  BOX_PASS  (required) — local admin password printed on the box label
  EXPORTER_PORT (default: 9101)
"""
import os, time, re, hashlib, hmac, urllib.request, urllib.parse, http.cookiejar
import xml.etree.ElementTree as ET
from prometheus_client import start_http_server, Gauge

BOX_URL  = os.getenv("BOX_URL",  "http://192.168.1.1")
BOX_USER = os.getenv("BOX_USER", "admin")
BOX_PASS = os.getenv("BOX_PASS", "")
PORT     = int(os.getenv("EXPORTER_PORT", "9101"))

# WAN
wan_up      = Gauge("sfr_box_wan_up",             "WAN IPv4 status (1=up, 0=down)")
wan_uptime  = Gauge("sfr_box_wan_uptime_seconds", "WAN connection uptime in seconds")
wan_ipv6_up = Gauge("sfr_box_wan_ipv6_up",        "WAN IPv6 status (1=up, 0=down)")

# System
sys_uptime  = Gauge("sfr_box_uptime_seconds",      "Box uptime in seconds")
sys_temp    = Gauge("sfr_box_temperature_celsius", "Box CPU temperature in Celsius")
sys_voltage = Gauge("sfr_box_voltage_volts",       "Box power supply voltage in Volts")

# Switch ports (label: port = LAN1/LAN2/LAN3/LAN4/FIBRE)
port_rx = Gauge("sfr_box_switch_rx_bytes", "Switch port total bytes received", ["port"])
port_tx = Gauge("sfr_box_switch_tx_bytes", "Switch port total bytes sent",     ["port"])

# WiFi
wifi_clients = Gauge("sfr_box_wifi_clients_total", "Number of connected WiFi clients")
wifi_signal  = Gauge("sfr_box_wifi_signal_dbm",    "WiFi client signal in dBm", ["mac"])

# Health
scrape_ok = Gauge("sfr_box_scrape_success", "1 if the last scrape fully succeeded")


# ---------------------------------------------------------------------------
def fetch_api(opener, method):
    with opener.open(f"{BOX_URL}/api/1.0/?method={method}", timeout=5) as r:
        return ET.fromstring(r.read().decode())


def get_session():
    """Authenticate and return an opener carrying the session cookie."""
    cj     = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

    req = urllib.request.Request(
        f"{BOX_URL}/login", data=b"action=challenge",
        headers={"Content-Type":     "application/x-www-form-urlencoded",
                 "X-Requested-With": "XMLHttpRequest"}
    )
    with opener.open(req, timeout=5) as r:
        challenge = ET.fromstring(r.read().decode()).findtext("challenge")

    # Hash = HMAC-SHA256(key=challenge, msg=SHA256(field)) for login then password
    def hmac_sha256(key, msg):
        return hmac.new(key.encode(), msg.encode(), hashlib.sha256).hexdigest()

    h_login = hmac_sha256(challenge, hashlib.sha256(BOX_USER.encode()).hexdigest())
    h_pass  = hmac_sha256(challenge, hashlib.sha256(BOX_PASS.encode()).hexdigest())

    data = urllib.parse.urlencode({
        "method": "passwd", "page_ref": "", "zsid": challenge,
        "hash":   h_login + h_pass, "login": "", "password": ""
    }).encode()
    opener.open(urllib.request.Request(
        f"{BOX_URL}/login", data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"}
    ), timeout=5)
    return opener


def parse_bytes(s):
    """Convert human-readable size ('2.48 GB', '724 MB' …) to integer bytes."""
    m = re.match(r"([\d.]+)\s*(GB|MB|KB|B)", s.strip(), re.I)
    if not m:
        return 0
    val, unit = float(m.group(1)), m.group(2).upper()
    return int(val * {"GB": 1_000_000_000, "MB": 1_000_000, "KB": 1_000, "B": 1}[unit])


# ---------------------------------------------------------------------------
def collect_unauthenticated():
    """WAN + system metrics — no login needed."""
    opener = urllib.request.build_opener()
    ok = True

    try:
        el = fetch_api(opener, "wan.getInfo").find("wan")
        if el is not None:
            wan_up.set(1 if el.get("status") == "up" else 0)
            wan_uptime.set(int(el.get("uptime") or 0))
            wan_ipv6_up.set(1 if el.get("status6") == "up" else 0)
        else:
            wan_up.set(0)
    except Exception as e:
        print(f"[wan] {e}", flush=True)
        wan_up.set(0)
        ok = False

    try:
        el = fetch_api(opener, "system.getInfo").find("system")
        if el is not None:
            sys_uptime.set(int(el.get("uptime") or 0))
            sys_temp.set(float(el.get("temperature") or 0) / 1000)
            sys_voltage.set(float(el.get("alimvoltage") or 0) / 1000)
    except Exception as e:
        print(f"[system] {e}", flush=True)
        ok = False

    return ok


def collect_authenticated():
    """Switch port stats + WiFi clients — requires login."""
    if not BOX_PASS:
        return True  # skip silently if no password configured

    try:
        opener = get_session()
    except Exception as e:
        print(f"[auth] {e}", flush=True)
        return False

    try:
        with opener.open(f"{BOX_URL}/state/lan", timeout=10) as r:
            body = r.read().decode()

        # Switch port stats — ports listed in table header, values in matching <td> cells
        port_names = [p.replace(" ", "") for p in re.findall(r"Port (LAN \d+|FIBRE)", body)]
        def _row_vals(label):
            m = re.search(re.escape(label) + r".*?</th>(.*?)</tr>", body, re.DOTALL)
            return re.findall(r"<td>\s*(.*?)\s*</td>", m.group(1)) if m else []
        tx_vals = _row_vals("Compteur octets émis")
        rx_vals = _row_vals("Compteur octets reçus")
        for i, name in enumerate(port_names):
            if i < len(tx_vals):
                port_tx.labels(port=name).set(parse_bytes(tx_vals[i]))
            if i < len(rx_vals):
                port_rx.labels(port=name).set(parse_bytes(rx_vals[i]))

        # WiFi clients — each client is in a separate wlanhost_stats table
        wifi_sections = re.findall(r"wlanhost_stats.*?</table>", body, re.DOTALL)
        macs, signals = [], []
        for sec in wifi_sections:
            mac_m = re.search(r"Adresse MAC.*?<td>\s*([\da-fA-F:]{17})\s*</td>", sec, re.DOTALL)
            sig_m = re.search(r'class="rssidb">\s*&nbsp;\s*(-?\d+)\s*dB', sec)
            if mac_m:
                macs.append(mac_m.group(1).strip().lower())
            if sig_m:
                signals.append(int(sig_m.group(1)))
        wifi_clients.set(len(macs))
        for mac, sig in zip(macs, signals):
            wifi_signal.labels(mac=mac).set(sig)

    except Exception as e:
        print(f"[lan] {e}", flush=True)
        return False

    return True


def collect():
    ok1 = collect_unauthenticated()
    ok2 = collect_authenticated()
    scrape_ok.set(1 if ok1 and ok2 else 0)


if __name__ == "__main__":
    if not BOX_PASS:
        print("WARNING: BOX_PASS not set — switch/WiFi metrics will be skipped.", flush=True)
    start_http_server(PORT)
    print(f"SFR Box exporter started on :{PORT}", flush=True)
    while True:
        collect()
        time.sleep(30)
