"""EcoFlow Tray - offline tests for the alerting logic.

No network, no device, no Windows needed: the Windows/GUI modules are stubbed,
so this runs anywhere. Covers grid-outage debouncing, battery thresholds, the
Telegram retry queue, program executions, and AC-input field detection across
EcoFlow generations.

Usage:
    python3 tools/test_alerts.py
"""

import io
import json
import pathlib
import sys
import tempfile
import threading
import types
import urllib.error

for _name in ("winreg", "pystray", "paho", "paho.mqtt", "paho.mqtt.client",
              "PIL", "PIL.Image", "PIL.ImageDraw", "PIL.ImageFont",
              "tkinter", "tkinter.ttk", "tkinter.messagebox"):
    _mod = types.ModuleType(_name)
    _mod.__getattr__ = lambda attr: types.SimpleNamespace()
    sys.modules.setdefault(_name, _mod)
sys.modules["paho"].mqtt = sys.modules["paho.mqtt"]
sys.modules["paho.mqtt"].client = sys.modules["paho.mqtt.client"]
sys.modules["PIL"].Image = sys.modules["PIL.Image"]
sys.modules["tkinter"].ttk = sys.modules["tkinter.ttk"]

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import time  # noqa: E402  (after the stubs, so the module import below works)

import ecoflow_tray as et  # noqa: E402

# Module level, not per test: test_gating builds its Alerter by hand, so a local
# stub wouldn't cover it - and nothing here may ever launch a real program.
RUNS = []
et.run_command = lambda path, args="": RUNS.append((path, args))

FAILS = []


def check(label, got, want):
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {label}: {got!r}" + ("" if ok else f"  (want {want!r})"))
    if not ok:
        FAILS.append(label)


def section(title):
    print(f"\n== {title} ==")


# --------------------------------------------------------------------------- #
# AC-input field detection, across both EcoFlow naming generations.
# Idle values are measured from a real DELTA 2 Max with the mains disconnected;
# the newer names come from the hassio-ecoflow-cloud device definitions.
# --------------------------------------------------------------------------- #
CLASSIC = {  # DELTA 2/Max, DELTA Pro/Max/Mini, RIVER 2/Pro/Max   (field: idle, mains)
    # Both columns measured on a real DELTA 2 Max, mains gone then restored.
    # chgLinePlug is not a 0/1 flag: it reads 34 with the cable connected.
    "inv.acInVol": (36691, 112637), "inv.acInAmp": (0, 13627), "inv.acInFreq": (41, 60),
    "inv.inputWatts": (0, 1606), "pd.wattsInSum": (0, 1602),
    "bms_emsStatus.chgLinePlug": (0, 34),
}
NEWER = {  # DELTA 3, DELTA Pro 3, RIVER 3 - renamed, and volts not millivolts
    "plug_in_info_ac_in_vol": (36, 121), "plug_in_info_ac_in_amp": (0, 3),
    "pow_get_ac_in": (0, 420), "pow_in_sum_w": (0, 420),
}
NOT_GRID = {  # must never be offered: solar, battery pack, and output fields
    "mppt.inputWatts": 0, "mppt.inWatts": 0, "bms_bmsStatus.inputWatts": 0,
    "inv.invOutVol": 120000, "pd.wattsOutSum": 455,
}


def test_field_detection():
    section("field detection")
    for label, fields, best in (("classic", CLASSIC, "inv.acInVol"),
                                ("newer", NEWER, "plug_in_info_ac_in_vol")):
        quota = {k: v[0] for k, v in fields.items()} | NOT_GRID
        cands = et.grid_candidates(quota)
        check(f"{label}: best candidate", cands[0][0], best)
        check(f"{label}: all offered", sorted(dict(cands)), sorted(fields))
        check(f"{label}: nothing leaked", [k for k, _ in cands if k in NOT_GRID], [])

    section("default thresholds separate idle from mains")
    for label, fields in (("classic", CLASSIC), ("newer", NEWER)):
        for field, (idle, mains) in fields.items():
            bar = et.default_grid_threshold(field, idle)
            check(f"{label}: {field} (thr {bar})", (idle > bar, mains > bar), (False, True))


# --------------------------------------------------------------------------- #
# Alerting engine
# --------------------------------------------------------------------------- #
CFG = {
    "device_name": "DELTA 2 Max", "soc_field": "soc",
    "grid_field": "v", "grid_threshold": 80000,
    "outage_delay_min": 2, "restore_delay_min": 3,
    "batt_alert_1": 30, "batt_alert_2": 15,
    "telegram_enabled": True, "telegram_token": "t", "telegram_chat_id": "1",
}
SENT = []
T0 = 1_000_000.0


def alerter():
    del SENT[:]
    a = et.Alerter(lambda: CFG)
    a._queue = lambda text, now: SENT.append(text)
    return a


