"""
seed_100k.py
============
Generates a realistic ~100 000 attendance_logs dataset for explain verification.

Usage:
    python seed_100k.py [--drop]

Env: MONGO_URI / MONGO_DB (falls back to localhost / attendance_test_100k if unset).

Design
------
* 500 employees across 5 departments (100 each), joined on varying dates.
* 200 working days of logs (Mon–Fri only, roughly 10 months).
* Each employee x each working day = 500 x 200 = 100 000 records.
* ~15 % of punch-ins are "late" to give the leaderboard something to rank.
* ~5 % are half-days; a handful of LEAVE/ABSENT records.
* Stores BSON dates for punch_in/punch_out (UTC-aware datetime objects).
"""
import os
import random
import sys
from datetime import datetime, date, timedelta, timezone

from dotenv import load_dotenv
from pymongo import MongoClient, ASCENDING, DESCENDING

load_dotenv()

MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB  = os.environ.get("MONGO_DB",  "attendance_test_100k")

DROP = "--drop" in sys.argv

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))

# ── parameters ──────────────────────────────────────────────────────────────
N_EMPLOYEES  = 500
DEPARTMENTS  = ["Engineering", "Sales", "Support", "HR", "Finance"]
SHIFT_START  = "09:30"   # HH:MM IST
SHIFT_END    = "18:30"   # HH:MM IST
SHIFT_START_H, SHIFT_START_M = 9, 30
SHIFT_END_H,   SHIFT_END_M   = 18, 30

# Working days window: 200 Mon–Fri days starting 2026-01-01
WINDOW_START = date(2026, 1, 1)
rng = random.Random(42)

def working_days_from(start: date, n: int):
    days, cur = [], start
    while len(days) < n:
        if cur.weekday() < 5:
            days.append(cur)
        cur += timedelta(days=1)
    return days

WORK_DAYS = working_days_from(WINDOW_START, 200)
WINDOW_END = WORK_DAYS[-1]


def ist_dt(d: date, h: int, m: int, s: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, h, m, s, tzinfo=IST).astimezone(UTC)


def build_employee(i: int) -> dict:
    dept = DEPARTMENTS[i % len(DEPARTMENTS)]
    # Spread join dates: first quarter employees join before window start
    join_offset = rng.randint(0, 30) if i < N_EMPLOYEES // 2 else rng.randint(0, 100)
    joined = (WINDOW_START - timedelta(days=join_offset)).isoformat()
    return {
        "emp_code":    f"EMP{i:05d}",
        "name":        f"Employee {i:05d}",
        "email":       f"emp{i:05d}@example.com",
        "department":  dept,
        "shift_start": SHIFT_START,
        "shift_end":   SHIFT_END,
        "joined_on":   joined,
        "created_at":  datetime(2025, 12, 1, 0, 0, 0, tzinfo=UTC),
    }


