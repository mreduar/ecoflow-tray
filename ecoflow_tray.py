"""
EcoFlow Tray - a Windows system-tray battery monitor for EcoFlow power stations.

Shows the battery percentage as a live tray icon. Right-click for a menu with
device status, a Settings dialog (enter your API keys, pick your device, set the
polling interval) and Quit.

Optionally sends Telegram alerts when grid power is lost or restored, and when
the battery drops past two configurable levels. Each user supplies their own bot
token, so no server is involved - alerts are plain HTTPS calls to the Bot API.
The same four events can launch a local program (an .exe, a .bat or a Windows
.lnk shortcut), each with its own delay, so a power cut can drive whatever
automation the user already has.

Credentials are stored per-user in %APPDATA%\\EcoFlowTray\\config.json. The secret
key and bot token are encrypted at rest with Windows DPAPI (tied to the current
user account).

Modes:
    EcoFlowTray.exe                Start the tray app (Settings opens on first run)
    ecoflow_tray.py --selftest     Import/render checks, no network, exit
    ecoflow_tray.py --make-icon P  Write the app .ico to path P and exit
"""

import base64
import ctypes
import functools
import hashlib
import hmac
import html
import json
import os
import queue
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import winreg
from ctypes import wintypes
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import paho.mqtt.client as mqtt
import pystray
from PIL import Image, ImageChops, ImageDraw, ImageFont

APP_NAME = "EcoFlowTray"
APP_TITLE = "EcoFlow Tray"
CONFIG_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / APP_NAME
CONFIG_PATH = CONFIG_DIR / "config.json"

HOSTS = {
    "Global (api.ecoflow.com)": "https://api.ecoflow.com",
    "Europe (api-e.ecoflow.com)": "https://api-e.ecoflow.com",
}
DEFAULT_HOST = HOSTS["Global (api.ecoflow.com)"]
DEFAULT_SOC_FIELD = "bms_emsStatus.f32LcdShowSoc"
CUSTOM_SOC_LABEL = "Custom field…"
DEFAULT_REFRESH = 60
UNKNOWN_TIME = 5999  # EcoFlow sentinel for "no estimate available"

# Grid (AC input) watch + Telegram alerts
DEFAULT_GRID_FIELD = "inv.acInVol"
DEFAULT_GRID_THRESHOLD = 80000  # mV - see default_grid_threshold()
DEFAULT_OUTAGE_DELAY_MIN = 1.0
DEFAULT_RESTORE_DELAY_MIN = 1.0
DEFAULT_BATT_ALERT_1 = 30
DEFAULT_BATT_ALERT_2 = 15

# Running local programs on the same four events. Each event ("kind") owns one
# path + arguments slot; each channel confirms the grid signal on its own delay,
# so a program can run at once while the Telegram message waits a minute.
EXEC_KINDS = ("outage", "restore", "batt1", "batt2")
CHANNELS = ("alert", "exec")
BATT_KIND = {"batt_alert_1": "batt1", "batt_alert_2": "batt2"}
DEFAULT_EXEC_OUTAGE_DELAY_MIN = 0.0
DEFAULT_EXEC_RESTORE_DELAY_MIN = 0.0
DELAY_DEFAULTS = {
    "outage_delay_min": DEFAULT_OUTAGE_DELAY_MIN,
    "restore_delay_min": DEFAULT_RESTORE_DELAY_MIN,
    "exec_outage_delay_min": DEFAULT_EXEC_OUTAGE_DELAY_MIN,
    "exec_restore_delay_min": DEFAULT_EXEC_RESTORE_DELAY_MIN,
}


# --------------------------------------------------------------------------- #
# Secret storage - Windows DPAPI via ctypes (no extra dependency)
# --------------------------------------------------------------------------- #
class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _to_blob(data: bytes) -> _Blob:
    buf = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _from_blob(blob: _Blob) -> bytes:
    return ctypes.string_at(blob.pbData, blob.cbData)


def dpapi_encrypt(text: str):
    """Return base64(DPAPI blob) for text, or None if DPAPI is unavailable."""
    try:
        src, out = _to_blob(text.encode("utf-8")), _Blob()
        ok = ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out)
        )
        if not ok:
            return None
        try:
            return base64.b64encode(_from_blob(out)).decode("ascii")
        finally:
            ctypes.windll.kernel32.LocalFree(out.pbData)
    except Exception:
        return None


def dpapi_decrypt(b64: str) -> str:
    src, out = _to_blob(base64.b64decode(b64)), _Blob()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out)
    )
    if not ok:
        raise OSError("CryptUnprotectData failed")
    try:
        return _from_blob(out).decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
_SECRET_FIELDS = ("secret_key", "telegram_token")

CONFIG_DEFAULTS = {
    "host": DEFAULT_HOST,
    "refresh_seconds": DEFAULT_REFRESH,
    "soc_field": DEFAULT_SOC_FIELD,
    "telegram_enabled": False,
    "telegram_chat_id": "",
    "grid_field": DEFAULT_GRID_FIELD,
    "grid_threshold": DEFAULT_GRID_THRESHOLD,
    "outage_delay_min": DEFAULT_OUTAGE_DELAY_MIN,
    "restore_delay_min": DEFAULT_RESTORE_DELAY_MIN,
    "batt_alert_1": DEFAULT_BATT_ALERT_1,
    "batt_alert_2": DEFAULT_BATT_ALERT_2,
    "exec_enabled": False,
    "exec_outage_delay_min": DEFAULT_EXEC_OUTAGE_DELAY_MIN,
    "exec_restore_delay_min": DEFAULT_EXEC_RESTORE_DELAY_MIN,
    # an empty path turns that slot off
    **{f"exec_{kind}_{part}": "" for kind in EXEC_KINDS for part in ("path", "args")},
}


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return dict(CONFIG_DEFAULTS)
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    for name in _SECRET_FIELDS:
        if cfg.get(f"{name}_enc"):
            try:
                cfg[name] = dpapi_decrypt(cfg[f"{name}_enc"])
            except Exception:
                cfg[name] = ""
        cfg.setdefault(name, "")
    for key, value in CONFIG_DEFAULTS.items():
        cfg.setdefault(key, value)
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    out = {
        "access_key": cfg.get("access_key", ""),
        "sn": cfg.get("sn", ""),
        "device_name": cfg.get("device_name", ""),
        "host": cfg.get("host", DEFAULT_HOST),
        "refresh_seconds": int(cfg.get("refresh_seconds", DEFAULT_REFRESH)),
        "soc_field": cfg.get("soc_field", DEFAULT_SOC_FIELD),
        "telegram_enabled": bool(cfg.get("telegram_enabled", False)),
        "telegram_chat_id": str(cfg.get("telegram_chat_id", "")),
        "grid_field": cfg.get("grid_field", DEFAULT_GRID_FIELD),
        "grid_threshold": float(cfg.get("grid_threshold", DEFAULT_GRID_THRESHOLD)),
        "outage_delay_min": float(cfg.get("outage_delay_min", DEFAULT_OUTAGE_DELAY_MIN)),
        "restore_delay_min": float(cfg.get("restore_delay_min", DEFAULT_RESTORE_DELAY_MIN)),
        "batt_alert_1": int(cfg.get("batt_alert_1", DEFAULT_BATT_ALERT_1)),
        "batt_alert_2": int(cfg.get("batt_alert_2", DEFAULT_BATT_ALERT_2)),
        "exec_enabled": bool(cfg.get("exec_enabled", False)),
        "exec_outage_delay_min": float(
            cfg.get("exec_outage_delay_min", DEFAULT_EXEC_OUTAGE_DELAY_MIN)),
        "exec_restore_delay_min": float(
            cfg.get("exec_restore_delay_min", DEFAULT_EXEC_RESTORE_DELAY_MIN)),
    }
    # Coerced to str: a hand-edited config with a number here would otherwise
    # blow up inside the launcher thread, where nobody sees the traceback.
    for kind in EXEC_KINDS:
        for part in ("path", "args"):
            out[f"exec_{kind}_{part}"] = str(cfg.get(f"exec_{kind}_{part}", "") or "")
    for name in _SECRET_FIELDS:
        value = cfg.get(name, "")
        enc = dpapi_encrypt(value) if value else None
        if enc:
            out[f"{name}_enc"] = enc
        else:
            out[name] = value  # plaintext fallback if DPAPI is unavailable
    CONFIG_PATH.write_text(json.dumps(out, indent=2), encoding="utf-8")


def is_configured(cfg: dict) -> bool:
    return bool(cfg.get("access_key") and cfg.get("secret_key") and cfg.get("sn"))


def watching_grid(cfg: dict) -> bool:
    """True while anything acts on the AC-input field, so it must stay fresh."""
    return bool(cfg.get("telegram_enabled") or cfg.get("exec_enabled"))


# --------------------------------------------------------------------------- #
# Start-with-Windows (per-user HKCU Run key - no admin, no shortcut file)
# --------------------------------------------------------------------------- #
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_VALUE = "EcoFlowTray"


def _launch_command() -> str:
    """The command Windows should run at logon to start this app."""
    if getattr(sys, "frozen", False):  # packaged .exe
        return f'"{sys.executable}"'
    # dev mode: run the script with pythonw so no console window appears
    pyw = Path(sys.executable).with_name("pythonw.exe")
    exe = str(pyw) if pyw.exists() else sys.executable
    return f'"{exe}" "{os.path.abspath(sys.argv[0])}"'


def autostart_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            winreg.QueryValueEx(key, _RUN_VALUE)
        return True
    except OSError:
        return False


def set_autostart(enabled: bool) -> None:
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
        if enabled:
            winreg.SetValueEx(key, _RUN_VALUE, 0, winreg.REG_SZ, _launch_command())
        else:
            try:
                winreg.DeleteValue(key, _RUN_VALUE)
            except FileNotFoundError:
                pass


# --------------------------------------------------------------------------- #
# EcoFlow API (HMAC-SHA256 signed requests)
# --------------------------------------------------------------------------- #
def _flatten(obj, prefix=""):
    result = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            result.update(_flatten(v, f"{prefix}.{k}" if prefix else k))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            result.update(_flatten(item, f"{prefix}[{i}]"))
    else:
        result[prefix] = obj
    return result


def _query_string(params):
    return "&".join(f"{k}={params[k]}" for k in sorted(params))


def _sign(cfg, params, nonce, timestamp):
    auth = {"accessKey": cfg["access_key"], "nonce": nonce, "timestamp": timestamp}
    sign_str = (_query_string(_flatten(params)) + "&" if params else "") + _query_string(auth)
    return hmac.new(cfg["secret_key"].encode(), sign_str.encode(), hashlib.sha256).hexdigest()


def api_get(cfg, path, params=None):
    nonce = str(random.randint(100000, 999999))
    timestamp = str(int(time.time() * 1000))
    headers = {
        "accessKey": cfg["access_key"],
        "nonce": nonce,
        "timestamp": timestamp,
        "sign": _sign(cfg, params, nonce, timestamp),
    }
    url = cfg["host"] + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=15) as resp:
        payload = json.loads(resp.read().decode())
    if str(payload.get("code")) != "0":
        raise RuntimeError(payload.get("message", f"API error (code {payload.get('code')})"))
    return payload.get("data")