def exec_alerter(**over):
    """An Alerter with executions on and a program in every slot."""
    del SENT[:]
    del RUNS[:]
    cfg = dict(CFG, exec_enabled=True,
               exec_outage_delay_min=0, exec_restore_delay_min=0)
    cfg.update({f"exec_{k}_path": f"{k}.lnk" for k in et.EXEC_KINDS})
    cfg.update({f"exec_{k}_args": "" for k in et.EXEC_KINDS})
    cfg.update(over)
    a = et.Alerter(lambda: cfg)
    a._queue = lambda text, now: SENT.append(text)
    return a


def settle():
    """Wait for the launcher threads _launch spawns."""
    for t in threading.enumerate():
        if t.name.startswith("exec-"):
            t.join(timeout=2)


def ran():
    settle()
    return [path for path, _ in RUNS]


def feed(a, *, volts, soc, at):
    quota = {"v": volts, "soc": soc, "pd.wattsInSum": 100 if volts > 80000 else 0,
             "pd.wattsOutSum": 200}
    real, time.time = time.time, lambda: at
    try:
        a.observe(quota, et.reading_from_quota(quota, "soc"))
    finally:
        time.time = real


def tick(a, at):
    real, time.time = time.time, lambda: at
    try:
        a.evaluate()
    finally:
        time.time = real


ON, OFF = 121000, 36691   # mains present / the idle reading that is NOT mains


def test_outage_debounce():
    section("outage / restore debouncing")
    a = alerter()
    feed(a, volts=ON, soc=90, at=T0)
    check("first reading is silent", SENT, [])

    feed(a, volts=OFF, soc=90, at=T0 + 10)
    feed(a, volts=OFF, soc=89, at=T0 + 100)          # 90s of a 120s delay
    check("silent before the delay", SENT, [])
    feed(a, volts=OFF, soc=88, at=T0 + 130)          # 120s -> fires
    check("outage fires at 2 min", len(SENT), 1)
    check("outage wording", "Power outage" in SENT[0], True)

    feed(a, volts=ON, soc=88, at=T0 + 200)           # flicker back on ...
    feed(a, volts=OFF, soc=87, at=T0 + 220)          # ... and off again
    check("flicker sends nothing", len(SENT), 1)

    feed(a, volts=ON, soc=87, at=T0 + 400)           # real restore begins
    feed(a, volts=ON, soc=90, at=T0 + 579)           # 179s of a 180s delay
    check("silent one second short", len(SENT), 1)
    feed(a, volts=ON, soc=90, at=T0 + 581)
    check("restore fires at 3 min", len(SENT), 2)
    check("restore wording", "Power restored" in SENT[1], True)
    check("restore reports the outage length", "Outage lasted" in SENT[1], True)


def test_zero_delay():
    section("zero delay")
    CFG["restore_delay_min"] = 0
    a = alerter()
    feed(a, volts=OFF, soc=50, at=T0)
    feed(a, volts=ON, soc=50, at=T0 + 1)
    check("not on the very first sighting", SENT, [])
    tick(a, T0 + 1.05)
    check("fires on the next evaluation", len(SENT), 1)
    CFG["restore_delay_min"] = 3


def test_tick_ages_out():
    section("background tick")
    a = alerter()
    feed(a, volts=ON, soc=90, at=T0)
    feed(a, volts=OFF, soc=90, at=T0 + 10)     # last reading the device sends
    tick(a, T0 + 200)                          # no new data, only the 10s tick
    check("tick alone fires the alert", len(SENT), 1)


def test_battery_levels():
    section("battery thresholds")
    a = alerter()
    feed(a, volts=OFF, soc=90, at=T0)
    feed(a, volts=OFF, soc=45, at=T0 + 60)
    check("silent above both levels", SENT, [])
    feed(a, volts=OFF, soc=30, at=T0 + 120)
    check("alert 1 at exactly 30%", len(SENT), 1)
    feed(a, volts=OFF, soc=29, at=T0 + 180)
    check("no repeat while below", len(SENT), 1)
    feed(a, volts=OFF, soc=15, at=T0 + 240)
    check("alert 2 at 15%", len(SENT), 2)
    feed(a, volts=OFF, soc=16, at=T0 + 300)
    check("no re-arm inside the margin", len(SENT), 2)
    feed(a, volts=ON, soc=22, at=T0 + 360)     # 15 + 5 -> re-arms
    feed(a, volts=ON, soc=14, at=T0 + 420)
    check("re-arms after recovering", len(SENT), 3)

    a = alerter()                               # steep drop past both levels
    feed(a, volts=OFF, soc=80, at=T0)
    feed(a, volts=OFF, soc=10, at=T0 + 60)
    check("one message on a steep drop", len(SENT), 1)
    check("uses the lower level", "below 15%" in SENT[0], True)

    CFG["batt_alert_2"] = 0                     # 0 disables
    a = alerter()
    feed(a, volts=OFF, soc=80, at=T0)
    feed(a, volts=OFF, soc=5, at=T0 + 60)
    check("0 turns an alert off", len(SENT), 1)
    CFG["batt_alert_2"] = 15

    a = alerter()                               # starting below must not alert
    feed(a, volts=OFF, soc=12, at=T0)
    feed(a, volts=OFF, soc=11, at=T0 + 60)
    check("silent when starting below", SENT, [])