def build_log(emp_code: str, d: date) -> dict:
    roll = rng.random()
    # 3% ABSENT, 2% LEAVE, rest presence
    if roll < 0.03:
        return {
            "emp_code": emp_code, "date": d.isoformat(),
            "status": "ABSENT", "punch_in": None, "punch_out": None,
            "work_hours": None, "late_minutes": 0, "overtime_minutes": 0,
            "half_day": False, "history": [],
        }
    if roll < 0.05:
        return {
            "emp_code": emp_code, "date": d.isoformat(),
            "status": "LEAVE", "punch_in": None, "punch_out": None,
            "work_hours": None, "late_minutes": 0, "overtime_minutes": 0,
            "half_day": False, "history": [],
        }

    # presence: 15% late, 5% half-day
    is_late     = rng.random() < 0.15
    is_half_day = rng.random() < 0.05

    # punch_in: if late, add 11–60 extra minutes past shift start
    pi_extra_min = rng.randint(11, 60) if is_late else rng.randint(-5, 9)
    pi_h = SHIFT_START_H + (SHIFT_START_M + pi_extra_min) // 60
    pi_m = (SHIFT_START_M + pi_extra_min) % 60
    pi_dt = ist_dt(d, pi_h, pi_m)

    late_seconds = max(0, int((pi_dt - ist_dt(d, SHIFT_START_H, SHIFT_START_M)).total_seconds()))
    late_minutes = 0 if late_seconds <= 600 else late_seconds // 60

    if is_half_day:
        work_secs = rng.randint(3 * 3600, int(4.49 * 3600))
    else:
        work_secs = rng.randint(7 * 3600, 10 * 3600)

    po_dt = datetime.fromtimestamp(pi_dt.timestamp() + work_secs, tz=UTC)
    wh = round(work_secs / 3600, 2)

    # overtime: minutes past shift_end if >= 30
    shift_end_dt = ist_dt(d, SHIFT_END_H, SHIFT_END_M)
    ot_secs = int((po_dt - shift_end_dt).total_seconds())
    ot_minutes = 0 if ot_secs < 1800 else ot_secs // 60

    half_day = wh < 4.50
    status = rng.choice(["PRESENT", "PRESENT", "PRESENT", "WFH", "ON_DUTY"])

    return {
        "emp_code":         emp_code,
        "date":             d.isoformat(),
        "status":           status,
        "punch_in":         pi_dt,
        "punch_out":        po_dt,
        "work_hours":       wh,
        "late_minutes":     late_minutes,
        "overtime_minutes": ot_minutes,
        "half_day":         half_day,
        "history":          [],
    }


def main():
    client = MongoClient(MONGO_URI, tz_aware=True, tzinfo=UTC)
    db = client[MONGO_DB]
    emp_col = db["employees"]
    att_col = db["attendance_logs"]

    if DROP:
        print("Dropping existing collections…")
        emp_col.drop()
        att_col.drop()

    # ── create indexes ────────────────────────────────────────────────────
    print("Creating indexes…")
    emp_col.create_index([("emp_code", ASCENDING)], unique=True, name="emp_code_unique")
    emp_col.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="dept_joined_on")
    emp_col.create_index([("joined_on", ASCENDING)], name="joined_on_asc")
    att_col.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="emp_date_unique")
    att_col.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_desc_emp_asc")
    att_col.create_index([("date", ASCENDING), ("late_minutes", ASCENDING)], name="date_late_minutes")
    att_col.create_index([("date", ASCENDING), ("status", ASCENDING)], name="date_status")
    print("Indexes created.")

    # ── employees ─────────────────────────────────────────────────────────
    existing = emp_col.count_documents({})
    if existing >= N_EMPLOYEES:
        print(f"Employees already seeded ({existing}), skipping.")
        emps = list(emp_col.find({}, {"emp_code": 1, "_id": 0}))
    else:
        print(f"Inserting {N_EMPLOYEES} employees…")
        employees = [build_employee(i) for i in range(N_EMPLOYEES)]
        try:
            emp_col.insert_many(employees, ordered=False)
        except Exception as e:
            print(f"  (some duplicates skipped: {e})")
        emps = [{"emp_code": f"EMP{i:05d}"} for i in range(N_EMPLOYEES)]
        print(f"  done. Total: {emp_col.count_documents({})}")

    # ── attendance_logs ───────────────────────────────────────────────────
    existing_logs = att_col.count_documents({})
    if existing_logs >= 90_000:
        print(f"Attendance logs already seeded ({existing_logs}), skipping.")
    else:
        BATCH = 5_000
        batch, total = [], 0
        emp_codes = [e["emp_code"] for e in emps]
        print(f"Generating logs for {len(emp_codes)} employees × {len(WORK_DAYS)} days…")
        for ec in emp_codes:
            for d in WORK_DAYS:
                batch.append(build_log(ec, d))
                if len(batch) >= BATCH:
                    try:
                        att_col.insert_many(batch, ordered=False)
                    except Exception:
                        pass
                    total += len(batch)
                    batch = []
                    print(f"\r  {total:,} logs inserted…", end="", flush=True)
        if batch:
            try:
                att_col.insert_many(batch, ordered=False)
            except Exception:
                pass
            total += len(batch)
        print(f"\r  {total:,} logs inserted (final count: {att_col.count_documents({}):,})")

    client.close()
    print("Done.")


if __name__ == "__main__":
    main()
