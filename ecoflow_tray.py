"""
EcoFlow Tray - a Windows system-tray battery monitor for EcoFlow power stations.

Shows the battery percentage as a live tray icon. Right-click for a menu with
device status, a Settings dialog (enter your API keys, pick your device, set the
polling interval) and Quit.

Credentials are stored per-user in %APPDATA%\\EcoFlowTray\\config.json. The secret
key is encrypted at rest with Windows DPAPI (tied to the current user account).

Modes:
    EcoFlowTray.exe                Start the tray app (Settings opens on first run)
    ecoflow_tray.py --selftest     Import/render checks, no network, exit
    ecoflow_tray.py --make-icon P  Write the app .ico to path P and exit
"""

import base64
import ctypes
import hashlib
import hmac
import json
import os
import queue
import random
import sys
import threading
import time
import urllib.parse
import urllib.request
import winreg
from ctypes import wintypes
from pathlib import Path

import tkinter as tk
from tkinter import messagebox, ttk

import paho.mqtt.client as mqtt
import pystray
from PIL import Image, ImageDraw, ImageFont

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
def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if cfg.get("secret_key_enc"):
        try:
            cfg["secret_key"] = dpapi_decrypt(cfg["secret_key_enc"])
        except Exception:
            cfg["secret_key"] = ""
    cfg.setdefault("secret_key", "")
    cfg.setdefault("host", DEFAULT_HOST)
    cfg.setdefault("refresh_seconds", DEFAULT_REFRESH)
    cfg.setdefault("soc_field", DEFAULT_SOC_FIELD)
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
    }
    secret = cfg.get("secret_key", "")
    enc = dpapi_encrypt(secret) if secret else None
    if enc:
        out["secret_key_enc"] = enc
    else:
        out["secret_key"] = secret  # plaintext fallback if DPAPI is unavailable
    CONFIG_PATH.write_text(json.dumps(out, indent=2), encoding="utf-8")


def is_configured(cfg: dict) -> bool:
    return bool(cfg.get("access_key") and cfg.get("secret_key") and cfg.get("sn"))


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

        frm = ttk.Frame(win, padding=16)
        frm.grid(sticky="nsew")
        frm.columnconfigure(1, weight=1)
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

        btns = ttk.Frame(frm)
        btns.grid(row=row, column=0, columnspan=3, sticky="e", pady=(16, 0))
        ttk.Button(btns, text="Cancel", command=self.close).grid(row=0, column=0, padx=6)
        ttk.Button(btns, text="Save", command=self._on_save).grid(row=0, column=1)

        win.update_idletasks()
        win.lift()
        win.focus_force()

        # If already configured, detect live fields on open so the dropdown
        # shows real values without needing to click "Test" again.
        if is_configured(cfg):
            win.after(150, self._load_soc_fields)

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
                cands = soc_candidates(data)
                self.app.post(lambda: self._soc_loaded(cands))
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
        try:
            refresh = max(10, int(float(self.sp_refresh.get())))
        except ValueError:
            refresh = DEFAULT_REFRESH
        self.app.cfg = {
            "access_key": access,
            "secret_key": secret,
            "sn": sn,
            "device_name": name,
            "host": HOSTS[self.cb_region.get()],
            "refresh_seconds": refresh,
            "soc_field": self._selected_soc_field(),
        }
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
        self.state = {"soc": "--", "detail": "Not configured", "source": ""}
        self.settings_win = None

        # Shared merged quota state, fed by HTTP (seed) and MQTT (live updates).
        self.quota = {}
        self.quota_lock = threading.Lock()
        self.mqtt = None
        self.last_live = 0.0     # time of last MQTT message
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
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Refresh now", self._on_refresh),
                pystray.MenuItem("Settings...", self._on_settings, default=True),
                pystray.MenuItem("Quit", self._on_quit),
            ),
        )

    def _tk_photo_icon(self):
        try:
            img = make_app_icon().resize((64, 64), Image.LANCZOS)
            photo = tk.PhotoImage(width=64, height=64)
            # Build a base64 PPM so PhotoImage can load it without a temp file.
            import io
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="PNG")
            return tk.PhotoImage(data=base64.b64encode(buf.getvalue()))
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

    def _on_refresh(self, icon, item):
        self.force_http = True
        self.refresh_event.set()

    def _on_quit(self, icon, item):
        self.stop_event.set()
        self.refresh_event.set()
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

    # -- icon/state updates ---------------------------------------------- #
    def _apply(self, reading):
        src = self.state.get("source")
        key = (reading["soc"], reading["charging"])
        if key != self._last_icon_key:  # only re-render when the % or state changes
            self.icon.icon = render_icon(reading["soc"], reading["charging"])
            self._last_icon_key = key
        self.icon.title = tooltip_for(reading) + (f"\n{src}" if src else "")
        self.state["soc"] = reading["soc"]
        self.state["detail"] = (
            f"{reading['state']} - In {reading['watts_in']}W / Out {reading['watts_out']}W"
        )
        self.icon.update_menu()

    def _apply_status(self, soc_text, detail, tooltip):
        self.icon.icon = render_icon(None, False)
        self._last_icon_key = None
        self.icon.title = tooltip
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
            self._apply(reading_from_quota(quota, soc_field))

    # -- MQTT callbacks (run on the paho thread) ------------------------- #
    def _on_mqtt_update(self, fields):
        self.last_live = time.time()
        self._merge_and_apply(fields)

    def _on_mqtt_status(self, text):
        self.state["source"] = text
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
                force, self.force_http = self.force_http, False
                # HTTP is a stale snapshot; use it only to seed, as a fallback
                # when MQTT is silent, or when the user hits "Refresh now".
                if force or not have_data or not mqtt_fresh:
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
