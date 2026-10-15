"""
_fuzz_test.py
=============
Final audit fuzz test.  Hits every endpoint with malformed / boundary inputs
and asserts that the response is never 500.  Only 4xx responses are acceptable
for bad inputs; 2xx is acceptable for inputs the server chooses to handle.

Requires a running server at http://localhost:8000 with any seeded data.

Usage:  python _fuzz_test.py
"""
import sys
import time
import requests

BASE = "http://localhost:8000"
PASS = 0
FAIL = 0
CASES = []


def check(label: str, method: str, path: str, *, json=None, params=None,
          expect_not: tuple = (500,), expect_one_of: tuple = None,
          timeout: int = 30):
    global PASS, FAIL
    url = BASE + path
    try:
        r = getattr(requests, method)(url, json=json, params=params, timeout=timeout)
    except Exception as e:
        print(f"  [ERROR] {label}: connection error — {e}")
        FAIL += 1
        return

    bad = r.status_code in expect_not
    if expect_one_of:
        ok = r.status_code in expect_one_of
    else:
        ok = not bad

    tag = "PASS" if ok else "FAIL"
    if not ok:
        FAIL += 1
        print(f"  [FAIL] {label}: HTTP {r.status_code}  {r.text[:120]}")
    else:
        PASS += 1
        CASES.append((label, r.status_code))


# ── helpers ──────────────────────────────────────────────────────────────────

def ist_ms(year, month, day, hour, minute, second=0):
    from datetime import datetime, timezone, timedelta
    IST = timezone(timedelta(hours=5, minutes=30))
    return int(datetime(year, month, day, hour, minute, second, tzinfo=IST).timestamp()) * 1000


# Seed one usable employee + record for tests that need a real object
FUZZ_EMP = "EMP00000"  # exists in 100k dataset; fallback: create our own

def seed_employee():
    # Try to create our own; if the DB already has EMP00000 from 100k seed, use it
    r = requests.post(f"{BASE}/employees", json={
        "emp_code":   "EMP09999",
        "name":       "Fuzz User",
        "email":      "fuzz@fuzz.com",
        "department": "FuzzDept",
        "joined_on":  "2026-01-01",
    }, timeout=10)
    global FUZZ_EMP
    if r.status_code in (201, 409):
        FUZZ_EMP = "EMP09999"
        return True
    return False


def seed_punchin():
    r = requests.post(f"{BASE}/attendance/punch-in", json={
        "emp_code":   FUZZ_EMP,
        "punched_at": ist_ms(2026, 7, 15, 9, 28),
    }, timeout=10)
    return r.status_code in (201, 409)


def get_fuzz_dept():
    """Return a department that definitely exists in the DB."""
    r = requests.get(f"{BASE}/employees", params={"page_size": 1}, timeout=10)
    if r.status_code == 200 and r.json()["items"]:
        return r.json()["items"][0]["department"]
    return "FuzzDept"


# ── /health ──────────────────────────────────────────────────────────────────
print("\n=== Setup ===")
seed_employee()
FUZZ_DEPT = get_fuzz_dept()
seed_punchin()
print(f"  Using emp_code={FUZZ_EMP}, department={FUZZ_DEPT}")

print("\n=== /health ===")
check("health ok", "get", "/health", expect_one_of=(200,))

# ── /employees POST ──────────────────────────────────────────────────────────
print("\n=== POST /employees ===")
check("bad emp_code pattern",    "post", "/employees",
      json={"emp_code":"BADCODE","name":"X","email":"x@x.com","department":"X","joined_on":"2026-01-01"},
      expect_one_of=(422,))
check("emp_code too short digits","post", "/employees",
      json={"emp_code":"EMP1","name":"X","email":"x@x.com","department":"X","joined_on":"2026-01-01"},
      expect_one_of=(422,))
check("bad email",               "post", "/employees",
      json={"emp_code":"EMP9001","name":"X","email":"notanemail","department":"X","joined_on":"2026-01-01"},
      expect_one_of=(422,))
check("shift_start == shift_end","post", "/employees",
      json={"emp_code":"EMP9002","name":"X","email":"x@x.com","department":"X",
            "shift_start":"09:30","shift_end":"09:30","joined_on":"2026-01-01"},
      expect_one_of=(422,))
check("bad joined_on",           "post", "/employees",
      json={"emp_code":"EMP9003","name":"X","email":"x@x.com","department":"X","joined_on":"not-a-date"},
      expect_one_of=(422,))
check("empty name",              "post", "/employees",
      json={"emp_code":"EMP9004","name":"","email":"x@x.com","department":"X","joined_on":"2026-01-01"},
      expect_one_of=(422,))
check("missing required field",  "post", "/employees",
      json={"name":"X","email":"x@x.com","department":"X","joined_on":"2026-01-01"},
      expect_one_of=(422,))