def list_devices(cfg):
    return api_get(cfg, "/iot-open/sign/device/list") or []


def fetch_full_quota(cfg):
    """HTTP snapshot of the full device quota (cached in the cloud)."""
    return api_get(cfg, "/iot-open/sign/device/quota/all", {"sn": cfg["sn"]}) or {}


def reading_from_quota(quota, soc_field):
    """Derive the display reading from a (merged) quota dict."""
    soc = int(round(float(quota.get(soc_field, 0))))
    watts_in = int(round(float(quota.get("pd.wattsInSum", 0))))
    watts_out = int(round(float(quota.get("pd.wattsOutSum", 0))))
    net = watts_in - watts_out
    if net > 5:
        state, remain = "Charging", quota.get("bms_emsStatus.chgRemainTime", UNKNOWN_TIME)
    elif net < -5:
        state, remain = "Discharging", quota.get("bms_emsStatus.dsgRemainTime", UNKNOWN_TIME)
    else:
        state, remain = "Idle", UNKNOWN_TIME
    return {
        "soc": soc,
        "state": state,
        "charging": net > 5,
        "watts_in": watts_in,
        "watts_out": watts_out,
        "remain_min": None if remain in (0, UNKNOWN_TIME) else int(remain),
    }


# Fields ranked as "what the EcoFlow app shows" first, then raw pack SoC.
SOC_PRIORITY = [
    "bms_emsStatus.f32LcdShowSoc", "bms_emsStatus.lcdShowSoc",
    "ems.f32LcdShowSoc", "ems.lcdShowSoc",
    "bms_emsStatus.f32LcdSoc",
    "cmsBattSoc", "cms_batt_soc",
    "hs_yj751_pd_appshow_addr.soc",
    "pd.soc", "inv.soc",
    "bms_bmsStatus.f32ShowSoc", "bmsMaster.f32ShowSoc",
    "bms_bmsStatus.soc", "bmsMaster.soc", "bmsBattSoc", "bms_batt_soc",
]
# Substrings that mark a config/limit field rather than a live reading.
_SOC_EXCLUDE = ("max", "min", "diff", "target", "design", "soh",
                "cyc", "remain", "cap", "ocv", "bppower")


def soc_candidates(data):
    """From a quota dict, return [(field, value)] of plausible battery-% fields,
    best 'app-shown' guess first."""
    out = []
    for k, v in (data or {}).items():
        kl = k.lower()
        if "soc" not in kl or not isinstance(v, (int, float)):
            continue
        if any(x in kl for x in _SOC_EXCLUDE):
            continue
        out.append((k, v))
    out.sort(key=lambda kv: (SOC_PRIORITY.index(kv[0]) if kv[0] in SOC_PRIORITY else 999, kv[0]))
    return out


# Fields that report AC (grid) input, best first. Voltage beats watts: a full
# battery stops drawing power, so watts drop to 0 while the grid is still there.
GRID_PRIORITY = [
    # Classic line (DELTA 2/Max, DELTA Pro/Max/Mini, RIVER 2/Pro/Max)
    "inv.acInVol", "inv.acInAmp", "inv.acInFreq",
    "inv.inputWatts", "inv.acInputWatts",
    # Newer line (DELTA 3, DELTA Pro 3, RIVER 3) renamed everything
    "plug_in_info_ac_in_vol", "plug_in_info_ac_in_amp", "pow_get_ac_in",
    "bms_emsStatus.chgLinePlug",
    # Totals last: they fold solar in, so they can mask an outage
    "pd.wattsInSum", "pow_in_sum_w",
]
_GRID_MATCH = ("acinvol", "acinamp", "acinfreq", "acinputwatts", "inputwatts",
               "wattsinsum", "powgetacin", "powinsumw")
# Solar input must not count as "the grid is up".
_GRID_EXCLUDE_PREFIX = ("mppt.", "pv.", "bms_")
# Offered despite the bms_ prefix: "AC cable connected". Not a 0/1 flag - a
# DELTA 2 Max reads 0 unplugged and 34 plugged in - but zero vs non-zero is a
# cleaner signal than any voltage threshold on devices that report it.
_GRID_ALLOW = ("bms_emsStatus.chgLinePlug", "ems.chgLinePlug")


def grid_candidates(data):
    """From a quota dict, return [(field, value)] of plausible AC-input fields,
    best 'is the grid up?' signal first."""
    out = []
    for k, v in (data or {}).items():
        kl = k.lower()
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            continue
        if k not in _GRID_ALLOW:
            if kl.startswith(_GRID_EXCLUDE_PREFIX):
                continue
            if not any(p in kl.replace("_", "") for p in _GRID_MATCH):
                continue
        out.append((k, v))
    out.sort(key=lambda kv: (GRID_PRIORITY.index(kv[0]) if kv[0] in GRID_PRIORITY else 999, kv[0]))
    return out


def default_grid_threshold(field, value=0):
    """Pick a sensible 'grid is present' threshold for a field.

    Voltage does NOT fall to zero when the mains drop: a DELTA 2 Max idles at
    ~36 V / 41 Hz on an open input. So the bar has to sit well above that idle
    reading but below any real mains voltage (100 V and up). Devices report
    volts either in mV or V, so the observed magnitude picks the unit.
    """
    fl = field.lower()
    if fl.endswith("plug"):   # "AC cable connected": zero vs non-zero, e.g.
        return 0              # chgLinePlug. Must not catch ..._ac_in_vol.
    if "vol" in fl:
        return 80000 if value >= 1000 else 80
    if "freq" in fl:
        return 45   # clears a ~41 Hz idle, still under both 50 and 60 Hz mains
    if "amp" in fl:
        return 0    # current reads a hard 0 with no mains, in A or mA alike
    return 5        # watts idle at zero, give or take sensor noise


# --------------------------------------------------------------------------- #
# Live updates over MQTT
# The HTTP quota is a cached snapshot that only refreshes when a session is
# active (e.g. the phone app). MQTT is the live stream the app uses; it pushes
# updates on its own and does NOT need the phone app to be open.
# --------------------------------------------------------------------------- #
# MQTT messages use "status" module names; map them back to the HTTP field names.
_MQTT_STATUS_TO_PLAIN = {
    "pdStatus": "pd", "mpptStatus": "mppt", "emsStatus": "bms_emsStatus",
    "bmsStatus": "bms_bmsStatus", "bmsInfo": "bms_bmsInfo", "invStatus": "inv",
    "bmsSlaveStatus": "bms_slave", "bmsSlaveStatus_1": "bms_slave_bmsSlaveStatus_1",
    "bmsSlaveStatus_2": "bms_slave_bmsSlaveStatus_2",
}


def mqtt_to_plain(raw):
    """Convert one MQTT quota message into a flat HTTP-style {field: value} dict."""
    prefix = ""
    type_code = raw.get("typeCode")
    if type_code:
        prefix = _MQTT_STATUS_TO_PLAIN.get(type_code, "unknown_" + type_code) + "."
    elif "cmdFunc" in raw and "cmdId" in raw:
        prefix = f"{raw['cmdFunc']}_{raw['cmdId']}."
    flat = {}
    for src in ("param", "params"):
        block = raw.get(src)
        if isinstance(block, dict):
            for k, v in block.items():
                flat[f"{prefix}{k}"] = v
                if isinstance(v, dict):  # flatten one nested level
                    for k2, v2 in v.items():
                        flat[f"{prefix}{k}.{k2}"] = v2
    return flat


class EcoflowMqtt:
    """Subscribes to the device's live quota topic and pushes merged updates."""

    def __init__(self, cfg, on_update, on_status):
        self.cfg = cfg
        self.on_update = on_update   # called with a flat {field: value} dict
        self.on_status = on_status   # called with a short status string
        self.client = None
        self._stopped = False

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            data = api_get(self.cfg, "/iot-open/sign/certification")
            host, port = data["url"], int(data["port"])
            user, password = data["certificateAccount"], data["certificatePassword"]
        except Exception as err:
            self.on_status(f"MQTT auth failed: {err}")
            return
        self.topic = f"/open/{user}/{self.cfg['sn']}/quota"
        # Stable client_id: EcoFlow allows only ~10 unique client IDs per day.
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"EcoFlowTray-{user}")
        client.username_pw_set(user, password)
        client.tls_set()
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.reconnect_delay_set(min_delay=2, max_delay=60)
        self.client = client
        try:
            client.connect(host, port, keepalive=60)
            client.loop_forever(retry_first_connection=True)
        except Exception as err:
            if not self._stopped:
                self.on_status(f"MQTT error: {err}")

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if getattr(reason_code, "is_failure", False):
            self.on_status(f"MQTT rejected: {reason_code}")
        else:
            client.subscribe(self.topic, qos=1)
            self.on_status("Live (MQTT)")

    def _on_message(self, client, userdata, msg):
        try:
            self.on_update(mqtt_to_plain(json.loads(msg.payload.decode())))
        except Exception:
            pass

    def stop(self):
        self._stopped = True
        if self.client:
            try:
                self.client.disconnect()
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# Telegram Bot API
# Plain HTTPS calls - no server, no webhook. Each user creates their own bot
# with @BotFather, pastes the token here, and sends it /start once so the chat
# ID can be detected.
# --------------------------------------------------------------------------- #
def telegram_call(token, method, params=None, timeout=20):
    url = f"https://api.telegram.org/bot{urllib.parse.quote(token, safe='')}/{method}"
    data = urllib.parse.urlencode(params or {}).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as err:  # Telegram puts the reason in the body
        try:
            payload = json.loads(err.read().decode())
        except Exception:
            raise RuntimeError(f"HTTP {err.code}") from None
    if not payload.get("ok"):
        raise RuntimeError(payload.get("description", "Telegram API error"))
    return payload.get("result")


def telegram_check_token(token):
    """Return the bot's @username, raising if the token is invalid."""
    return (telegram_call(token, "getMe") or {}).get("username", "?")


def telegram_send(token, chat_id, text):
    telegram_call(token, "sendMessage", {
        "chat_id": chat_id, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": "true",
    })


def telegram_detect_chat(token):
    """Return (chat_id, display name) from the newest message sent to the bot,
    or (None, None) if nobody has messaged it yet."""
    for upd in reversed(telegram_call(token, "getUpdates", {"limit": 20, "timeout": 0}) or []):
        msg = upd.get("message") or upd.get("edited_message") or upd.get("channel_post")
        chat = (msg or {}).get("chat") or {}
        if chat.get("id") is not None:
            name = chat.get("title") or " ".join(
                x for x in (chat.get("first_name"), chat.get("last_name")) if x
            ) or chat.get("username") or str(chat["id"])
            return str(chat["id"]), name
    return None, None


