"""
verify_explain.py
=================
Calls /admin/explain/{endpoint} for all five endpoints against a running server,
recursively searches the explain output for COLLSCAN and IXSCAN, and reports
timings and pass/fail.

Usage:
    # Start server first:  uvicorn app.main:app --port 8000
    python verify_explain.py [--base-url http://localhost:8000]
"""
import json
import sys
import time
import argparse

try:
    import requests
except ImportError:
    print("pip install requests")
    sys.exit(1)

BASE = "http://localhost:8000"

def find_values(obj, key: str, found=None):
    """Recursively collect all values for `key` anywhere in a JSON structure."""
    if found is None:
        found = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                found.append(v)
            find_values(v, key, found)
    elif isinstance(obj, list):
        for item in obj:
            find_values(item, key, found)
    return found


def has_stage(obj: str, stage: str) -> bool:
    """Check if stage name appears anywhere in the stringified explain."""
    return stage in obj


def check_explain(explain_doc: dict) -> tuple[list[str], list[str]]:
    """
    Returns (collscans, ixscans) lists of stage descriptions found.
    We stringify and search — covers nested $lookup explain sub-documents.
    """
    explain_str = json.dumps(explain_doc)
    collscans = []
    ixscans   = []

    # Walk all "stage" keys recursively
    stages = find_values(explain_doc, "stage")
    for s in stages:
        if isinstance(s, str):
            if "COLLSCAN" in s:
                collscans.append(s)
            if "IXSCAN" in s:
                ixscans.append(s)

    # Also check "inputStage" and "inputStages" recursively via string
    if "COLLSCAN" in explain_str:
        # Find them via raw string to catch any nested location
        import re
        extra = re.findall(r'"stage"\s*:\s*"COLLSCAN"', explain_str)
        for e in extra:
            if "COLLSCAN" not in collscans:
                collscans.append("COLLSCAN (nested)")

    if "IXSCAN" in explain_str:
        pass  # already found above or via stages list

    # Deduplicate
    collscans = list(set(collscans))
    ixscans   = list(set(ixscans))
    return collscans, ixscans


ENDPOINTS = [
    {
        "name":   "attendance_list",
        "params": {"date_from": "2026-07-01", "date_to": "2026-07-31"},
        "expect_collection": "attendance_logs",
    },
    {
        "name":   "employee_monthly",
        "params": {"emp_code": "EMP00001", "month": "2026-07"},
        "expect_collection": "attendance_logs",
    },
    {
        "name":   "department_summary",
        "params": {"month": "2026-07"},
        "expect_collection": "employees",
    },
    {
        "name":   "late_leaderboard",
        "params": {"month": "2026-07"},
        "expect_collection": "attendance_logs",
    },
    {
        "name":   "department_trend",
        "params": {"department": "Engineering", "from": "2026-07-01", "to": "2026-07-31"},
        "expect_collection": "attendance_logs",
    },
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=BASE)
    args = parser.parse_args()
    base = args.base_url.rstrip("/")

    print(f"\n{'='*65}")
    print(f"  Explain verification against {base}")
    print(f"{'='*65}\n")

    all_pass = True
    timings = {}

    for ep in ENDPOINTS:
        name   = ep["name"]
        params = ep["params"]
        url    = f"{base}/admin/explain/{name}"

        t0 = time.perf_counter()
        try:
            resp = requests.get(url, params=params, timeout=60)
        except Exception as exc:
            print(f"[FAIL] {name}: connection error — {exc}")
            all_pass = False
            continue
        elapsed = time.perf_counter() - t0
        timings[name] = elapsed

        if resp.status_code != 200:
            print(f"[FAIL] {name}: HTTP {resp.status_code} — {resp.text[:200]}")
            all_pass = False
            continue

        body = resp.json()
        explain_doc = body.get("explain", {})
        explain_str = json.dumps(explain_doc)

        # Recursive search
        collscans, ixscans = check_explain(explain_doc)

        # Determine pass/fail
        has_collscan = bool(collscans) or "COLLSCAN" in explain_str
        has_ixscan   = bool(ixscans)   or "IXSCAN"   in explain_str

        status = "PASS" if (not has_collscan and has_ixscan) else "FAIL"
        if status == "FAIL":
            all_pass = False

        tag = "✓" if status == "PASS" else "✗"
        print(f"[{tag}] {name:<22}  {elapsed*1000:6.0f} ms  "
              f"IXSCAN={'yes' if has_ixscan else 'NO '}  "
              f"COLLSCAN={'NO' if not has_collscan else 'YES ← FAIL'}")

        if status == "FAIL":
            # Print the first 2000 chars of explain to diagnose
            print(f"     explain snippet: {explain_str[:2000]}")

    print(f"\n{'='*65}")
    print(f"  Overall: {'ALL PASS ✓' if all_pass else 'SOME FAILED ✗'}")
    print(f"{'='*65}\n")
    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()