check("extra unknown field ok",  "post", "/employees",
      json={"emp_code":"EMP9005","name":"X","email":"x@x.com","department":"X",
            "joined_on":"2026-01-01","extra_field":"ignored"},
      expect_one_of=(201, 409, 422))  # FastAPI ignores extra fields → 201/409
check("name too long (101 chars)","post", "/employees",
      json={"emp_code":"EMP9006","name":"A"*101,"email":"x@x.com","department":"X","joined_on":"2026-01-01"},
      expect_one_of=(422,))

# ── /employees GET ───────────────────────────────────────────────────────────
print("\n=== GET /employees ===")
check("page=0 → 422",             "get", "/employees", params={"page": 0},       expect_one_of=(422,))
check("page_size=101 → 422",      "get", "/employees", params={"page_size": 101},expect_one_of=(422,))
check("page_size=0 → 422",        "get", "/employees", params={"page_size": 0},  expect_one_of=(422,))
check("page=string → 422",        "get", "/employees", params={"page": "abc"},   expect_one_of=(422,))
check("page_size=float → 422",    "get", "/employees", params={"page_size": 1.5},expect_one_of=(422,))
check("valid page=1",             "get", "/employees", params={"page": 1},       expect_one_of=(200,))
check("page_size=100 (max)",      "get", "/employees", params={"page_size": 100},expect_one_of=(200,))
check("unknown dept ok (empty)",  "get", "/employees", params={"department": "NoSuchDept"}, expect_one_of=(200,))

# ── /attendance/punch-in ─────────────────────────────────────────────────────
print("\n=== POST /attendance/punch-in ===")
check("float timestamp",          "post", "/attendance/punch-in",
      json={"emp_code": FUZZ_EMP,"punched_at": 1783312500.0},      expect_one_of=(422,))
check("string timestamp",         "post", "/attendance/punch-in",
      json={"emp_code": FUZZ_EMP,"punched_at": "2026-07-06T09:28:00"}, expect_one_of=(422,))
check("seconds-scale timestamp",  "post", "/attendance/punch-in",
      json={"emp_code": FUZZ_EMP,"punched_at": 1783312},            expect_one_of=(422,))
check("boolean timestamp",        "post", "/attendance/punch-in",
      json={"emp_code": FUZZ_EMP,"punched_at": True},               expect_one_of=(422,))
check("status=ABSENT rejected",   "post", "/attendance/punch-in",
      json={"emp_code": FUZZ_EMP,"punched_at": ist_ms(2026,8,1,9,0),"status":"ABSENT"}, expect_one_of=(422,))
check("status=LEAVE rejected",    "post", "/attendance/punch-in",
      json={"emp_code": FUZZ_EMP,"punched_at": ist_ms(2026,8,2,9,0),"status":"LEAVE"},  expect_one_of=(422,))
check("unknown employee → 404",   "post", "/attendance/punch-in",
      json={"emp_code":"EMP99999","punched_at": ist_ms(2026,7,1,9,0)}, expect_one_of=(404,))
check("missing emp_code → 422",   "post", "/attendance/punch-in",
      json={"punched_at": ist_ms(2026,7,1,9,0)},                   expect_one_of=(422,))
check("null punched_at uses now", "post", "/attendance/punch-in",
      json={"emp_code": FUZZ_EMP},                                   expect_one_of=(201, 409))

# ── /attendance/punch-out ────────────────────────────────────────────────────
print("\n=== POST /attendance/punch-out ===")
check("float timestamp",          "post", "/attendance/punch-out",
      json={"emp_code": FUZZ_EMP,"punched_at": 1783312500.0},      expect_one_of=(422,))
check("seconds-scale timestamp",  "post", "/attendance/punch-out",
      json={"emp_code": FUZZ_EMP,"punched_at": 1000000},            expect_one_of=(422,))
check("unknown employee → 404",   "post", "/attendance/punch-out",
      json={"emp_code":"EMP99999","punched_at": ist_ms(2026,7,15,18,0)}, expect_one_of=(404,))
check("missing emp_code → 422",   "post", "/attendance/punch-out",
      json={"punched_at": ist_ms(2026,7,15,18,0)},                  expect_one_of=(422,))

check("punch_out before punch_in","post", "/attendance/punch-out",
      json={"emp_code": FUZZ_EMP,"punched_at": ist_ms(2026,7,15,8,0)},
      # 8:00 IST < 9:28 IST punch-in → no open record found at that time → 404
      expect_one_of=(404, 422))
check("punch_out >24h after",     "post", "/attendance/punch-out",
      json={"emp_code": FUZZ_EMP,"punched_at": ist_ms(2026,7,17,9,28)}, expect_one_of=(422,))

