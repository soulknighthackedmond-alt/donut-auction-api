"""Smoke test for the donut.auction price API.

Runs against a live server (default http://127.0.0.1:8787) and against the real
donut.auction upstream, so it needs network access.

    python test_smoke.py [base_url]
"""

import json
import sys
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8787").rstrip("/")
failures = []


def call(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=45) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def check(label, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + label + (f"  {detail}" if detail else ""))
    if not ok:
        failures.append(label)


st, body = call("/health")
check("/health", st == 200 and body.get("status") == "ok", str(body)[:120])

st, body = call("/price?query=netherite_ingot")
ok = (
    st == 200
    and body.get("found")
    and body["item"]["itemName"] == "netherite_ingot"
    and isinstance(body["price"]["value"], (int, float))
)
check("/price?query=netherite_ingot", ok, str(body.get("item"))[:120])

# messy input should normalise to the same item
st, body = call("/price?query=Netherite%20Ingot")
check("query normalisation (spaces/case)", st == 200 and body["item"]["itemName"] == "netherite_ingot")

st, body = call("/price?query=netherite_ingot&period=7d")
check("/price trend period=7d", st == 200 and body.get("trend", {}).get("points", 0) > 0, str(body.get("trend"))[:160])

st, body = call("/prices?query=netherite&limit=5")
check("/prices partial match", st == 200 and body["count"] > 0 and len(body["results"]) <= 5, f"count={body.get('count')}")

st, body = call("/history?query=netherite_ingot&period=1d")
check("/history period=1d", st == 200 and len(body.get("dataPoints", [])) > 0, f"points={len(body.get('dataPoints', []))}")

st, body = call("/transactions?query=netherite_ingot&limit=3")
check("/transactions", st == 200 and len(body.get("transactions", [])) <= 3, f"count={body.get('count')}")

st, body = call("/listings?query=netherite_ingot")
check("/listings", st == 200 and "listings" in body, f"count={body.get('count')}")

st, body = call("/price?query=definitely_not_a_real_item_xyz")
check("unknown item -> 404", st == 404, f"status={st}")

st, body = call("/price?query=netherite_ingot&period=bogus")
check("bad period -> 400", st == 400, f"status={st}")

print()
if failures:
    print(f"{len(failures)} check(s) failed: {failures}")
    sys.exit(1)
print("all checks passed")