# --------------------------------------------------------------------------- #
# Running local programs on power events
# The user picks a program per event; the app launches it exactly as a double
# click would, so Windows shortcuts and script associations both work.
# --------------------------------------------------------------------------- #
def resolve_command_path(path):
    """The absolute path run_command would actually launch.

    "Copy as path" in Explorer wraps the path in quotes, and a process started
    from the HKCU Run key has its CWD in C:\\Windows\\system32 - so a relative
    path saved from a dev run would resolve somewhere else entirely.
    """
    return os.path.abspath(os.path.expanduser(os.path.expandvars(str(path).strip().strip('"'))))


def run_command(path, args=""):
    """Launch path the way a double click would.

    ShellExecute is the only thing that resolves .lnk shortcuts and script
    associations like .vbs - CreateProcess, which is what subprocess uses,
    can do neither.
    """
    if not hasattr(os, "startfile"):
        raise RuntimeError("Launching programs is only supported on Windows")
    target = resolve_command_path(path)
    # a .lnk carries its own "Start in" folder; passing cwd would override it
    cwd = None if target.lower().endswith(".lnk") else (os.path.dirname(target) or None)
    os.startfile(target, arguments=args, cwd=cwd)


def _co_initialize():
    """Give the calling thread a COM apartment.

    ShellExecute resolves a .lnk through COM. Harmless when one already exists.
    """
    try:
        ctypes.windll.ole32.CoInitializeEx(None, 2)   # COINIT_APARTMENTTHREADED
    except Exception:
        pass


_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def render_args(args, values):
    """Substitute {name} placeholders, leaving anything else untouched.

    Deliberately not str.format: that raises KeyError on an unknown name,
    ValueError on a stray brace and IndexError on "{}". Here a typo simply
    survives into the command line, where the user can see it.
    """
    return _PLACEHOLDER.sub(lambda m: str(values.get(m.group(1), m.group(0))), str(args or ""))


def exec_values(event, reading, device, when):
    """The placeholder dict for render_args - always every key, "" where unknown.

    {time} uses a format without spaces so it can't split one argument in two.
    """
    reading = reading or {}
    return {
        "event": event,
        "soc": reading.get("soc", ""),
        "device": device or "",
        "watts_out": reading.get("watts_out", ""),
        "watts_in": reading.get("watts_in", ""),
        "time": time.strftime("%Y-%m-%dT%H:%M", time.localtime(when)),
    }