# ── GET /attendance ──────────────────────────────────────────────────────────
print("\n=== GET /attendance ===")
check("page=0 → 422",             "get", "/attendance", params={"page":0},         expect_one_of=(422,))
check("page_size=101 → 422",      "get", "/attendance", params={"page_size":101},  expect_one_of=(422,))
check("date_from bad format",     "get", "/attendance", params={"date_from":"not-a-date"}, expect_one_of=(422,))
check("date_to bad format",       "get", "/attendance", params={"date_to":"2026-13-01"},   expect_one_of=(422,))
check("date_from > date_to",      "get", "/attendance",
      params={"date_from":"2026-07-31","date_to":"2026-07-01"},     expect_one_of=(422,))
check("invalid status value",     "get", "/attendance", params={"status":"UNKNOWN"},expect_one_of=(422,))
check("valid request",            "get", "/attendance",
      params={"date_from":"2026-07-01","date_to":"2026-07-31"},     expect_one_of=(200,))
check("float page_size",          "get", "/attendance", params={"page_size":1.9},  expect_one_of=(422,))

# ── PATCH /attendance/{emp_code}/{date} ──────────────────────────────────────
print("\n=== PATCH /attendance ===")
check("bad date path",            "patch", "/attendance/FUZZ001/not-a-date",
      json={"reason":"test reason ok","regularized_by":"hr"},       expect_one_of=(422,))
check("unknown employee → 404",   "patch", "/attendance/EMP99999/2026-07-15",
      json={"reason":"test reason ok","regularized_by":"hr"},       expect_one_of=(404,))
check("no record → 404",          "patch", f"/attendance/{FUZZ_EMP}/2026-01-01",
      json={"reason":"test reason ok","regularized_by":"hr"},       expect_one_of=(404,))
check("float punch_in",           "patch", f"/attendance/{FUZZ_EMP}/2026-07-15",
      json={"punch_in":1783312500.0,"reason":"valid reason here","regularized_by":"hr"}, expect_one_of=(422,))
check("reason too short (<5)",    "patch", f"/attendance/{FUZZ_EMP}/2026-07-15",
      json={"reason":"xx","regularized_by":"hr"},                   expect_one_of=(422,))
check("ABSENT + punch_in → 422",  "patch", f"/attendance/{FUZZ_EMP}/2026-07-15",
      json={"status":"ABSENT","punch_in":ist_ms(2026,7,15,9,0),
            "reason":"test reason ok","regularized_by":"hr"},       expect_one_of=(422,))
check("missing reason → 422",     "patch", f"/attendance/{FUZZ_EMP}/2026-07-15",
      json={"regularized_by":"hr"},                                 expect_one_of=(422,))
check("missing reg_by → 422",     "patch", f"/attendance/{FUZZ_EMP}/2026-07-15",
      json={"reason":"test reason ok"},                             expect_one_of=(422,))

# ── /analytics/employees/{emp_code}/monthly ──────────────────────────────────
print("\n=== GET /analytics/employees/{emp_code}/monthly ===")
check("bad month format",         "get", "/analytics/employees/FUZZ001/monthly",
      params={"month":"2026-13"},                                   expect_one_of=(422,))
check("month as date",            "get", "/analytics/employees/FUZZ001/monthly",
      params={"month":"2026-07-01"},                                expect_one_of=(422,))
check("month missing",            "get", "/analytics/employees/FUZZ001/monthly",
                                                                    expect_one_of=(422,))
check("unknown employee → 404",   "get", "/analytics/employees/EMP99999/monthly",
      params={"month":"2026-07"},                                   expect_one_of=(404,))
check("valid request",            "get", f"/analytics/employees/{FUZZ_EMP}/monthly",
      params={"month":"2026-07"},                                   expect_one_of=(200,))

# ── /analytics/departments/summary ──────────────────────────────────────────
print("\n=== GET /analytics/departments/summary ===")
check("bad month",                "get", "/analytics/departments/summary",
      params={"month":"2026-00"},                                   expect_one_of=(422,))
check("month missing",            "get", "/analytics/departments/summary",
                                                                    expect_one_of=(422,))
check("valid request",            "get", "/analytics/departments/summary",
      params={"month":"2026-07"},                                   expect_one_of=(200,))
check("dept filter no match",     "get", "/analytics/departments/summary",
      params={"month":"2026-07","department":"NoSuchDept"},         expect_one_of=(200,))  # empty items ok

# ── /analytics/leaderboard/late ──────────────────────────────────────────────
print("\n=== GET /analytics/leaderboard/late ===")
check("bad month",                "get", "/analytics/leaderboard/late",
      params={"month":"26-07"},                                     expect_one_of=(422,))
check("limit=0 → 422",            "get", "/analytics/leaderboard/late",
      params={"month":"2026-07","limit":0},                        expect_one_of=(422,))