def test_gating():
    section("delivery gating")
    CFG["telegram_enabled"] = False
    a = et.Alerter(lambda: CFG)
    feed(a, volts=ON, soc=90, at=T0)
    feed(a, volts=OFF, soc=90, at=T0 + 200)
    feed(a, volts=OFF, soc=90, at=T0 + 400)
    check("nothing queued while off", a.outbox, [])
    check("but state is still tracked", a.grid["alert"], False)
    CFG["telegram_enabled"] = True


# --------------------------------------------------------------------------- #
# Running local programs
# --------------------------------------------------------------------------- #
def test_exec_independent_delay():
    section("executions: their own delay")
    a = exec_alerter(outage_delay_min=2, exec_outage_delay_min=0)
    feed(a, volts=ON, soc=90, at=T0)
    feed(a, volts=OFF, soc=90, at=T0 + 10)
    tick(a, T0 + 11)
    check("the program runs at once", ran(), ["outage.lnk"])
    check("telegram is still waiting", SENT, [])
    tick(a, T0 + 140)
    check("the alert fires at 2 min", len(SENT), 1)
    check("the program did not run twice", len(ran()), 1)


def test_exec_flicker_independent():
    section("executions: a flicker with a 0 delay")
    a = exec_alerter(outage_delay_min=2, restore_delay_min=2)
    feed(a, volts=ON, soc=90, at=T0)
    feed(a, volts=OFF, soc=90, at=T0 + 10)
    tick(a, T0 + 11)
    feed(a, volts=ON, soc=90, at=T0 + 20)      # back before the alert delay
    tick(a, T0 + 21)
    check("both slots ran", ran(), ["outage.lnk", "restore.lnk"])
    check("telegram stayed quiet", SENT, [])


def test_exec_disabled():
    section("executions: switched off")
    a = exec_alerter(exec_enabled=False)
    feed(a, volts=ON, soc=90, at=T0)
    feed(a, volts=OFF, soc=90, at=T0 + 10)
    tick(a, T0 + 11)
    check("nothing launched", ran(), [])
    check("but state is still tracked", a.grid["exec"], False)


def test_exec_empty_path():
    section("executions: an empty slot")
    a = exec_alerter(exec_outage_path="")
    feed(a, volts=ON, soc=90, at=T0)
    feed(a, volts=OFF, soc=90, at=T0 + 10)
    tick(a, T0 + 11)
    check("an empty path is simply off", ran(), [])


def test_exec_battery_slots():
    section("executions: battery slots")
    a = exec_alerter()
    feed(a, volts=OFF, soc=80, at=T0)
    feed(a, volts=OFF, soc=10, at=T0 + 60)     # steep drop past both levels
    check("still one message", len(SENT), 1)
    check("but both slots ran", sorted(ran()), ["batt1.lnk", "batt2.lnk"])


def test_exec_reset_is_silent():
    section("executions: reset re-adopts silently")
    a = exec_alerter()
    feed(a, volts=ON, soc=90, at=T0)
    a.reset()                                   # e.g. Settings saved, or a reboot
    feed(a, volts=OFF, soc=90, at=T0 + 10)
    tick(a, T0 + 20)
    check("nothing launched after a reset", ran(), [])
    check("nothing sent after a reset", SENT, [])


def test_render_args():
    section("argument placeholders")
    vals = et.exec_values("outage", {"soc": 42, "watts_in": 0, "watts_out": 120},
                          "DELTA 2 Max", T0)
    check("known keys are substituted",
          et.render_args('--soc {soc} --dev "{device}"', vals),
          '--soc 42 --dev "DELTA 2 Max"')
    check("the event name is available", et.render_args("{event}", vals), "outage")
    check("a typo is left alone", et.render_args("{sock}", vals), "{sock}")
    check("stray braces survive", et.render_args("50% { {} }", vals), "50% { {} }")
    check("every key is present", sorted(vals),
          ["device", "event", "soc", "time", "watts_in", "watts_out"])


# --------------------------------------------------------------------------- #
# Telegram API + retry queue
# --------------------------------------------------------------------------- #
def fake_urlopen(payload, status=200):
    class Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps(payload).encode()

    def opener(req, timeout=None):
        if status != 200:
            raise urllib.error.HTTPError(req.full_url, status, "err", {},
                                         io.BytesIO(json.dumps(payload).encode()))
        return Resp()
    return opener