# --------------------------------------------------------------------------- #
# Alerting - debounced grid-outage and battery-level notifications
# The house losing power usually takes the router down too, so a send can fail
# for reasons that have nothing to do with the message. Alerts are queued and
# retried, and each one carries the timestamp of the event, not of the send.
# The same events also drive the user's local programs, which need no network
# and so run on their own (usually much shorter) delays.
# --------------------------------------------------------------------------- #
def _fmt_duration(seconds):
    minutes = max(0, int(seconds // 60))
    h, m = divmod(minutes, 60)
    return f"{h}h {m}m" if h else f"{m}m"


def _fmt_clock(when):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(when))


def _alert_body(reading):
    parts = [f"Battery {reading['soc']}%"]
    if reading["watts_out"]:
        parts.append(f"Load {reading['watts_out']} W")
    if reading["watts_in"]:
        parts.append(f"In {reading['watts_in']} W")
    if reading["remain_min"] is not None:
        parts.append(f"{_fmt_remain(reading['remain_min'])}")
    return " · ".join(parts)


class Alerter:
    """Watches AC input and battery level, pushes Telegram alerts and launches
    the user's programs.

    Transitions must hold for the user's configured delay before they count, so
    a brief flicker doesn't fire anything. The raw signal is shared, but each
    channel ("alert" and "exec") confirms it on its own delay - that is how a
    program can run immediately while the message waits a minute. State is
    tracked even while Telegram and executions are off, so enabling one never
    dumps a backlog of stale events.
    """

    TICK_SECONDS = 10
    REARM_MARGIN = 5       # % the battery must climb back before an alert re-arms
    MAX_AGE_SECONDS = 6 * 3600
    RETRY_DELAYS = (15, 30, 60, 120, 300, 600)

    def __init__(self, get_cfg, on_status=None):
        self.get_cfg = get_cfg
        self.on_status = on_status or (lambda text: None)
        self.lock = threading.Lock()
        # evaluate() runs from three threads (paho, the HTTP worker and the tick
        # loop below). Without this a duplicate Telegram message was the worst
        # case; with executions it would launch a program twice.
        # Lock order: eval_lock is ALWAYS taken before self.lock, never after.
        self.eval_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.outbox = []          # [{"text", "created", "tries", "next_try"}]
        self.latest = None        # (reading, grid_present) from the last update
        self.raw = None           # last instantaneous grid state seen
        self.raw_since = 0.0      # when that value first showed up
        self.grid = {}            # {channel: confirmed grid state}
        self.grid_since = {}      # {channel: when that confirmed state began}
        self.batt_armed = {}      # {config key: bool}
        self.batt_level = {}      # {config key: threshold it was armed at}

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self.stop_event.set()

    def reset(self):
        """Forget tracked state (device or watched field changed).

        All four grid fields go together: "raw is None" must mean "both dicts
        are empty", or _grid_events raises KeyError inside observe() - and
        EcoflowMqtt swallows that, leaving the tray silently frozen.
        """
        with self.eval_lock, self.lock:
            self.latest = None
            self.raw, self.raw_since = None, 0.0
            self.grid, self.grid_since = {}, {}
            self.batt_armed.clear()
            self.batt_level.clear()

    # -- observation ------------------------------------------------------- #
    def observe(self, quota, reading):
        cfg = self.get_cfg()
        value = quota.get(cfg.get("grid_field", DEFAULT_GRID_FIELD))
        present = None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            present = float(value) > float(cfg.get("grid_threshold", DEFAULT_GRID_THRESHOLD))
        with self.lock:
            self.latest = (reading, present)
        self.evaluate()

    def evaluate(self):
        with self.lock:
            snapshot = self.latest
        if not snapshot:
            return
        reading, present = snapshot
        cfg, now = self.get_cfg(), time.time()
        device = cfg.get("device_name") or "EcoFlow"
        launches = []
        with self.eval_lock:
            fresh = self._track_raw(present, now)
            if not fresh:      # never fire on the observation that starts a transition
                event = self._grid_events("alert", cfg, now)
                if event:
                    self._queue(self._grid_text(event, reading, device), now)
                event = self._grid_events("exec", cfg, now)
                if event:
                    launches.append(event[0])
            for kinds, text in self._battery_events(reading, device, cfg, now):
                self._queue(text, now)
                launches.extend(kinds)
        # Outside the lock on purpose: ShellExecute can block on a shortcut that
        # points at a dead network share, on UAC or on the "how do you want to
        # open this file?" dialog. Holding eval_lock there would stall observe()
        # on the paho thread and stop the MQTT network loop.
        for kind in launches:
            self._launch(kind, reading, cfg, now)

    def _track_raw(self, present, now):
        """Record the instantaneous reading; True if it just changed."""
        if present is None or present == self.raw:
            return False
        first = self.raw is None
        self.raw, self.raw_since = present, now
        if first:                                  # first reading: adopt silently
            self.grid = {ch: present for ch in CHANNELS}
            self.grid_since = {ch: now for ch in CHANNELS}
        return True

    def _grid_events(self, channel, cfg, now):
        """(kind, began, held) once the raw state has outlasted this channel's
        delay, else None.

        Pure state - the caller formats the text. Flicker cancellation comes for
        free: raw_since restarts on every flip, so leaving and re-entering the
        confirmed state restarts the countdown.
        """
        if self.raw is None or self.raw == self.grid[channel]:
            return None
        prefix = "" if channel == "alert" else "exec_"
        key = f"{prefix}{'restore' if self.raw else 'outage'}_delay_min"
        if now - self.raw_since < 60 * float(cfg.get(key, DELAY_DEFAULTS[key])):
            return None
        began = self.raw_since                     # the event, not its confirmation
        held = began - self.grid_since[channel]
        self.grid[channel], self.grid_since[channel] = self.raw, began
        return ("restore" if self.raw else "outage"), began, held

    @staticmethod
    def _grid_text(event, reading, device):
        kind, began, held = event
        if kind == "restore":
            return (f"🟢 <b>Power restored</b>\n{html.escape(device)} is back on grid power.\n"
                    f"{_alert_body(reading)}\nOutage lasted {_fmt_duration(held)} · {_fmt_clock(began)}")
        return (f"🔴 <b>Power outage</b>\n{html.escape(device)} is running on battery.\n"
                f"{_alert_body(reading)}\n{_fmt_clock(began)}")

    def _battery_events(self, reading, device, cfg, now):
        """[(kinds, text)] - one message, but every crossed slot to launch.

        Battery levels are instantaneous crossings shared by both channels, so
        they have no delay of their own. The kind travels with the level because
        nothing stops the user from setting alert 1 lower than alert 2.
        """
        soc, crossed = reading["soc"], []
        for key, kind in BATT_KIND.items():
            try:
                level = int(cfg.get(key, 0) or 0)
            except (TypeError, ValueError):
                continue
            if not 1 <= level <= 100:
                continue
            if self.batt_level.get(key) != level:  # new or changed threshold
                self.batt_level[key] = level
                self.batt_armed[key] = soc > level
                continue
            if self.batt_armed.get(key) and soc <= level:
                self.batt_armed[key] = False
                crossed.append((level, kind))
            elif not self.batt_armed.get(key) and soc >= level + self.REARM_MARGIN:
                self.batt_armed[key] = True
        if not crossed:
            return []
        level, _ = min(crossed)  # a steep drop past both thresholds is still one alert
        icon = "🪫" if level <= 20 else "⚠️"
        text = (f"{icon} <b>Battery at {soc}%</b>\n{html.escape(device)} dropped below {level}%.\n"
                f"{_alert_body(reading)}\n{_fmt_clock(now)}")
        return [([kind for _, kind in crossed], text)]

    # -- launching --------------------------------------------------------- #
    def _launch(self, kind, reading, cfg, now):
        if not cfg.get("exec_enabled"):
            return
        path = str(cfg.get(f"exec_{kind}_path", "") or "").strip()
        if not path:                               # empty path = slot off
            return
        device = cfg.get("device_name") or "EcoFlow"
        args = render_args(cfg.get(f"exec_{kind}_args", ""),
                           exec_values(kind, reading, device, now))

        def work():
            try:
                _co_initialize()
                run_command(path, args)
            except Exception as err:
                self.on_status(f"Run: {kind} failed ({err})")

        threading.Thread(target=work, name=f"exec-{kind}", daemon=True).start()

    # -- delivery ---------------------------------------------------------- #
    def _queue(self, text, now):
        cfg = self.get_cfg()
        if not (cfg.get("telegram_enabled") and cfg.get("telegram_token")
                and cfg.get("telegram_chat_id")):
            return
        with self.lock:
            self.outbox.append({"text": text, "created": now, "tries": 0, "next_try": now})

    def _loop(self):
        while not self.stop_event.wait(self.TICK_SECONDS):
            try:
                self.evaluate()   # let a pending transition age out without new data
                self._flush()
            except Exception:
                pass

    def _flush(self):
        cfg = self.get_cfg()
        token, chat = cfg.get("telegram_token"), cfg.get("telegram_chat_id")
        if not (cfg.get("telegram_enabled") and token and chat):
            with self.lock:   # switched off mid-flight: don't deliver later
                self.outbox.clear()
            return
        now = time.time()
        with self.lock:
            due = [m for m in self.outbox if m["next_try"] <= now]
        for msg in due:
            if now - msg["created"] > self.MAX_AGE_SECONDS:
                self._drop(msg)
                self.on_status("Telegram: alert expired undelivered")
                continue
            try:
                telegram_send(token, chat, msg["text"])
                self._drop(msg)
            except Exception as err:
                msg["tries"] += 1
                delay = self.RETRY_DELAYS[min(msg["tries"] - 1, len(self.RETRY_DELAYS) - 1)]
                msg["next_try"] = now + delay
                self.on_status(f"Telegram: send failed ({err}), retrying")

    def _drop(self, msg):
        with self.lock:
            if msg in self.outbox:
                self.outbox.remove(msg)


# --------------------------------------------------------------------------- #
# Icon rendering
# --------------------------------------------------------------------------- #
_FONT_CANDIDATES = [
    "C:/Windows/Fonts/segoeuib.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/arial.ttf",
]


def _font(size):
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _color_for(soc, charging):
    if charging:
        return (41, 182, 246, 255)
    if soc >= 60:
        return (76, 175, 80, 255)
    if soc >= 30:
        return (255, 179, 0, 255)
    if soc >= 15:
        return (255, 109, 0, 255)
    return (244, 67, 54, 255)


def render_icon(soc, charging):
    text = "?" if soc is None else str(soc)
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font = _font({1: 52, 2: 46, 3: 34}.get(len(text), 30))
    color = (150, 150, 150, 255) if soc is None else _color_for(soc, charging)
    draw.text(
        (size // 2, size // 2 + 2), text, font=font, fill=color,
        anchor="mm", stroke_width=4, stroke_fill=(0, 0, 0, 255),
    )
    return img


def _fmt_remain(minutes):
    if minutes is None:
        return "time remaining: n/a"
    h, m = divmod(minutes, 60)
    return f"{h}h {m}m remaining" if h else f"{m}m remaining"


def tooltip_for(reading):
    return (
        f"{APP_TITLE}: {reading['soc']}%\n"
        f"{reading['state']} - In {reading['watts_in']}W / Out {reading['watts_out']}W\n"
        f"{_fmt_remain(reading['remain_min'])}"
    )


# --------------------------------------------------------------------------- #
# Details panel (left click): a Windows 11 style flyout drawn with Pillow
# --------------------------------------------------------------------------- #
PANEL_THEMES = {  # Windows 11 flyout surfaces; picked from the taskbar theme
    "light": {"surface": "#f9f9f9", "band": "#eeeeee", "line": "#e3e3e3", "text": "#1b1b1b",
              "text2": "#5c5c5c", "text3": "#6b6b6b", "on_fill": "#1b1b1b"},
    "dark": {"surface": "#2b2b2b", "band": "#202020", "line": "#3a3a3a", "text": "#ffffff",
             "text2": "#d1d1d1", "text3": "#a0a0a0", "on_fill": "#1b1b1b"},
}
_GLYPH_IN, _GLYPH_OUT, _GLYPH_BOLT = "\ue896", "\ue898", "\ue945"  # Segoe Fluent/MDL2


@functools.lru_cache(maxsize=None)
def _ui_font(size, style="Regular Text"):
    """Segoe UI Variable at a named instance, falling back to classic Segoe UI."""
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/SegUIVar.ttf", size)
        font.set_variation_by_name(style)
        return font
    except (OSError, ValueError):
        name = "seguisb.ttf" if style.startswith("Semibold") else "segoeui.ttf"
        try:
            return ImageFont.truetype(f"C:/Windows/Fonts/{name}", size)
        except OSError:
            return _font(size)


@functools.lru_cache(maxsize=None)
def _icon_font(size):
    for name in ("SegoeIcons.ttf", "segmdl2.ttf"):  # Windows 11, then 10
        try:
            return ImageFont.truetype(f"C:/Windows/Fonts/{name}", size)
        except OSError:
            continue
    return None


def system_theme():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            light = winreg.QueryValueEx(key, "SystemUsesLightTheme")[0]
    except OSError:
        light = 0
    return "light" if light else "dark"


def _clip(draw, text, font, width):
    """Shorten text with an ellipsis until it fits in `width` pixels."""
    if draw.textlength(text, font=font) <= width:
        return text
    while text and draw.textlength(text + "…", font=font) > width:
        text = text[:-1]
    return text.rstrip() + "…"


def _wrap(draw, text, font, width, max_lines=3):
    """Break at spaces, or mid-word when one word is wider than the line
    (error text is full of URLs and reprs)."""
    lines, text = [], " ".join(text.split())
    while text and len(lines) < max_lines:
        n = len(text)
        while n > 1 and draw.textlength(text[:n], font=font) > width:
            n -= 1
        cut = n if n == len(text) else text.rfind(" ", 0, n + 1)
        cut = cut if cut > 0 else n
        lines.append(text[:cut].strip())
        text = text[cut:].lstrip()
    if text:
        lines[-1] = _clip(draw, f"{lines[-1]} {text}", font, width)
    return lines


def _fmt_span(minutes):
    h, m = divmod(minutes, 60)
    return f"{h} h {m:02d} min" if h else f"{m} min"


def render_details(reading, source, device, note="", theme="light"):
    """Draw the left-click panel. `reading` is None while there is no data,
    and `note` then says why (an error, or that the first reading is due)."""
    c = PANEL_THEMES[theme]
    W, P, S = 320, 20, 4  # width, gutter, supersampling for the shapes
    bx0, by0, bx1, by1 = P, P, W - P - 7, P + 72      # battery body
    ix0, iy0, ix1, iy1 = bx0 + 5, by0 + 5, bx1 - 5, by1 - 5  # fill well
    probe = ImageDraw.Draw(Image.new("L", (1, 1)))
    msg_font = _ui_font(14)
    msg_lines = [] if reading else _wrap(probe, note, msg_font, W - 2 * P)
    body_bottom = 196 if reading else by1 + 26 + 20 * len(msg_lines)
    H = body_bottom + 20 + 40  # gap, footer band

    img = Image.new("RGB", (W, H), c["surface"])
    d = ImageDraw.Draw(img)

    # Battery shell and level fill, supersampled so the curves stay smooth.
    shell = Image.new("L", (W * S, H * S), 0)
    ds = ImageDraw.Draw(shell)
    ds.rounded_rectangle([bx0 * S, by0 * S, bx1 * S, by1 * S], radius=12 * S, outline=255, width=2 * S)
    mid = (by0 + by1) // 2
    ds.rounded_rectangle([(bx1 + 2) * S, (mid - 10) * S, (bx1 + 6) * S, (mid + 10) * S], radius=2 * S, fill=255)
    fill = Image.new("L", (W * S, H * S), 0)
    soc = reading["soc"] if reading else 0
    if soc > 0:
        ImageDraw.Draw(fill).rounded_rectangle([ix0 * S, iy0 * S, ix1 * S, iy1 * S], radius=8 * S, fill=255)
        cut = ix0 + (ix1 - ix0) * min(soc, 100) / 100
        ImageDraw.Draw(fill).rectangle([cut * S, 0, W * S, H * S], fill=0)
    shell, fill = (m.resize((W, H), Image.LANCZOS) for m in (shell, fill))
    img.paste(c["text2"], mask=shell)
    if reading:
        img.paste(_color_for(soc, reading["charging"])[:3], mask=fill)

    # The percentage sits inside the battery and flips colour where the
    # fill runs under it, so it reads on both the fill and the empty well.
    ink = Image.new("L", (W, H), 0)
    di = ImageDraw.Draw(ink)
    base = mid + 14
    big = _ui_font(40, "Semibold Display")
    num = str(soc) if reading else "—"
    di.text((ix0 + 14, base), num, font=big, fill=255, anchor="ls")
    if reading:
        di.text((ix0 + 16 + di.textlength(num, font=big), base), "%",
                font=_ui_font(22, "Semibold Display"), fill=255, anchor="ls")
        if reading["charging"] and _icon_font(22):
            di.text((ix1 - 14, mid), _GLYPH_BOLT, font=_icon_font(22), fill=255, anchor="rm")
    img.paste(c["text"] if reading else c["text3"], mask=ink)
    img.paste(c["on_fill"], mask=ImageChops.multiply(ink, fill))

    if reading:
        # State and time, then the two power flows.
        y = by1 + 30
        state_font = _ui_font(14, "Semibold Text")
        d.text((P, y), reading["state"], font=state_font, fill=c["text"], anchor="ls")
        if reading["remain_min"] is not None:
            span = _fmt_span(reading["remain_min"])
            tail = f"  ·  {span} to full" if reading["charging"] else f"  ·  {span} left"
            d.text((P + d.textlength(reading["state"], font=state_font), y), tail,
                   font=msg_font, fill=c["text2"], anchor="ls")
        for col, (glyph, label, watts) in enumerate(
                ((_GLYPH_IN, "Input", reading["watts_in"]), (_GLYPH_OUT, "Output", reading["watts_out"]))):
            x = P + col * (W - 2 * P) // 2
            if _icon_font(14):
                d.text((x, 158), glyph, font=_icon_font(14), fill=c["text2"], anchor="lm")
            d.text((x + 22, 158), label, font=_ui_font(12), fill=c["text2"], anchor="lm")
            d.text((x, 190), f"{watts} W", font=_ui_font(22, "Semibold Display"),
                   fill=c["text"] if watts else c["text3"], anchor="ls")
    else:
        for i, line in enumerate(msg_lines):
            d.text((P, by1 + 30 + 20 * i), line, font=msg_font, fill=c["text2"], anchor="ls")

    # Footer band: where the numbers come from, and which unit this is.
    top = H - 40
    d.rectangle([0, top, W, H], fill=c["band"])
    d.line([0, top, W, top], fill=c["line"])
    source = source or "Connecting…"
    dot = (_color_for(100, False) if source.startswith("Live")
           else (255, 179, 0) if source.startswith("Connect") else (244, 67, 54))
    d.ellipse([P, top + 16, P + 8, top + 24], fill=dot[:3])
    small = _ui_font(12)
    d.text((P + 16, top + 20), _clip(d, source, small, 150), font=small, fill=c["text2"], anchor="lm")
    if device:
        d.text((W - P, top + 20), _clip(d, device, small, 110), font=small, fill=c["text3"], anchor="rm")
    return img


def _photo(img):
    """PIL image -> Tk PhotoImage via PNG, so Pillow's ImageTk isn't needed."""
    import io
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return tk.PhotoImage(data=base64.b64encode(buf.getvalue()))


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


def work_area(x, y):
    """Desktop minus taskbar on the monitor under (x, y)."""
    user32 = ctypes.windll.user32
    user32.MonitorFromPoint.restype = wintypes.HMONITOR
    user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
    info = _MONITORINFO(cbSize=ctypes.sizeof(_MONITORINFO))
    mon = user32.MonitorFromPoint(wintypes.POINT(x, y), 2)  # MONITOR_DEFAULTTONEAREST
    user32.GetMonitorInfoW(mon, ctypes.byref(info))
    r = info.rcWork
    return r.left, r.top, r.right, r.bottom


def animations_enabled():
    flag = wintypes.BOOL(True)
    ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(flag), 0)  # SPI_GETCLIENTAREAANIMATION
    return bool(flag.value)


def make_app_icon():
    """Return a battery-style app icon (used for the .ico and window icon)."""
    s = 256
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([8, 8, s - 8, s - 8], radius=48, fill=(38, 50, 56, 255))
    bx0, by0, bx1, by1 = 52, 92, 186, 164
    d.rounded_rectangle([bx0, by0, bx1, by1], radius=12, outline=(255, 255, 255, 255), width=10)
    d.rounded_rectangle([bx1 + 6, 112, bx1 + 24, 144], radius=6, fill=(255, 255, 255, 255))
    fill_w = int((bx1 - bx0 - 28) * 0.62)
    d.rounded_rectangle([bx0 + 14, by0 + 14, bx0 + 14 + fill_w, by1 - 14], radius=6, fill=(76, 175, 80, 255))
    return img


# --------------------------------------------------------------------------- #
# Settings dialog (tkinter)
# --------------------------------------------------------------------------- #
class SettingsDialog:
    def __init__(self, app):
        self.app = app
        cfg = app.cfg
        self.devices = []  # list of (label, sn)
        self.soc_choices = []  # list of (label, field)
        self.grid_values = {}  # {field: last seen value}

        win = tk.Toplevel(app.root)
        self.win = win
        app.settings_win = win
        win.title(f"{APP_TITLE} - Settings")
        win.resizable(False, False)
        win.protocol("WM_DELETE_WINDOW", self.close)
        try:
            win.iconphoto(True, app.tk_icon)
        except Exception:
            pass

        nb = self.nb = ttk.Notebook(win)
        nb.grid(row=0, column=0, sticky="nsew", padx=8, pady=(8, 0))
        frm = ttk.Frame(nb, padding=16)
        frm.columnconfigure(1, weight=1)
        nb.add(frm, text="Device")
        row = 0

        ttk.Label(frm, text="Access Key").grid(row=row, column=0, sticky="w", pady=4)
        self.e_access = ttk.Entry(frm, width=46)
        self.e_access.insert(0, cfg.get("access_key", ""))
        self.e_access.grid(row=row, column=1, columnspan=2, sticky="ew", pady=4)
        row += 1

        ttk.Label(frm, text="Secret Key").grid(row=row, column=0, sticky="w", pady=4)
        self.e_secret = ttk.Entry(frm, width=46, show="\u2022")
        self.e_secret.insert(0, cfg.get("secret_key", ""))
        self.e_secret.grid(row=row, column=1, sticky="ew", pady=4)
        self.show_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(frm, text="Show", variable=self.show_var, command=self._toggle_secret)\
            .grid(row=row, column=2, sticky="w", padx=(8, 0))
        row += 1

        ttk.Label(frm, text="Region").grid(row=row, column=0, sticky="w", pady=4)
        self.cb_region = ttk.Combobox(frm, values=list(HOSTS), state="readonly")
        current_region = next((k for k, v in HOSTS.items() if v == cfg.get("host")), list(HOSTS)[0])
        self.cb_region.set(current_region)
        self.cb_region.grid(row=row, column=1, columnspan=2, sticky="ew", pady=4)
        row += 1

        self.btn_test = ttk.Button(frm, text="Test & load devices", command=self._on_test)
        self.btn_test.grid(row=row, column=0, sticky="w", pady=(10, 4))
        self.lbl_status = ttk.Label(frm, text="", foreground="#666")
        self.lbl_status.grid(row=row, column=1, columnspan=2, sticky="w", pady=(10, 4))
        row += 1

        ttk.Label(frm, text="Device").grid(row=row, column=0, sticky="w", pady=4)
        self.cb_device = ttk.Combobox(frm, values=[], state="readonly")
        if cfg.get("sn"):
            label = cfg.get("device_name") or cfg["sn"]
            self.devices = [(f"{label} ({cfg['sn']})", cfg["sn"])]
            self.cb_device["values"] = [d[0] for d in self.devices]
            self.cb_device.current(0)
        self.cb_device.grid(row=row, column=1, columnspan=2, sticky="ew", pady=4)
        self.cb_device.bind("<<ComboboxSelected>>", lambda e: self._load_soc_fields())
        row += 1

        ttk.Label(frm, text="Refresh every (seconds)").grid(row=row, column=0, sticky="w", pady=4)
        self.sp_refresh = ttk.Spinbox(frm, from_=10, to=3600, increment=5, width=8)
        self.sp_refresh.set(int(cfg.get("refresh_seconds", DEFAULT_REFRESH)))
        self.sp_refresh.grid(row=row, column=1, sticky="w", pady=4)
        row += 1

        self.autostart_var = tk.BooleanVar(value=autostart_enabled())
        ttk.Checkbutton(frm, text="Start automatically with Windows",
                        variable=self.autostart_var)\
            .grid(row=row, column=0, columnspan=3, sticky="w", pady=4)
        row += 1

        ttk.Separator(frm, orient="horizontal").grid(row=row, column=0, columnspan=3, sticky="ew", pady=10)
        row += 1
        ttk.Label(frm, text="Battery reading").grid(row=row, column=0, sticky="w", pady=4)
        self.cb_soc = ttk.Combobox(frm, values=[], state="readonly")
        current_field = cfg.get("soc_field", DEFAULT_SOC_FIELD)
        self.cb_soc["values"] = [f"{current_field} (current)", CUSTOM_SOC_LABEL]
        self.cb_soc.current(0)
        self.cb_soc.grid(row=row, column=1, columnspan=2, sticky="ew", pady=4)
        self.cb_soc.bind("<<ComboboxSelected>>", lambda e: self._on_soc_selected())
        row += 1
        self.custom_row = row
        self.lbl_custom = ttk.Label(frm, text="Custom field")
        self.e_custom = ttk.Entry(frm, width=32)
        self.e_custom.insert(0, current_field)
        # shown only when "Custom field…" is selected
        row += 1
        ttk.Label(frm, text="Pick your device, then choose the value that matches the EcoFlow app.",
                  foreground="#888").grid(row=row, column=0, columnspan=3, sticky="w")
        row += 1

        self._build_notifications_tab(nb)
        self._build_executions_tab(nb)

        btns = ttk.Frame(win, padding=(16, 10))
        btns.grid(row=1, column=0, sticky="e")
        ttk.Button(btns, text="Cancel", command=self.close).grid(row=0, column=0, padx=6)
        ttk.Button(btns, text="Save", command=self._on_save).grid(row=0, column=1)

        win.update_idletasks()
        win.lift()
        win.focus_force()

        # If already configured, detect live fields on open so the dropdown
        # shows real values without needing to click "Test" again.
        if is_configured(cfg):
            win.after(150, self._load_soc_fields)

    # -- notifications tab ------------------------------------------------- #
    def _build_notifications_tab(self, nb):
        cfg = self.app.cfg
        frm = ttk.Frame(nb, padding=16)
        frm.columnconfigure(1, weight=1)
        nb.add(frm, text="Notifications")
        row = 0

        self.tg_enabled = tk.BooleanVar(value=bool(cfg.get("telegram_enabled")))
        ttk.Checkbutton(frm, text="Send Telegram alerts", variable=self.tg_enabled)\
            .grid(row=row, column=0, columnspan=3, sticky="w", pady=(0, 6))
        row += 1

        ttk.Label(frm, text="Bot token").grid(row=row, column=0, sticky="w", pady=4)
        self.e_token = ttk.Entry(frm, width=38, show="\u2022")
        self.e_token.insert(0, cfg.get("telegram_token", ""))
        self.e_token.grid(row=row, column=1, sticky="ew", pady=4)
        self.tg_show = tk.BooleanVar(value=False)
        ttk.Checkbutton(frm, text="Show", variable=self.tg_show, command=self._toggle_token)\
            .grid(row=row, column=2, sticky="w", padx=(8, 0))
        row += 1

        ttk.Label(frm, text="Chat ID").grid(row=row, column=0, sticky="w", pady=4)
        self.e_chat = ttk.Entry(frm, width=38)
        self.e_chat.insert(0, cfg.get("telegram_chat_id", ""))
        self.e_chat.grid(row=row, column=1, sticky="ew", pady=4)
        self.btn_detect = ttk.Button(frm, text="Detect", width=9, command=self._on_detect_chat)
        self.btn_detect.grid(row=row, column=2, sticky="w", padx=(8, 0))
        row += 1

        self.btn_test_tg = ttk.Button(frm, text="Send test message", command=self._on_test_telegram)
        self.btn_test_tg.grid(row=row, column=0, sticky="w", pady=(6, 2))
        self.lbl_tg = ttk.Label(frm, text="", foreground="#666", wraplength=300, justify="left")
        self.lbl_tg.grid(row=row, column=1, columnspan=2, sticky="w", pady=(6, 2))
        row += 1

        ttk.Label(frm, justify="left", foreground="#888",
                  text="1. Message @BotFather on Telegram \u2192 /newbot \u2192 copy the token.\n"
                       "2. Open your new bot and send it /start.\n"
                       "3. Paste the token above, then click Detect.")\
            .grid(row=row, column=0, columnspan=3, sticky="w", pady=(2, 0))
        row += 1

        ttk.Separator(frm, orient="horizontal").grid(row=row, column=0, columnspan=3, sticky="ew", pady=12)
        row += 1

        ttk.Label(frm, text="Grid input field").grid(row=row, column=0, sticky="w", pady=4)
        self.cb_grid = ttk.Combobox(frm, values=[], width=30)  # editable: any field name
        self.cb_grid.set(cfg.get("grid_field", DEFAULT_GRID_FIELD))
        self.cb_grid.grid(row=row, column=1, columnspan=2, sticky="ew", pady=4)
        self.cb_grid.bind("<<ComboboxSelected>>", lambda e: self._update_grid_preview())
        self.cb_grid.bind("<KeyRelease>", lambda e: self._update_grid_preview())
        row += 1

        ttk.Label(frm, text="Power is on above").grid(row=row, column=0, sticky="w", pady=4)
        self.sp_thresh = ttk.Spinbox(frm, from_=0, to=1000000, increment=1, width=10,
                                     command=self._update_grid_preview)
        self.sp_thresh.set(self._tidy(cfg.get("grid_threshold", DEFAULT_GRID_THRESHOLD)))
        self.sp_thresh.grid(row=row, column=1, sticky="w", pady=4)
        self.sp_thresh.bind("<KeyRelease>", lambda e: self._update_grid_preview())
        row += 1

        self.lbl_grid = ttk.Label(frm, text="", foreground="#888", wraplength=380, justify="left")
        self.lbl_grid.grid(row=row, column=0, columnspan=3, sticky="w", pady=(0, 4))
        row += 1

        ttk.Label(frm, text="Alert after (minutes) without power").grid(row=row, column=0, sticky="w", pady=4)
        self.sp_outage = ttk.Spinbox(frm, from_=0, to=180, increment=1, width=10)
        self.sp_outage.set(self._tidy(cfg.get("outage_delay_min", DEFAULT_OUTAGE_DELAY_MIN)))
        self.sp_outage.grid(row=row, column=1, sticky="w", pady=4)
        row += 1

        ttk.Label(frm, text="Alert after (minutes) with power back").grid(row=row, column=0, sticky="w", pady=4)
        self.sp_restore = ttk.Spinbox(frm, from_=0, to=180, increment=1, width=10)
        self.sp_restore.set(self._tidy(cfg.get("restore_delay_min", DEFAULT_RESTORE_DELAY_MIN)))
        self.sp_restore.grid(row=row, column=1, sticky="w", pady=4)
        row += 1

        ttk.Separator(frm, orient="horizontal").grid(row=row, column=0, columnspan=3, sticky="ew", pady=12)
        row += 1

        ttk.Label(frm, text="Battery alert 1 (%)").grid(row=row, column=0, sticky="w", pady=4)
        self.sp_batt1 = ttk.Spinbox(frm, from_=0, to=100, increment=1, width=10)
        self.sp_batt1.set(cfg.get("batt_alert_1", DEFAULT_BATT_ALERT_1))
        self.sp_batt1.grid(row=row, column=1, sticky="w", pady=4)
        row += 1

        ttk.Label(frm, text="Battery alert 2 (%)").grid(row=row, column=0, sticky="w", pady=4)
        self.sp_batt2 = ttk.Spinbox(frm, from_=0, to=100, increment=1, width=10)
        self.sp_batt2.set(cfg.get("batt_alert_2", DEFAULT_BATT_ALERT_2))
        self.sp_batt2.grid(row=row, column=1, sticky="w", pady=4)
        row += 1

        ttk.Label(frm, text="Alerts fire when the battery drops past a level. 0 turns one off.",
                  foreground="#888").grid(row=row, column=0, columnspan=3, sticky="w")

    # -- executions tab ---------------------------------------------------- #
    def _build_executions_tab(self, nb):
        cfg = self.app.cfg
        frm = ttk.Frame(nb, padding=16)
        frm.columnconfigure(0, weight=1)
        nb.add(frm, text="Executions")
        self.exec_w = {}    # {kind: {"path": Entry, "args": Entry, "run": Button}}
        row = 0

        self.exec_enabled = tk.BooleanVar(value=bool(cfg.get("exec_enabled")))
        ttk.Checkbutton(frm, text="Run programs on these events", variable=self.exec_enabled)\
            .grid(row=row, column=0, sticky="w", pady=(0, 6))
        row += 1

        # Both delays share a row: the Notebook takes the height of its tallest
        # tab, and the window is not resizable.
        delays = ttk.Frame(frm)
        delays.grid(row=row, column=0, sticky="w", pady=(0, 6))
        ttk.Label(delays, text="Run after (minutes)").grid(row=0, column=0, sticky="w")
        self.sp_exec_outage = ttk.Spinbox(delays, from_=0, to=180, increment=1, width=5)
        self.sp_exec_outage.set(
            self._tidy(cfg.get("exec_outage_delay_min", DEFAULT_EXEC_OUTAGE_DELAY_MIN)))
        self.sp_exec_outage.grid(row=0, column=1, padx=(8, 4))
        ttk.Label(delays, text="without power").grid(row=0, column=2, sticky="w")
        self.sp_exec_restore = ttk.Spinbox(delays, from_=0, to=180, increment=1, width=5)
        self.sp_exec_restore.set(
            self._tidy(cfg.get("exec_restore_delay_min", DEFAULT_EXEC_RESTORE_DELAY_MIN)))
        self.sp_exec_restore.grid(row=0, column=3, padx=(14, 4))
        ttk.Label(delays, text="with power back").grid(row=0, column=4, sticky="w")
        row += 1

        titles = {
            "outage": "Power outage",
            "restore": "Power restored",
            "batt1": f"Battery alert 1 ({cfg.get('batt_alert_1', DEFAULT_BATT_ALERT_1)}%)",
            "batt2": f"Battery alert 2 ({cfg.get('batt_alert_2', DEFAULT_BATT_ALERT_2)}%)",
        }
        for kind in EXEC_KINDS:
            self._exec_slot(frm, kind, titles[kind], row)
            row += 1

        # One shared status line, again to keep the tab short.
        self.lbl_exec = ttk.Label(frm, text="", foreground="#666", wraplength=440, justify="left")
        self.lbl_exec.grid(row=row, column=0, sticky="w", pady=(4, 2))
        row += 1

        ttk.Label(frm, justify="left", foreground="#888", wraplength=440,
                  text="An empty program turns that event off. Arguments accept {event}, "
                       "{soc}, {device}, {watts_in}, {watts_out} and {time}; add your own "
                       "quotes if a value may contain spaces.\n"
                       "0 minutes means “as soon as it is noticed” (within 10 s), so "
                       "a flickering grid runs the outage and the restore program back to "
                       "back — set a minute if that matters. If both battery levels are "
                       "equal, both slots run at once.\n"
                       "Saving Settings during an outage clears the pending event.")\
            .grid(row=row, column=0, sticky="w")

        self._check_exec_paths()

    def _exec_slot(self, parent, kind, title, row):
        cfg = self.app.cfg
        box = ttk.LabelFrame(parent, text=title, padding=(8, 2, 8, 6))
        box.grid(row=row, column=0, sticky="ew", pady=3)
        box.columnconfigure(1, weight=1)

        ttk.Label(box, text="Program", width=10).grid(row=0, column=0, sticky="w", pady=2)
        e_path = ttk.Entry(box, width=42)
        e_path.insert(0, cfg.get(f"exec_{kind}_path", ""))
        e_path.grid(row=0, column=1, sticky="ew", pady=2)
        e_path.bind("<FocusOut>", lambda e: self._check_exec_paths())
        ttk.Button(box, text="Browse…", width=9, command=lambda: self._browse_exec(kind))\
            .grid(row=0, column=2, sticky="w", padx=(8, 0))

        ttk.Label(box, text="Arguments", width=10).grid(row=1, column=0, sticky="w", pady=2)
        e_args = ttk.Entry(box, width=42)
        e_args.insert(0, cfg.get(f"exec_{kind}_args", ""))
        e_args.grid(row=1, column=1, sticky="ew", pady=2)
        btn = ttk.Button(box, text="Run now", width=9, command=lambda: self._on_run_now(kind))
        btn.grid(row=1, column=2, sticky="w", padx=(8, 0))

        self.exec_w[kind] = {"path": e_path, "args": e_args, "run": btn}

    def _exec_paths(self):
        return {kind: w["path"].get().strip() for kind, w in self.exec_w.items()}

    def _check_exec_paths(self):
        """Warn about programs that aren't there - never block on it, the drive
        may simply be unplugged right now."""
        missing = [k for k, p in self._exec_paths().items()
                   if p and not os.path.exists(resolve_command_path(p))]
        if missing:
            self.lbl_exec.config(text=f"Not found right now: {', '.join(missing)}. "
                                      "Saved anyway - check the path if that's a surprise.",
                                 foreground="#c60")
        elif self.lbl_exec.cget("foreground") == "#c60":
            self.lbl_exec.config(text="", foreground="#666")

    def _browse_exec(self, kind):
        path = filedialog.askopenfilename(
            parent=self.win, title="Pick a program or shortcut",
            filetypes=[("Programs and shortcuts", "*.lnk *.exe *.bat *.cmd *.vbs *.ps1"),
                       ("All files", "*.*")])
        if path:
            entry = self.exec_w[kind]["path"]
            entry.delete(0, tk.END)
            entry.insert(0, os.path.normpath(path))
            self._check_exec_paths()

    def _on_run_now(self, kind):
        """Launch what's typed in the widgets right now - not what's saved, so
        the user can try a path before committing to it."""
        path = self.exec_w[kind]["path"].get().strip()
        if not path:
            self.lbl_exec.config(text="Pick a program for this event first.", foreground="#c00")
            return
        snapshot = self.app.alerter.latest
        device = self.app.cfg.get("device_name") or "EcoFlow"
        args = render_args(self.exec_w[kind]["args"].get(),
                           exec_values(kind, snapshot[0] if snapshot else None,
                                       device, time.time()))
        btn = self.exec_w[kind]["run"]
        btn.config(state="disabled")
        self.lbl_exec.config(text="Launching…", foreground="#666")

        def work():
            try:
                _co_initialize()
                run_command(path, args)
                self.app.post(lambda: self._run_done(kind, None))
            except Exception as err:
                self.app.post(lambda err=err: self._run_done(kind, err))

        threading.Thread(target=work, name=f"exec-{kind}", daemon=True).start()

    def _run_done(self, kind, err):
        self.exec_w[kind]["run"].config(state="normal")
        if err:
            self.lbl_exec.config(text=f"Failed: {err}", foreground="#c00")
        else:
            self.lbl_exec.config(text=f"Launched the {kind} program — check it did what "
                                      "you expect.", foreground="#2a7")

    def _toggle_token(self):
        self.e_token.config(show="" if self.tg_show.get() else "\u2022")

    def _update_grid_preview(self):
        field = self.cb_grid.get().strip()
        if field not in self.grid_values:
            self.lbl_grid.config(
                text="Pick your device on the Device tab to read live AC-input values.")
            return
        value = self.grid_values[field]
        try:
            threshold = float(self.sp_thresh.get())
        except ValueError:
            threshold = DEFAULT_GRID_THRESHOLD
        on = float(value) > threshold
        self.lbl_grid.config(
            text=f"Now: {value:g} \u2192 power is {'ON' if on else 'OFF'} "
                 f"(with grid connected this should read ON).")

    def _grid_loaded(self, cands):
        self.grid_values = dict(cands)
        self.cb_grid["values"] = [k for k, _ in cands]
        # Only override the field when this device doesn't report the configured
        # one - then the saved threshold belongs to a field that isn't in use.
        if cands and self.cb_grid.get().strip() not in self.grid_values:
            best, value = cands[0]
            self.cb_grid.set(best)
            self.sp_thresh.set(self._tidy(default_grid_threshold(best, value)))
        self._update_grid_preview()

    def _telegram_draft(self):
        return self.e_token.get().strip(), self.e_chat.get().strip()

    def _on_detect_chat(self):
        token, _ = self._telegram_draft()
        if not token:
            self.lbl_tg.config(text="Paste your bot token first.", foreground="#c00")
            return
        self.btn_detect.config(state="disabled")
        self.lbl_tg.config(text="Looking for your chat...", foreground="#666")

        def work():
            try:
                bot = telegram_check_token(token)
                chat_id, name = telegram_detect_chat(token)
                self.app.post(lambda: self._chat_detected(bot, chat_id, name))
            except Exception as err:
                self.app.post(lambda err=err: self._telegram_failed(err))

        threading.Thread(target=work, daemon=True).start()

    def _chat_detected(self, bot, chat_id, name):
        self.btn_detect.config(state="normal")
        if not chat_id:
            self.lbl_tg.config(
                text=f"@{bot} is valid, but nobody has messaged it yet. "
                     "Open the bot in Telegram, send /start, then click Detect again.",
                foreground="#c60")
            return
        self.e_chat.delete(0, tk.END)
        self.e_chat.insert(0, chat_id)
        self.lbl_tg.config(text=f"Found {name} via @{bot}.", foreground="#2a7")

    def _telegram_failed(self, err):
        self.btn_detect.config(state="normal")
        self.btn_test_tg.config(state="normal")
        self.lbl_tg.config(text=f"Failed: {err}", foreground="#c00")

    def _on_test_telegram(self):
        token, chat = self._telegram_draft()
        if not token or not chat:
            self.lbl_tg.config(text="Bot token and chat ID are both required.", foreground="#c00")
            return
        self.btn_test_tg.config(state="disabled")
        self.lbl_tg.config(text="Sending...", foreground="#666")
        device = self.app.cfg.get("device_name") or "EcoFlow"

        def work():
            try:
                telegram_send(token, chat,
                              f"\u2705 <b>{APP_TITLE}</b>\nAlerts for {html.escape(device)} are set up.")
                self.app.post(lambda: self._test_sent())
            except Exception as err:
                self.app.post(lambda err=err: self._telegram_failed(err))

        threading.Thread(target=work, daemon=True).start()

    def _test_sent(self):
        self.btn_test_tg.config(state="normal")
        self.lbl_tg.config(text="Test message sent - check Telegram.", foreground="#2a7")

    # -- helpers ----------------------------------------------------------- #
    def _toggle_secret(self):
        self.e_secret.config(show="" if self.show_var.get() else "\u2022")

    def _draft_cfg(self):
        return {
            "access_key": self.e_access.get().strip(),
            "secret_key": self.e_secret.get().strip(),
            "host": HOSTS[self.cb_region.get()],
        }

    def _set_status(self, text, color="#666"):
        self.lbl_status.config(text=text, foreground=color)

    @staticmethod
    def _tidy(value):
        """1.0 -> '1', 0.5 -> '0.5' - spinboxes shouldn't show a pointless .0"""
        return f"{float(value):g}"

    @staticmethod
    def _num(widget, default, lo, hi):
        """Read a spinbox, falling back to the default if it was typed into."""
        try:
            return min(hi, max(lo, float(widget.get())))
        except (ValueError, TypeError):
            return default

    def _on_test(self):
        draft = self._draft_cfg()
        if not draft["access_key"] or not draft["secret_key"]:
            self._set_status("Enter both keys first.", "#c00")
            return
        self.btn_test.config(state="disabled")
        self._set_status("Connecting...", "#666")

        def work():
            try:
                devices = list_devices(draft)
                self.app.post(lambda: self._devices_loaded(devices))
            except Exception as err:
                # bind err now: Python unbinds it when the except block ends
                self.app.post(lambda err=err: self._devices_failed(err))

        threading.Thread(target=work, daemon=True).start()

    def _devices_loaded(self, devices):
        self.btn_test.config(state="normal")
        if not devices:
            self._set_status("Keys OK, but no devices on this account.", "#c60")
            return
        self.devices = [(f"{d.get('deviceName', d.get('sn'))} ({d.get('sn')})", d.get("sn")) for d in devices]
        self.cb_device["values"] = [d[0] for d in self.devices]
        self.cb_device.current(0)
        self._set_status(f"Connected. Found {len(devices)} device(s).", "#2a7")
        self._load_soc_fields()

    def _devices_failed(self, err):
        self.btn_test.config(state="normal")
        self._set_status(f"Failed: {err}", "#c00")

    def _selected_sn(self):
        label = self.cb_device.get()
        for lbl, sn in self.devices:
            if lbl == label:
                return sn, lbl.rsplit(" (", 1)[0]
        return "", ""

    # -- battery-field detection ------------------------------------------ #
    def _load_soc_fields(self):
        sn, _ = self._selected_sn()
        if not sn:
            return
        draft = self._draft_cfg()
        if not draft["access_key"] or not draft["secret_key"]:
            return
        draft["sn"] = sn
        self._set_status("Reading battery fields...", "#666")

        def work():
            try:
                data = api_get(draft, "/iot-open/sign/device/quota/all", {"sn": sn})
                soc, grid = soc_candidates(data), grid_candidates(data)
                self.app.post(lambda: (self._soc_loaded(soc), self._grid_loaded(grid)))
            except Exception as err:
                self.app.post(lambda err=err: self._set_status(f"Field detect failed: {err}", "#c00"))

        threading.Thread(target=work, daemon=True).start()

    def _soc_loaded(self, cands):
        if not cands:
            self._set_status("Connected, but no battery field detected.", "#c60")
            return
        self.soc_choices = [(f"{k}  ({int(round(v))}%)", k) for k, v in cands]
        self.cb_soc["values"] = [c[0] for c in self.soc_choices] + [CUSTOM_SOC_LABEL]
        target = self.app.cfg.get("soc_field")
        idx = next((i for i, (_, k) in enumerate(self.soc_choices) if k == target), None)
        if idx is None:
            idx = 0  # best "app-shown" guess is first
        self.cb_soc.current(idx)
        self._on_soc_selected()
        self._set_status(f"Detected {len(cands)} battery field(s). Pick the app value.", "#2a7")

    def _on_soc_selected(self):
        if self.cb_soc.get() == CUSTOM_SOC_LABEL:
            self.lbl_custom.grid(row=self.custom_row, column=0, sticky="w", pady=4)
            self.e_custom.grid(row=self.custom_row, column=1, columnspan=2, sticky="ew", pady=4)
        else:
            self.lbl_custom.grid_remove()
            self.e_custom.grid_remove()

    def _selected_soc_field(self):
        choice = self.cb_soc.get()
        if choice == CUSTOM_SOC_LABEL:
            return self.e_custom.get().strip() or DEFAULT_SOC_FIELD
        for lbl, field in self.soc_choices:
            if lbl == choice:
                return field
        # dialog opened without a live fetch: fall back to the "(current)" entry
        return choice.replace(" (current)", "").strip() or DEFAULT_SOC_FIELD

    def _on_save(self):
        access = self.e_access.get().strip()
        secret = self.e_secret.get().strip()
        if not access or not secret:
            messagebox.showwarning(APP_TITLE, "Access Key and Secret Key are required.", parent=self.win)
            return
        sn, name = self._selected_sn()
        if not sn:
            messagebox.showwarning(
                APP_TITLE, "Click 'Test & load devices' and pick your device first.", parent=self.win)
            return
        token, chat = self._telegram_draft()
        if self.tg_enabled.get() and not (token and chat):
            messagebox.showwarning(
                APP_TITLE, "Telegram alerts need a bot token and a chat ID.\n"
                           "Fill them in on the Notifications tab, or untick "
                           "'Send Telegram alerts'.", parent=self.win)
            return
        exec_paths = self._exec_paths()
        if self.exec_enabled.get() and not any(exec_paths.values()):
            messagebox.showwarning(
                APP_TITLE, "Running programs needs at least one program.\n"
                           "Fill one in on the Executions tab, or untick "
                           "'Run programs on these events'.", parent=self.win)
            return
        self.app.cfg = {
            "access_key": access,
            "secret_key": secret,
            "sn": sn,
            "device_name": name,
            "host": HOSTS[self.cb_region.get()],
            "refresh_seconds": int(self._num(self.sp_refresh, DEFAULT_REFRESH, 10, 3600)),
            "soc_field": self._selected_soc_field(),
            "telegram_enabled": bool(self.tg_enabled.get()),
            "telegram_token": token,
            "telegram_chat_id": chat,
            "grid_field": self.cb_grid.get().strip() or DEFAULT_GRID_FIELD,
            "grid_threshold": self._num(self.sp_thresh, DEFAULT_GRID_THRESHOLD, 0, 10 ** 6),
            "outage_delay_min": self._num(self.sp_outage, DEFAULT_OUTAGE_DELAY_MIN, 0, 180),
            "restore_delay_min": self._num(self.sp_restore, DEFAULT_RESTORE_DELAY_MIN, 0, 180),
            "batt_alert_1": int(self._num(self.sp_batt1, DEFAULT_BATT_ALERT_1, 0, 100)),
            "batt_alert_2": int(self._num(self.sp_batt2, DEFAULT_BATT_ALERT_2, 0, 100)),
            "exec_enabled": bool(self.exec_enabled.get()),
            "exec_outage_delay_min": self._num(
                self.sp_exec_outage, DEFAULT_EXEC_OUTAGE_DELAY_MIN, 0, 180),
            "exec_restore_delay_min": self._num(
                self.sp_exec_restore, DEFAULT_EXEC_RESTORE_DELAY_MIN, 0, 180),
        }
        for kind in EXEC_KINDS:
            self.app.cfg[f"exec_{kind}_path"] = exec_paths[kind]
            self.app.cfg[f"exec_{kind}_args"] = self.exec_w[kind]["args"].get().strip()
        save_config(self.app.cfg)
        try:
            set_autostart(self.autostart_var.get())
        except Exception as err:  # non-fatal: config still saved
            messagebox.showwarning(APP_TITLE, f"Could not change autostart:\n{err}", parent=self.win)
        self.app.apply_new_config()  # re-seed via HTTP and (re)connect MQTT
        self.close()

    def close(self):
        self.app.settings_win = None
        try:
            self.win.destroy()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Tray application
# --------------------------------------------------------------------------- #
class App:
    def __init__(self):
        self.cfg = load_config()
        self.stop_event = threading.Event()
        self.refresh_event = threading.Event()
        self.ui_queue = queue.Queue()
        self.state = {"soc": "--", "detail": "Not configured", "source": "", "alerts": ""}
        self.settings_win = None
        self.details_win = None
        self._details_closed = 0.0  # when the details panel last closed
        self.reading = None          # last reading, for the details panel
        self.note = "Waiting for the first reading…"  # why there is none
        self.alerter = Alerter(lambda: self.cfg, self._on_alert_status)

        # Shared merged quota state, fed by HTTP (seed) and MQTT (live updates).
        self.quota = {}
        self.quota_lock = threading.Lock()
        self.mqtt = None
        self.last_live = 0.0     # time of last MQTT message
        self.last_grid_live = 0.0  # time the AC-input field last arrived over MQTT
        self.force_http = False  # set by "Refresh now"
        self._last_icon_key = None  # (soc, charging) last rendered

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title(APP_TITLE)
        self.tk_icon = self._tk_photo_icon()

        self.icon = pystray.Icon(
            APP_NAME,
            icon=render_icon(None, False),
            title=APP_TITLE,
            menu=pystray.Menu(
                pystray.MenuItem(lambda i: f"Battery: {self.state['soc']}%", None, enabled=False),
                pystray.MenuItem(lambda i: self.state["detail"], None, enabled=False),
                pystray.MenuItem(lambda i: self.state.get("source") or "Connecting…", None, enabled=False),
                pystray.MenuItem(lambda i: self.state.get("alerts"), None, enabled=False,
                                 visible=lambda i: bool(self.state.get("alerts"))),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Details", self._on_details, default=True),  # left click
                pystray.MenuItem("Refresh now", self._on_refresh),
                pystray.MenuItem("Settings...", self._on_settings),
                pystray.MenuItem("Quit", self._on_quit),
            ),
        )

    def _tk_photo_icon(self):
        try:
            return _photo(make_app_icon().resize((64, 64), Image.LANCZOS).convert("RGB"))
        except Exception:
            return None

    # -- cross-thread UI marshalling -------------------------------------- #
    def post(self, fn):
        self.ui_queue.put(fn)

    def _pump(self):
        try:
            while True:
                self.ui_queue.get_nowait()()
        except queue.Empty:
            pass
        if not self.stop_event.is_set():
            self.root.after(120, self._pump)

    # -- menu callbacks (run on the pystray thread) ----------------------- #
    def _on_settings(self, icon=None, item=None):
        self.post(self.open_settings)

    def _on_details(self, icon=None, item=None):
        self.post(self.toggle_details)

    def _on_refresh(self, icon, item):
        self.force_http = True
        self.refresh_event.set()

    def _on_quit(self, icon, item):
        self.stop_event.set()
        self.refresh_event.set()
        self.alerter.stop()
        try:
            self.icon.stop()
        finally:
            self.post(self.root.quit)

    def open_settings(self):
        if self.settings_win is not None:
            try:
                self.settings_win.deiconify()
                self.settings_win.lift()
                self.settings_win.focus_force()
                return
            except Exception:
                self.settings_win = None
        SettingsDialog(self)

    # -- details panel: what the tooltip says, as a flyout, on left click - #
    def toggle_details(self):
        if self.details_win is not None:
            self._close_details()
            return
        # Clicking the tray icon while the panel is open first takes focus
        # away (closing it), then delivers the click; don't reopen on that.
        if time.time() - self._details_closed < 0.4:
            return
        if not is_configured(self.cfg):
            self.open_settings()  # nothing to show yet
            return
        win = self.details_win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        self._details_label = tk.Label(win, bd=0, highlightthickness=0)
        self._details_label.pack()
        self._details_key = None
        self._refresh_details(win)

        # Flyout spot: 12px clear of the taskbar, centred on the icon, kept
        # inside the work area of whichever monitor holds the tray.
        win.update_idletasks()
        px, py = win.winfo_pointerxy()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        left, top, right, bottom = work_area(px, py)
        x = min(max(px - w // 2, left + 12), right - w - 12)
        y = min(max(py - h - 12, top + 12), bottom - h - 12)
        try:  # Windows 11 rounds the corners and adds the hairline border
            pref = ctypes.c_int(2)  # DWMWCP_ROUND
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                int(win.wm_frame(), 16), 33, ctypes.byref(pref), 4)  # DWMWA_WINDOW_CORNER_PREFERENCE
        except Exception:
            pass

        win.bind("<FocusOut>", lambda e: e.widget is win and self._close_details())
        win.bind("<Escape>", lambda e: self._close_details())
        win.bind("<Button>", lambda e: self._close_details())
        win.focus_force()
        rise = 12 if py >= bottom else -12 if py < top else 0  # slide away from the taskbar
        self._slide_in(win, x, y, rise if animations_enabled() else 0)

    def _slide_in(self, win, x, y, rise, step=0):
        steps = 10  # ~170 ms, exponential-style ease-out
        if win is not self.details_win:
            return
        ease = 1 - (1 - step / steps) ** 3 if rise else 1
        win.attributes("-alpha", ease)
        win.geometry(f"+{x}+{y + round(rise * (1 - ease))}")
        if ease < 1:
            win.after(16, self._slide_in, win, x, y, rise, step + 1)

    def _refresh_details(self, win):
        if win is not self.details_win:  # closed (or replaced) since
            return
        args = (self.reading, self.state.get("source"), self.cfg.get("device_name", ""),
                self.note, system_theme())
        if args != self._details_key:  # redraw only when something changed
            self._details_key = args
            self._details_photo = _photo(render_details(*args))  # keep a ref for Tk
            self._details_label.config(image=self._details_photo)
        win.after(1000, self._refresh_details, win)

    def _close_details(self):
        win, self.details_win = self.details_win, None
        self._details_closed = time.time()
        if win is not None:
            win.destroy()

    # -- icon/state updates ---------------------------------------------- #
    def _apply(self, reading):
        src = self.state.get("source")
        key = (reading["soc"], reading["charging"])
        if key != self._last_icon_key:  # only re-render when the % or state changes
            self.icon.icon = render_icon(reading["soc"], reading["charging"])
            self._last_icon_key = key
        self.icon.title = tooltip_for(reading) + (f"\n{src}" if src else "")
        self.reading = reading
        self.state["soc"] = reading["soc"]
        self.state["detail"] = (
            f"{reading['state']} - In {reading['watts_in']}W / Out {reading['watts_out']}W"
        )
        self.icon.update_menu()

    def _apply_status(self, soc_text, detail, tooltip):
        self.icon.icon = render_icon(None, False)
        self._last_icon_key = None
        self.icon.title = tooltip
        self.reading, self.note = None, detail
        self.state["soc"] = soc_text
        self.state["detail"] = detail
        self.icon.update_menu()

    def _merge_and_apply(self, fields, replace=False):
        soc_field = self.cfg.get("soc_field", DEFAULT_SOC_FIELD)
        with self.quota_lock:
            if replace:
                self.quota = dict(fields)
            else:
                self.quota.update(fields)
            quota = dict(self.quota)
        if soc_field in quota:  # avoid a 0% flash from a partial update
            reading = reading_from_quota(quota, soc_field)
            self._apply(reading)
            self.alerter.observe(quota, reading)

    # -- MQTT callbacks (run on the paho thread) ------------------------- #
    def _on_mqtt_update(self, fields):
        self.last_live = time.time()
        if self.cfg.get("grid_field", DEFAULT_GRID_FIELD) in fields:
            self.last_grid_live = time.time()
        self._merge_and_apply(fields)

    def _on_mqtt_status(self, text):
        self.state["source"] = text
        self.icon.update_menu()

    def _on_alert_status(self, text):
        self.state["alerts"] = text
        self.icon.update_menu()

    def start_mqtt(self):
        if self.mqtt:
            self.mqtt.stop()
            self.mqtt = None
        self.last_live = 0.0
        if is_configured(self.cfg):
            self.state["source"] = "Connecting…"
            self.mqtt = EcoflowMqtt(dict(self.cfg), self._on_mqtt_update, self._on_mqtt_status)
            self.mqtt.start()

    def apply_new_config(self):
        with self.quota_lock:
            self.quota = {}          # drop state from a previous device
        self.alerter.reset()         # don't alert on a change of device or field
        self.state["alerts"] = ""
        self.force_http = True
        self.refresh_event.set()     # re-seed over HTTP right away
        self.start_mqtt()            # reconnect MQTT with the new keys/device

    def _worker(self):
        while not self.stop_event.is_set():
            if is_configured(self.cfg):
                refresh = int(self.cfg.get("refresh_seconds", DEFAULT_REFRESH))
                soc_field = self.cfg.get("soc_field", DEFAULT_SOC_FIELD)
                with self.quota_lock:
                    have_data = soc_field in self.quota
                mqtt_fresh = (time.time() - self.last_live) < max(120, 2 * refresh)
                # AC-input fields ride on invStatus/pdStatus messages, which
                # arrive far less often than bmsStatus (a few per minute on a
                # DELTA 2 Max). A live MQTT feed therefore doesn't prove the
                # outage signal is current, so fall back to HTTP for it while
                # alerts or executions are on - but only if it really has gone
                # quiet.
                grid_fresh = (time.time() - self.last_grid_live) < max(120, 2 * refresh)
                need_grid = watching_grid(self.cfg) and not grid_fresh
                force, self.force_http = self.force_http, False
                # HTTP is a stale snapshot; use it only to seed, as a fallback
                # when MQTT is silent, or when the user hits "Refresh now".
                if force or not have_data or not mqtt_fresh or need_grid:
                    try:
                        self._merge_and_apply(fetch_full_quota(self.cfg), replace=not have_data)
                    except Exception as err:
                        self._apply_status("!", f"Error: {err}", f"{APP_TITLE}: error\n{err}")
                wait = refresh
            else:
                self._apply_status("--", "Not configured - open Settings",
                                   f"{APP_TITLE}: not configured")
                wait = 5
            self.refresh_event.wait(timeout=wait)
            self.refresh_event.clear()

    def _on_ready(self, icon):
        # Runs on the pystray thread once the tray icon exists, so the first
        # update is applied to a live icon (not lost).
        icon.visible = True
        threading.Thread(target=self._worker, daemon=True).start()
        self.alerter.start()
        if is_configured(self.cfg):
            self.start_mqtt()

    def run(self):
        threading.Thread(target=lambda: self.icon.run(setup=self._on_ready), daemon=True).start()
        self.root.after(120, self._pump)
        if not is_configured(self.cfg):
            self.root.after(400, self.open_settings)
        self.root.mainloop()


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #
def cmd_selftest():
    make_app_icon()
    render_icon(54, False)
    render_icon(None, False)
    for theme in PANEL_THEMES:
        render_details({"soc": 54, "state": "Charging", "charging": True, "watts_in": 900,
                        "watts_out": 40, "remain_min": 75}, "Live (MQTT)", "DELTA 2 Max", theme=theme)
        render_details(None, "", "", note="Error: " + "x" * 300, theme=theme)
    # malformed on purpose: substitution must never raise, whatever is typed
    render_args("--soc {soc} {typo} { {} 50%",
                exec_values("outage", None, "EcoFlow", time.time()))
    root = tk.Tk()
    root.withdraw()
    root.destroy()
    print("selftest OK")


def cmd_make_icon(path):
    make_app_icon().save(path, sizes=[(256, 256), (64, 64), (48, 48), (32, 32), (16, 16)])
    print(f"wrote {path}")


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "--selftest":
        cmd_selftest()
    elif arg == "--make-icon":
        cmd_make_icon(sys.argv[2])
    else:
        App().run()


if __name__ == "__main__":
    main()