check("limit=51 → 422",           "get", "/analytics/leaderboard/late",
      params={"month":"2026-07","limit":51},                       expect_one_of=(422,))
check("limit=float → 422",        "get", "/analytics/leaderboard/late",
      params={"month":"2026-07","limit":1.5},                      expect_one_of=(422,))
check("valid request",            "get", "/analytics/leaderboard/late",
      params={"month":"2026-07"},                                   expect_one_of=(200,))
check("month missing",            "get", "/analytics/leaderboard/late",
                                                                    expect_one_of=(422,))

# ── /analytics/departments/{dept}/trend ──────────────────────────────────────
print("\n=== GET /analytics/departments/{department}/trend ===")
check("to before from",           "get", f"/analytics/departments/{FUZZ_DEPT}/trend",
      params={"from":"2026-07-10","to":"2026-07-01"},               expect_one_of=(422,))
check("range > 92 days",          "get", f"/analytics/departments/{FUZZ_DEPT}/trend",
      params={"from":"2026-01-01","to":"2026-04-05"},               expect_one_of=(422,))
check("bad from date",            "get", f"/analytics/departments/{FUZZ_DEPT}/trend",
      params={"from":"not-a-date","to":"2026-07-31"},               expect_one_of=(422,))
check("bad to date",              "get", f"/analytics/departments/{FUZZ_DEPT}/trend",
      params={"from":"2026-07-01","to":"2026-13-31"},               expect_one_of=(422,))
check("missing from → 422",       "get", f"/analytics/departments/{FUZZ_DEPT}/trend",
      params={"to":"2026-07-31"},                                   expect_one_of=(422,))
check("missing to → 422",         "get", f"/analytics/departments/{FUZZ_DEPT}/trend",
      params={"from":"2026-07-01"},                                 expect_one_of=(422,))
check("unknown dept → 404",       "get", "/analytics/departments/NoSuchDept9999/trend",
      params={"from":"2026-07-01","to":"2026-07-31"},               expect_one_of=(404,))
check("valid request",            "get", f"/analytics/departments/{FUZZ_DEPT}/trend",
      params={"from":"2026-07-01","to":"2026-07-31"},               expect_one_of=(200,), timeout=60)

# ── /admin/explain ────────────────────────────────────────────────────────────
print("\n=== GET /admin/explain/{endpoint} ===")
check("unknown endpoint → 404",   "get", "/admin/explain/nonexistent",    expect_one_of=(404,))
check("missing month → 422",      "get", "/admin/explain/employee_monthly",
      params={"emp_code":"FUZZ001"},                                expect_one_of=(422,))
check("missing emp_code → 422",   "get", "/admin/explain/employee_monthly",
      params={"month":"2026-07"},                                   expect_one_of=(422,))
check("bad month → 422",          "get", "/admin/explain/department_summary",
      params={"month":"2026-13"},                                   expect_one_of=(422,))
check("trend missing from → 422", "get", "/admin/explain/department_trend",
      params={"department":"FuzzDept","to":"2026-07-31"},           expect_one_of=(422,))
check("trend missing dept → 422", "get", "/admin/explain/department_trend",
      params={"from":"2026-07-01","to":"2026-07-31"},               expect_one_of=(422,))
check("page=0 on list → 422",     "get", "/admin/explain/attendance_list",
      params={"page":0},                                            expect_one_of=(422,))
check("page_size=101 → 422",      "get", "/admin/explain/attendance_list",
      params={"page_size":101},                                     expect_one_of=(422,))
check("valid attendance_list",    "get", "/admin/explain/attendance_list",
      params={"date_from":"2026-07-01","date_to":"2026-07-31"},     expect_one_of=(200,), timeout=60)
check("valid employee_monthly",   "get", "/admin/explain/employee_monthly",
      params={"emp_code": FUZZ_EMP,"month":"2026-07"},              expect_one_of=(200,), timeout=60)
check("valid department_summary", "get", "/admin/explain/department_summary",
      params={"month":"2026-07"},                                   expect_one_of=(200,), timeout=60)
check("valid late_leaderboard",   "get", "/admin/explain/late_leaderboard",
      params={"month":"2026-07"},                                   expect_one_of=(200,), timeout=60)
check("valid department_trend",   "get", "/admin/explain/department_trend",
      params={"department": FUZZ_DEPT,"from":"2026-07-01","to":"2026-07-31"}, expect_one_of=(200,), timeout=60)

# ── summary ───────────────────────────────────────────────────────────────────
print(f"\n{'='*55}")
print(f"  Fuzz results: {PASS} PASS  /  {FAIL} FAIL  /  {PASS+FAIL} total")
print(f"{'='*55}")
if FAIL:
    print("\nFailed cases:")
    # already printed inline
    sys.exit(1)
else:
    print("  All cases passed — no 500s on any bad input.")
    sys.exit(0)