def test_telegram_api():
    section("telegram api")
    et.urllib.request.urlopen = fake_urlopen({"ok": True, "result": {"username": "my_bot"}})
    check("getMe returns the bot name", et.telegram_check_token("123:abc"), "my_bot")

    et.urllib.request.urlopen = fake_urlopen({"ok": False, "description": "Unauthorized"}, 401)
    try:
        et.telegram_check_token("bad")
        check("bad token raises", False, True)
    except RuntimeError as err:
        check("bad token gives the reason", str(err), "Unauthorized")

    et.urllib.request.urlopen = fake_urlopen({"ok": True, "result": [
        {"message": {"chat": {"id": 111, "first_name": "Old"}}},
        {"message": {"chat": {"id": 999, "first_name": "Eduar", "last_name": "B"}}}]})
    check("detects the newest chat", et.telegram_detect_chat("t"), ("999", "Eduar B"))

    et.urllib.request.urlopen = fake_urlopen({"ok": True, "result": [
        {"channel_post": {"chat": {"id": -100200, "title": "Home"}}}]})
    check("supports groups/channels", et.telegram_detect_chat("t"), ("-100200", "Home"))

    et.urllib.request.urlopen = fake_urlopen({"ok": True, "result": []})
    check("no messages yet", et.telegram_detect_chat("t"), (None, None))


def test_retry_queue():
    section("retry queue")
    cfg = {"telegram_enabled": True, "telegram_token": "t", "telegram_chat_id": "1"}
    a = et.Alerter(lambda: cfg)
    statuses = []
    a.on_status = statuses.append
    tries = []

    def flaky(token, chat, text):
        tries.append(text)
        if len(tries) < 3:                       # router down for the first two
            raise OSError("Network is unreachable")

    et.telegram_send = flaky
    now = [T0]
    real, time.time = time.time, lambda: now[0]
    try:
        a._queue("outage!", now[0])
        a._flush()
        check("kept after a failed send", len(a.outbox), 1)
        check("failure surfaced", "send failed" in statuses[-1], True)
        a._flush()
        check("respects the backoff", len(tries), 1)
        now[0] += 20
        a._flush()
        check("retries after the backoff", len(tries), 2)
        now[0] += 60
        a._flush()
        check("delivered on the third try", len(tries), 3)
        check("queue drained", a.outbox, [])

        a._queue("ancient", now[0])              # older than the cap -> dropped
        a.outbox[0]["created"] = now[0] - et.Alerter.MAX_AGE_SECONDS - 1
        a._flush()
        check("stale alert dropped", a.outbox, [])

        a._queue("pending", now[0])              # switched off -> discarded
        cfg["telegram_enabled"] = False
        before = len(tries)
        a._flush()
        check("nothing sent once disabled", len(tries), before)
        check("queue discarded once disabled", a.outbox, [])
    finally:
        time.time = real


def test_config_roundtrip():
    section("config round-trip")
    tmp = pathlib.Path(tempfile.mkdtemp())
    et.CONFIG_DIR, et.CONFIG_PATH = tmp, tmp / "config.json"
    et.dpapi_encrypt = lambda text: None          # simulate DPAPI unavailable
    et.save_config({"access_key": "AK", "secret_key": "SK", "sn": "SN",
                    "telegram_token": "123:abc", "telegram_chat_id": "42",
                    "telegram_enabled": True, "outage_delay_min": 7,
                    "batt_alert_1": 25, "exec_enabled": True,
                    "exec_outage_path": r"C:\tools\lights.lnk",
                    "exec_outage_args": "--soc {soc}",
                    "exec_outage_delay_min": 2})
    back = et.load_config()
    check("token round-trips", back["telegram_token"], "123:abc")
    check("delay round-trips", back["outage_delay_min"], 7.0)
    check("battery level round-trips", back["batt_alert_1"], 25)
    check("defaults fill in", back["restore_delay_min"], et.DEFAULT_RESTORE_DELAY_MIN)
    check("exec path round-trips", back["exec_outage_path"], r"C:\tools\lights.lnk")
    check("exec args round-trip", back["exec_outage_args"], "--soc {soc}")
    check("exec delay round-trips", back["exec_outage_delay_min"], 2.0)
    check("exec defaults fill in", back["exec_batt1_path"], "")


def main():
    for test in (test_field_detection, test_outage_debounce, test_zero_delay,
                 test_tick_ages_out, test_battery_levels, test_gating,
                 test_exec_independent_delay, test_exec_flicker_independent,
                 test_exec_disabled, test_exec_empty_path, test_exec_battery_slots,
                 test_exec_reset_is_silent, test_render_args,
                 test_telegram_api, test_retry_queue, test_config_roundtrip):
        test()
    print("\n" + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
