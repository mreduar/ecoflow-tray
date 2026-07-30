"""
EcoFlow IoT Open Platform - connectivity test.

Validates the AccessKey/SecretKey, lists your devices (to discover the SN),
and dumps the full quota of each device so we can locate the battery SoC field.

Usage:
    ECOFLOW_ACCESS_KEY=xxx ECOFLOW_SECRET_KEY=yyy python3 test_api.py

Optional:
    ECOFLOW_HOST=https://api-e.ecoflow.com   # use this if you registered on the EU portal
"""

import hashlib
import hmac
import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request

# Global/US host by default. EU-registered accounts: https://api-e.ecoflow.com
HOST = os.environ.get("ECOFLOW_HOST", "https://api.ecoflow.com")
ACCESS_KEY = os.environ.get("ECOFLOW_ACCESS_KEY", "")
SECRET_KEY = os.environ.get("ECOFLOW_SECRET_KEY", "")


def flatten(obj, prefix=""):
    """Flatten nested dicts/lists into EcoFlow's key notation (a.b, a[0])."""
    result = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            result.update(flatten(v, f"{prefix}.{k}" if prefix else k))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            result.update(flatten(item, f"{prefix}[{i}]"))
    else:
        result[prefix] = obj
    return result


def query_string(params):
    """key=value pairs joined by &, sorted by key ascending."""
    return "&".join(f"{k}={params[k]}" for k in sorted(params))


def sign(params, nonce, timestamp):
    auth = {"accessKey": ACCESS_KEY, "nonce": nonce, "timestamp": timestamp}
    sign_str = (query_string(flatten(params)) + "&" if params else "") + query_string(auth)
    digest = hmac.new(SECRET_KEY.encode(), sign_str.encode(), hashlib.sha256).digest()
    return digest.hex()


def api_get(path, params=None):
    nonce = str(random.randint(100000, 999999))
    timestamp = str(int(time.time() * 1000))
    headers = {
        "accessKey": ACCESS_KEY,
        "nonce": nonce,
        "timestamp": timestamp,
        "sign": sign(params, nonce, timestamp),
    }
    url = HOST + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def main():
    if not ACCESS_KEY or not SECRET_KEY:
        print("Set ECOFLOW_ACCESS_KEY and ECOFLOW_SECRET_KEY environment variables first.")
        return

    print(f"Host: {HOST}\n")

    print("== Device list ==")
    devices = api_get("/iot-open/sign/device/list")
    print(json.dumps(devices, indent=2, ensure_ascii=False))

    for device in devices.get("data", []) or []:
        sn = device.get("sn")
        if not sn:
            continue
        print(f"\n== Quota for {sn} ({device.get('productName', '?')}) ==")
        quota = api_get("/iot-open/sign/device/quota/all", {"sn": sn})
        data = quota.get("data", {}) or {}

        # Highlight any field that looks like a state-of-charge / battery percentage.
        soc_fields = {k: v for k, v in data.items() if "soc" in k.lower()}
        print("Candidate battery/SoC fields:")
        print(json.dumps(soc_fields, indent=2, ensure_ascii=False) if soc_fields else "  (none matched 'soc')")

        with open(f"quota_{sn}.json", "w", encoding="utf-8") as f:
            json.dump(quota, f, indent=2, ensure_ascii=False)
        print(f"Full quota saved to quota_{sn}.json")


if __name__ == "__main__":
    main()
