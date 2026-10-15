"""
tests/test_api.py
=================
Standalone pytest suite for the Employee Attendance & Analytics API.

Requires a real local MongoDB (mongodb://localhost:27017).  Each test class
uses a uniquely-named database that is dropped on teardown, so tests are
fully isolated and can run in any order.

Run:
    pytest tests/test_api.py -v

The test process sets MONGO_URI and MONGO_DB itself, so no .env file is needed.
"""

import os
import threading
import time
import uuid
from datetime import datetime, timezone, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))


# ---------------------------------------------------------------------------
# Helpers: epoch-ms ↔ datetime
# ---------------------------------------------------------------------------

def to_ms(dt: datetime) -> int:
    """datetime → epoch milliseconds (truncated to whole seconds)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp()) * 1000


def ist(year, month, day, hour, minute, second=0) -> datetime:
    """Build a UTC-aware datetime from an IST wall-clock time."""
    return datetime(year, month, day, hour, minute, second, tzinfo=IST).astimezone(UTC)


def ist_ms(year, month, day, hour, minute, second=0) -> int:
    return to_ms(ist(year, month, day, hour, minute, second))


# ---------------------------------------------------------------------------
# Fixture: isolated app + TestClient per test class
# ---------------------------------------------------------------------------

@pytest.fixture(scope="class")
def client(request):
    """
    Spin up a fresh FastAPI TestClient against a randomly-named MongoDB database.
    The database is dropped after the test class finishes.
    """
    db_name = f"test_{uuid.uuid4().hex[:12]}"
    os.environ["MONGO_URI"] = "mongodb://localhost:27017"
    os.environ["MONGO_DB"]  = db_name

    # Import app fresh for each class (re-evaluates module-level DB connection).
    # We achieve isolation by patching the module's db reference.
    import importlib
    import app.main as main_mod
    importlib.reload(main_mod)

    # Point the module's collection handles at the new DB.
    from pymongo import MongoClient
    _client = MongoClient("mongodb://localhost:27017", tz_aware=True, tzinfo=UTC)
    _db     = _client[db_name]
    main_mod.client        = _client
    main_mod.db            = _db
    main_mod.employees_col = _db["employees"]
    main_mod.attendance_col = _db["attendance_logs"]

    tc = TestClient(main_mod.app, raise_server_exceptions=True)

    # Manually trigger lifespan (index creation).
    with tc:
        yield tc

    _client[db_name].client.drop_database(db_name)
    _client.close()


# ===========================================================================
# Helpers shared by tests
# ===========================================================================

def create_emp(client, emp_code, dept="Engineering",
               shift_start="09:30", shift_end="18:30",
               joined_on="2026-07-01", name=None, email=None):
    name  = name  or f"User {emp_code}"
    email = email or f"{emp_code.lower()}@test.com"
    r = client.post("/employees", json={
        "emp_code":    emp_code,
        "name":        name,
        "email":       email,
        "department":  dept,
        "shift_start": shift_start,
        "shift_end":   shift_end,
        "joined_on":   joined_on,
    })
    assert r.status_code == 201, r.text
    return r.json()


def punch(client, emp_code, punched_at_ms, status="PRESENT"):
    return client.post("/attendance/punch-in", json={
        "emp_code":   emp_code,
        "punched_at": punched_at_ms,
        "status":     status,
    })


def pout(client, emp_code, punched_at_ms):
    return client.post("/attendance/punch-out", json={
        "emp_code":   emp_code,
        "punched_at": punched_at_ms,
    })


def seed_log(col, emp_code, date_str, status,
             punch_in_dt=None, punch_out_dt=None,
             work_hours=None, late_minutes=0,
             overtime_minutes=0, half_day=False, history=None):
    """Insert an attendance log directly (simulates grader-seeded data)."""
    col.insert_one({
        "emp_code":         emp_code,
        "date":             date_str,
        "status":           status,
        "punch_in":         punch_in_dt,
        "punch_out":        punch_out_dt,
        "work_hours":       work_hours,
        "late_minutes":     late_minutes,
        "overtime_minutes": overtime_minutes,
        "half_day":         half_day,
        "history":          history or [],
    })


# ===========================================================================
# T1 – /health
# ===========================================================================
class TestHealth:
    def test_health_ok(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


# ===========================================================================
# T2 – /employees  (create, list, duplicate, validation)
# ===========================================================================
class TestEmployees:
    def test_create_returns_201(self, client):
        r = client.post("/employees", json={
            "emp_code":   "EMP0001",
            "name":       "Alice",
            "email":      "alice@ex.com",
            "department": "Eng",
            "joined_on":  "2026-01-01",
        })
        assert r.status_code == 201
        body = r.json()
        assert body["emp_code"] == "EMP0001"
        assert "_id" not in body
        assert isinstance(body["created_at"], int)
        assert body["created_at"] > 100_000_000_000

    def test_duplicate_emp_code_409(self, client):
        create_emp(client, "EMP0002")
        r = client.post("/employees", json={
            "emp_code":   "EMP0002",
            "name":       "Bob",
            "email":      "bob@ex.com",
            "department": "Eng",
            "joined_on":  "2026-01-01",
        })
        assert r.status_code == 409

    def test_invalid_emp_code_422(self, client):
        r = client.post("/employees", json={
            "emp_code":   "BADCODE",
            "name":       "X",
            "email":      "x@x.com",
            "department": "X",
            "joined_on":  "2026-01-01",
        })
        assert r.status_code == 422

    def test_shift_equal_422(self, client):
        r = client.post("/employees", json={
            "emp_code":    "EMP0099",
            "name":        "Y",
            "email":       "y@y.com",
            "department":  "Y",
            "shift_start": "09:30",
            "shift_end":   "09:30",
            "joined_on":   "2026-01-01",
        })
        assert r.status_code == 422

    def test_list_sorted_by_emp_code(self, client):
        # EMP0001/EMP0002 already created in earlier tests within this class.
        # Create a few more in reverse order.
        create_emp(client, "EMP0010")
        create_emp(client, "EMP0005")
        r = client.get("/employees")
        assert r.status_code == 200
        body = r.json()
        codes = [i["emp_code"] for i in body["items"]]
        assert codes == sorted(codes)

    def test_list_pagination_skip(self, client):
        # page=1 should NOT skip any items.
        r1 = client.get("/employees?page=1&page_size=2")
        r2 = client.get("/employees?page=2&page_size=2")
        assert r1.status_code == r2.status_code == 200
        items1 = r1.json()["items"]
        items2 = r2.json()["items"]
        # No overlap.
        codes1 = {i["emp_code"] for i in items1}
        codes2 = {i["emp_code"] for i in items2}
        assert codes1.isdisjoint(codes2)

    def test_list_total_respects_filter(self, client):
        create_emp(client, "EMP0020", dept="Sales")
        r_all   = client.get("/employees")
        r_sales = client.get("/employees?department=Sales")
        assert r_sales.json()["total"] < r_all.json()["total"]
        assert r_sales.json()["total"] >= 1

    def test_page_size_max_422(self, client):
        r = client.get("/employees?page_size=101")
        assert r.status_code == 422


# ===========================================================================
# T3 – /attendance/punch-in  (validation, late rule, overnight)
# ===========================================================================
class TestPunchIn:
    def test_basic_punch_in(self, client):
        create_emp(client, "EMP1001")
        r = punch(client, "EMP1001", ist_ms(2026, 7, 6, 9, 28))
        assert r.status_code == 201
        body = r.json()
        assert body["date"] == "2026-07-06"
        assert body["late_minutes"] == 0
        assert body["punch_out"] is None
        assert body["half_day"] is False
        assert "_id" not in body

    def test_404_unknown_employee(self, client):
        r = punch(client, "EMP9999", ist_ms(2026, 7, 6, 9, 0))
        assert r.status_code == 404

    def test_409_double_punch_in(self, client):
        create_emp(client, "EMP1002")
        r1 = punch(client, "EMP1002", ist_ms(2026, 7, 7, 9, 0))
        assert r1.status_code == 201
        r2 = punch(client, "EMP1002", ist_ms(2026, 7, 7, 9, 30))
        assert r2.status_code == 409

    def test_absent_status_rejected_422(self, client):
        create_emp(client, "EMP1003")
        r = client.post("/attendance/punch-in", json={
            "emp_code":   "EMP1003",
            "punched_at": ist_ms(2026, 7, 6, 9, 0),
            "status":     "ABSENT",
        })
        assert r.status_code == 422

    # ── R2 Late threshold cases ──────────────────────────────────────────

    def test_exactly_grace_not_late(self, client):
        """09:40:00 IST (600 s after 09:30) → late_minutes = 0."""
        create_emp(client, "EMP1004")
        r = punch(client, "EMP1004", ist_ms(2026, 7, 6, 9, 40, 0))
        assert r.status_code == 201
        assert r.json()["late_minutes"] == 0

    def test_one_second_past_grace_is_late(self, client):
        """09:40:01 IST (601 s after 09:30) → late_minutes = 10."""
        create_emp(client, "EMP1005")
        r = punch(client, "EMP1005", ist_ms(2026, 7, 6, 9, 40, 1))
        assert r.status_code == 201
        assert r.json()["late_minutes"] == 10

    def test_late_minutes_is_from_shift_start(self, client):
        """10:15:59 IST → 45 minutes (floor since 09:30, not since grace end)."""
        create_emp(client, "EMP1006")
        r = punch(client, "EMP1006", ist_ms(2026, 7, 6, 10, 15, 59))
        assert r.status_code == 201
        assert r.json()["late_minutes"] == 45

    # ── R1 Overnight shift ───────────────────────────────────────────────

    def test_overnight_punch_in_after_shift_start(self, client):
        """22:05 IST → attendance date = 2026-07-06 (shift started that evening)."""
        create_emp(client, "EMP1007", shift_start="22:00", shift_end="06:00")
        r = punch(client, "EMP1007", ist_ms(2026, 7, 6, 22, 5))
        assert r.status_code == 201
        assert r.json()["date"] == "2026-07-06"

    def test_overnight_punch_in_before_shift_end_is_previous_day(self, client):
        """05:30 IST on July 7 → belongs to shift that started July 6."""
        create_emp(client, "EMP1008", shift_start="22:00", shift_end="06:00")
        # First punch the start (July 6 22:05) to create the record.
        punch(client, "EMP1008", ist_ms(2026, 7, 6, 22, 5))
        # Now try punch-in at 05:30 July 7 — should be rejected as a duplicate
        # for July 6 (because the attendance date resolves to July 6).
        r2 = punch(client, "EMP1008", ist_ms(2026, 7, 7, 5, 30))
        assert r2.status_code == 409

    def test_overnight_date_resolution_standalone(self, client):
        """
        A fresh overnight-shift employee punches in at 05:30 July 7 with NO
        prior record.  The attendance date must resolve to July 6 (previous day).
        """
        create_emp(client, "EMP1009", shift_start="22:00", shift_end="06:00")
        r = punch(client, "EMP1009", ist_ms(2026, 7, 7, 5, 30))
        assert r.status_code == 201
        assert r.json()["date"] == "2026-07-06"

    # ── Timestamp validation ─────────────────────────────────────────────

    def test_float_timestamp_rejected_422(self, client):
        create_emp(client, "EMP1010")
        r = client.post("/attendance/punch-in", json={
            "emp_code":   "EMP1010",
            "punched_at": 1783312500.0,   # float
        })
        assert r.status_code == 422

    def test_seconds_scale_rejected_422(self, client):
        create_emp(client, "EMP1011")
        r = client.post("/attendance/punch-in", json={
            "emp_code":   "EMP1011",
            "punched_at": 1783312500,     # looks like seconds (< 1e11)
        })
        assert r.status_code == 422

    def test_string_timestamp_rejected_422(self, client):
        create_emp(client, "EMP1012")
        r = client.post("/attendance/punch-in", json={
            "emp_code":   "EMP1012",
            "punched_at": "2026-07-06T09:28:00",
        })
        assert r.status_code == 422


# ===========================================================================
# T4 – /attendance/punch-out  (work_hours, overtime, half_day, concurrency)
# ===========================================================================
class TestPunchOut:
    def test_basic_punch_out_computes_fields(self, client):
        create_emp(client, "EMP2001")
        punch(client, "EMP2001", ist_ms(2026, 7, 7, 9, 28))
        r = pout(client, "EMP2001", ist_ms(2026, 7, 7, 18, 35))
        assert r.status_code == 200
        body = r.json()
        # 9h 7m = 32820 s / 3600 = 9.1166… → 9.12 HALF-UP
        assert body["work_hours"] == 9.12
        # shift_end 18:30, punch_out 18:35 → 5 min < 30 min threshold → 0
        assert body["overtime_minutes"] == 0
        assert body["half_day"] is False

    # ── R3 Overtime threshold ─────────────────────────────────────────────

    def test_overtime_29min_is_zero(self, client):
        """29 minutes past shift_end → overtime_minutes = 0."""
        create_emp(client, "EMP2002")
        punch(client, "EMP2002", ist_ms(2026, 7, 7, 9, 0))
        r = pout(client, "EMP2002", ist_ms(2026, 7, 7, 18, 59))
        assert r.status_code == 200
        assert r.json()["overtime_minutes"] == 0

    def test_overtime_exactly_30min(self, client):
        """Exactly 30 minutes past shift_end → overtime_minutes = 30."""
        create_emp(client, "EMP2003")
        punch(client, "EMP2003", ist_ms(2026, 7, 7, 9, 0))
        r = pout(client, "EMP2003", ist_ms(2026, 7, 7, 19, 0))
        assert r.status_code == 200
        assert r.json()["overtime_minutes"] == 30

    # ── R4 work_hours HALF-UP rounding ───────────────────────────────────

    def test_work_hours_half_up_4495(self, client):
        """
        4.495 h = 16182 s.  Decimal(16182)/3600 = 4.4950000…
        HALF-UP → 4.50.  Python round() → 4.49 (banker's). This test would
        fail if the starter's round() were still in place.
        """
        create_emp(client, "EMP2004")
        # 16182 seconds of work: punch in at 09:00:00, out at 13:29:42 IST
        punch(client, "EMP2004", ist_ms(2026, 7, 8, 9, 0, 0))
        r = pout(client, "EMP2004", ist_ms(2026, 7, 8, 13, 29, 42))
        assert r.status_code == 200
        assert r.json()["work_hours"] == 4.50

    def test_work_hours_below_450_is_half_day(self, client):
        """R5: work_hours < 4.50 → half_day = True."""
        create_emp(client, "EMP2005")
        # 3.5 h = 12600 s
        punch(client, "EMP2005", ist_ms(2026, 7, 8, 9, 0))
        r = pout(client, "EMP2005", ist_ms(2026, 7, 8, 12, 30))
        assert r.status_code == 200
        assert r.json()["work_hours"] == 3.50
        assert r.json()["half_day"] is True

    def test_work_hours_exactly_450_not_half_day(self, client):
        """R5: work_hours = 4.50 → half_day = False (not strictly less than)."""
        create_emp(client, "EMP2006")
        # 4.50 h = 16200 s → punch in 09:00, out 13:30
        punch(client, "EMP2006", ist_ms(2026, 7, 8, 9, 0, 0))
        r = pout(client, "EMP2006", ist_ms(2026, 7, 8, 13, 30, 0))
        assert r.status_code == 200
        assert r.json()["work_hours"] == 4.50
        assert r.json()["half_day"] is False

    def test_404_no_open_record(self, client):
        create_emp(client, "EMP2007")
        r = pout(client, "EMP2007", ist_ms(2026, 7, 8, 17, 0))
        assert r.status_code == 404

    def test_409_already_punched_out(self, client):
        create_emp(client, "EMP2008")
        punch(client, "EMP2008", ist_ms(2026, 7, 8, 9, 0))
        pout(client, "EMP2008", ist_ms(2026, 7, 8, 18, 0))
        r = pout(client, "EMP2008", ist_ms(2026, 7, 8, 18, 30))
        assert r.status_code == 409

    def test_422_punch_out_before_punch_in(self, client):
        create_emp(client, "EMP2009")
        punch(client, "EMP2009", ist_ms(2026, 7, 8, 9, 0))
        r = pout(client, "EMP2009", ist_ms(2026, 7, 8, 8, 59))
        assert r.status_code == 422

    def test_422_punch_out_over_24h(self, client):
        create_emp(client, "EMP2010")
        punch(client, "EMP2010", ist_ms(2026, 7, 8, 9, 0))
        # 25 hours later
        r = pout(client, "EMP2010", ist_ms(2026, 7, 9, 10, 0))
        assert r.status_code == 422

    def test_overnight_shift_punch_out(self, client):
        """shift 22:00-06:00: punch-in July 6 22:05 IST, out July 7 06:40 IST."""
        create_emp(client, "EMP2011", shift_start="22:00", shift_end="06:00")
        punch(client, "EMP2011", ist_ms(2026, 7, 6, 22, 5))
        r = pout(client, "EMP2011", ist_ms(2026, 7, 7, 6, 40))
        assert r.status_code == 200
        body = r.json()
        assert body["date"] == "2026-07-06"
        # 06:40 is 40 min past 06:00 shift_end → overtime = 40
        assert body["overtime_minutes"] == 40
        assert body["half_day"] is False

    # ── Concurrent punch-out race ─────────────────────────────────────────

    def test_concurrent_punch_out_exactly_one_succeeds(self, client):
        """
        Two threads punch-out simultaneously.  Exactly one must get 200;
        the other must get 409.
        """
        create_emp(client, "EMP2099")
        punch(client, "EMP2099", ist_ms(2026, 7, 10, 9, 0))

        results = []
        barrier = threading.Barrier(2)

        def do_pout():
            barrier.wait()   # release both threads at the same instant
            r = pout(client, "EMP2099", ist_ms(2026, 7, 10, 18, 0))
            results.append(r.status_code)

        t1 = threading.Thread(target=do_pout)
        t2 = threading.Thread(target=do_pout)
        t1.start(); t2.start()
        t1.join();  t2.join()

        assert sorted(results) == [200, 409], f"Expected [200,409], got {results}"


# ===========================================================================
# T5 – /attendance/punch-in concurrency race
# ===========================================================================
class TestPunchInConcurrency:
    def test_concurrent_punch_in_exactly_one_succeeds(self, client):
        create_emp(client, "EMP3001")
        results = []
        barrier = threading.Barrier(2)

        def do_punch():
            barrier.wait()
            r = punch(client, "EMP3001", ist_ms(2026, 7, 6, 9, 0))
            results.append(r.status_code)

        t1 = threading.Thread(target=do_punch)
        t2 = threading.Thread(target=do_punch)
        t1.start(); t2.start()
        t1.join();  t2.join()

        assert sorted(results) == [201, 409], f"Expected [201,409], got {results}"


# ===========================================================================
# T6 – /attendance list
# ===========================================================================
class TestListAttendance:
    def test_sort_date_desc_emp_asc(self, client):
        create_emp(client, "EMP4001")
        create_emp(client, "EMP4002")
        punch(client, "EMP4001", ist_ms(2026, 7, 6, 9, 0))
        punch(client, "EMP4002", ist_ms(2026, 7, 6, 9, 5))
        punch(client, "EMP4001", ist_ms(2026, 7, 7, 9, 0))

        r = client.get("/attendance")
        items = r.json()["items"]
        dates = [i["date"] for i in items]
        assert dates == sorted(dates, reverse=True)

    def test_total_reflects_filter(self, client):
        create_emp(client, "EMP4003")
        create_emp(client, "EMP4004")
        punch(client, "EMP4003", ist_ms(2026, 7, 8, 9, 0))
        punch(client, "EMP4004", ist_ms(2026, 7, 8, 9, 0))
        r_all = client.get("/attendance?date_from=2026-07-08&date_to=2026-07-08")
        r_one = client.get(f"/attendance?emp_code=EMP4003")
        assert r_all.json()["total"] >= 2
        assert r_one.json()["total"] == 1

    def test_date_from_after_date_to_422(self, client):
        r = client.get("/attendance?date_from=2026-07-10&date_to=2026-07-01")
        assert r.status_code == 422

    def test_no_id_in_response(self, client):
        create_emp(client, "EMP4005")
        punch(client, "EMP4005", ist_ms(2026, 7, 9, 9, 0))
        r = client.get("/attendance?emp_code=EMP4005")
        for item in r.json()["items"]:
            assert "_id" not in item
            assert "id"  not in item

    def test_punch_times_are_epoch_ms(self, client):
        create_emp(client, "EMP4006")
        punch(client, "EMP4006", ist_ms(2026, 7, 9, 9, 0))
        r = client.get("/attendance?emp_code=EMP4006")
        item = r.json()["items"][0]
        assert isinstance(item["punch_in"], int)
        assert item["punch_in"] > 100_000_000_000


# ===========================================================================
# T7 – PATCH /attendance/{emp_code}/{date}  (regularize)
# ===========================================================================
class TestRegularize:
    def test_basic_regularize_punch_in(self, client):
        """Correct a late punch-in → late_minutes drops to 0."""
        import app.main as m
        create_emp(client, "EMP5001")
        punch(client, "EMP5001", ist_ms(2026, 7, 7, 10, 5, 0))   # 35 min late

        r = client.patch("/attendance/EMP5001/2026-07-07", json={
            "punch_in":       ist_ms(2026, 7, 7, 9, 28, 0),
            "reason":         "biometric glitch corrected",
            "regularized_by": "hr.admin",
        })
        assert r.status_code == 200
        body = r.json()
        assert body["late_minutes"] == 0
        assert len(body["history"]) == 1
        entry = body["history"][0]
        assert "punch_in"     in entry["changes"]
        assert "late_minutes" in entry["changes"]
        assert entry["changes"]["late_minutes"]["from"] == 35
        assert entry["changes"]["late_minutes"]["to"]   == 0

    def test_history_changes_contain_only_changed_fields(self, client):
        """A correction that only changes status must not include punch times."""
        import app.main as m
        create_emp(client, "EMP5002")
        # Insert an ABSENT record directly (seeded data).
        m.attendance_col.insert_one({
            "emp_code": "EMP5002", "date": "2026-07-07",
            "status": "ABSENT", "punch_in": None, "punch_out": None,
            "work_hours": None, "late_minutes": 0,
            "overtime_minutes": 0, "half_day": False, "history": [],
        })
        r = client.patch("/attendance/EMP5002/2026-07-07", json={
            "status":         "LEAVE",
            "reason":         "changing to approved leave",
            "regularized_by": "hr",
        })
        assert r.status_code == 200
        changes = r.json()["history"][0]["changes"]
        assert "status" in changes
        assert "punch_in"  not in changes
        assert "punch_out" not in changes

    def test_no_op_regularize_422(self, client):
        import app.main as m
        create_emp(client, "EMP5003")
        m.attendance_col.insert_one({
            "emp_code": "EMP5003", "date": "2026-07-07",
            "status": "ABSENT", "punch_in": None, "punch_out": None,
            "work_hours": None, "late_minutes": 0,
            "overtime_minutes": 0, "half_day": False, "history": [],
        })
        # Re-sending the same status → nothing changes → 422
        r = client.patch("/attendance/EMP5003/2026-07-07", json={
            "status":         "ABSENT",
            "reason":         "no change here",
            "regularized_by": "hr",
        })
        assert r.status_code == 422

    def test_absent_with_punch_in_422(self, client):
        create_emp(client, "EMP5004")
        punch(client, "EMP5004", ist_ms(2026, 7, 7, 9, 0))
        r = client.patch("/attendance/EMP5004/2026-07-07", json={
            "status":         "ABSENT",
            "punch_in":       ist_ms(2026, 7, 7, 9, 0),
            "reason":         "bad request",
            "regularized_by": "hr",
        })
        assert r.status_code == 422

    def test_404_no_record(self, client):
        create_emp(client, "EMP5005")
        r = client.patch("/attendance/EMP5005/2026-07-07", json={
            "reason":         "nothing here",
            "regularized_by": "hr",
        })
        assert r.status_code == 404

    def test_history_punch_times_are_epoch_ms(self, client):
        """punch_in/punch_out in history.changes must be epoch ms, not ISO strings."""
        create_emp(client, "EMP5006")
        punch(client, "EMP5006", ist_ms(2026, 7, 7, 10, 5, 0))
        r = client.patch("/attendance/EMP5006/2026-07-07", json={
            "punch_in":       ist_ms(2026, 7, 7, 9, 28, 0),
            "reason":         "fix punch time",
            "regularized_by": "hr",
        })
        assert r.status_code == 200
        pi_change = r.json()["history"][0]["changes"]["punch_in"]
        assert isinstance(pi_change["from"], int)
        assert isinstance(pi_change["to"],   int)

    def test_concurrent_patch_409(self, client):
        """Two simultaneous PATCHes: one succeeds, the other gets 409."""
        import app.main as m
        create_emp(client, "EMP5099")
        punch(client, "EMP5099", ist_ms(2026, 7, 7, 10, 5))

        results = []
        barrier = threading.Barrier(2)

        def do_patch():
            barrier.wait()
            r = client.patch("/attendance/EMP5099/2026-07-07", json={
                "punch_in":       ist_ms(2026, 7, 7, 9, 28),
                "reason":         "concurrent correction attempt",
                "regularized_by": "hr",
            })
            results.append(r.status_code)

        t1 = threading.Thread(target=do_patch)
        t2 = threading.Thread(target=do_patch)
        t1.start(); t2.start()
        t1.join();  t2.join()

        assert sorted(results) == [200, 409], f"Expected [200,409], got {results}"


# ===========================================================================
# T8 – GET /analytics/employees/{emp_code}/monthly
# ===========================================================================
class TestEmployeeMonthly:
    def _setup(self, client, m):
        """
        EMP6001: joined 2026-07-01, standard 09:30-18:30 shift.
        Logs for July 2026:
          Mon 2026-07-06: PRESENT, late, work_hours 9.08, ot 40
          Tue 2026-07-07: PRESENT, on time, work_hours 9.03
          Wed 2026-07-08: LEAVE
          Sat 2026-07-11: PRESENT (weekend — should NOT count for present_days)
          Mon 2026-07-13: WFH, half_day
        """
        create_emp(client, "EMP6001")
        col = m.attendance_col
        seed_log(col, "EMP6001", "2026-07-06", "PRESENT",
                 punch_in_dt=ist(2026,7,6,10,5), punch_out_dt=ist(2026,7,6,13,40),
                 work_hours=9.08, late_minutes=35, overtime_minutes=40)
        seed_log(col, "EMP6001", "2026-07-07", "PRESENT",
                 punch_in_dt=ist(2026,7,7,9,28), punch_out_dt=ist(2026,7,7,18,0),
                 work_hours=9.03, late_minutes=0, overtime_minutes=0)
        seed_log(col, "EMP6001", "2026-07-08", "LEAVE")
        seed_log(col, "EMP6001", "2026-07-11", "PRESENT",  # Saturday
                 punch_in_dt=ist(2026,7,11,9,28), punch_out_dt=ist(2026,7,11,18,0),
                 work_hours=9.03, late_minutes=0, overtime_minutes=0)
        seed_log(col, "EMP6001", "2026-07-13", "WFH",
                 punch_in_dt=ist(2026,7,13,9,28), punch_out_dt=ist(2026,7,13,13,0),
                 work_hours=3.53, late_minutes=0, overtime_minutes=0, half_day=True)

    def test_present_days_excludes_weekend(self, client):
        import app.main as m
        self._setup(client, m)
        r = client.get("/analytics/employees/EMP6001/monthly?month=2026-07")
        assert r.status_code == 200
        body = r.json()
        # Mon 07-06: 1.0, Tue 07-07: 1.0, Sat 07-11: excluded, Mon 07-13: 0.5 (half_day)
        assert body["present_days"] == 2.5

    def test_leave_days(self, client):
        r = client.get("/analytics/employees/EMP6001/monthly?month=2026-07")
        assert r.json()["leave_days"] == 1

    def test_late_count_and_total_late_minutes(self, client):
        r = client.get("/analytics/employees/EMP6001/monthly?month=2026-07")
        body = r.json()
        assert body["late_count"]         == 1
        assert body["total_late_minutes"] == 35

    def test_total_overtime_minutes(self, client):
        r = client.get("/analytics/employees/EMP6001/monthly?month=2026-07")
        # Overtime: 40 (July 6) + 0 (July 7) + 0 (Sat July 11) + 0 (July 13) = 40
        # Weekend records still add to overtime totals (R7).
        assert r.json()["total_overtime_minutes"] == 40

    def test_working_days_mid_month_joiner(self, client):
        """Employee who joins mid-month has fewer working days counted (R7)."""
        import app.main as m
        create_emp(client, "EMP6002", joined_on="2026-07-14")
        r = client.get("/analytics/employees/EMP6002/monthly?month=2026-07")
        assert r.status_code == 200
        body = r.json()
        # July 14 is a Tuesday; Mon-Fri from 14th to 31st = 14 working days.
        assert body["working_days"] == 14

    def test_attendance_pct_rounding(self, client):
        r = client.get("/analytics/employees/EMP6001/monthly?month=2026-07")
        body = r.json()
        # present_days=2.5, working_days for EMP6001 joined 2026-07-01: 23 Mon-Fri days in July
        # pct = 2.5/23*100 = 10.869… → 10.87 HALF-UP
        assert body["attendance_pct"] == pytest.approx(10.87, abs=0.01)

    def test_404_unknown_employee(self, client):
        r = client.get("/analytics/employees/EMP9999/monthly?month=2026-07")
        assert r.status_code == 404

    def test_no_logs_employee_zero_present_days(self, client):
        create_emp(client, "EMP6003")
        r = client.get("/analytics/employees/EMP6003/monthly?month=2026-07")
        assert r.status_code == 200
        body = r.json()
        assert body["present_days"]  == 0.0
        assert body["leave_days"]    == 0
        assert body["late_count"]    == 0
        assert body["attendance_pct"] == 0.0


# ===========================================================================
# T9 – GET /analytics/departments/summary
# ===========================================================================
class TestDepartmentSummary:
    def _setup(self, client, m):
        """
        Dept "Alpha":
          EMP7001 joined 2026-07-01 — has logs
          EMP7002 joined 2026-07-01 — NO logs (zero-log employee)
          EMP7003 joined 2026-08-01 — NOT in headcount for July (joined later)
        Dept "Beta":
          EMP7004 joined 2026-07-01 — has logs
        """
        create_emp(client, "EMP7001", dept="Alpha")
        create_emp(client, "EMP7002", dept="Alpha")
        create_emp(client, "EMP7003", dept="Alpha", joined_on="2026-08-01")
        create_emp(client, "EMP7004", dept="Beta")
        col = m.attendance_col
        # EMP7001: Mon 2026-07-06 PRESENT, late
        seed_log(col, "EMP7001", "2026-07-06", "PRESENT",
                 punch_in_dt=ist(2026,7,6,10,5), punch_out_dt=ist(2026,7,6,18,35),
                 work_hours=8.5, late_minutes=35, overtime_minutes=0)
        # EMP7001: Tue 2026-07-07 WFH half_day
        seed_log(col, "EMP7001", "2026-07-07", "WFH",
                 punch_in_dt=ist(2026,7,7,9,0), punch_out_dt=ist(2026,7,7,13,0),
                 work_hours=4.0, late_minutes=0, overtime_minutes=0, half_day=True)
        # EMP7004: Beta employee
        seed_log(col, "EMP7004", "2026-07-06", "PRESENT",
                 punch_in_dt=ist(2026,7,6,9,0), punch_out_dt=ist(2026,7,6,18,30),
                 work_hours=9.5, late_minutes=0, overtime_minutes=0)

    def test_headcount_includes_zero_log_employee(self, client):
        import app.main as m
        self._setup(client, m)
        r = client.get("/analytics/departments/summary?month=2026-07")
        assert r.status_code == 200
        items = {i["department"]: i for i in r.json()["items"]}
        # EMP7001 + EMP7002 = 2; EMP7003 joined August → excluded.
        assert items["Alpha"]["headcount"] == 2

    def test_headcount_excludes_future_joiner(self, client):
        items = {
            i["department"]: i
            for i in client.get("/analytics/departments/summary?month=2026-07").json()["items"]
        }
        assert items["Alpha"]["headcount"] == 2   # EMP7003 joined Aug → excluded

    def test_present_days_half_day_weekend(self, client):
        items = {
            i["department"]: i
            for i in client.get("/analytics/departments/summary?month=2026-07").json()["items"]
        }
        # EMP7001: Mon 07-06 → 1.0, Tue 07-07 half_day → 0.5
        assert items["Alpha"]["present_days"] == 1.5

    def test_avg_work_hours_over_records_not_employees(self, client):
        """avg_work_hours is mean over records, not per-employee average (spec)."""
        items = {
            i["department"]: i
            for i in client.get("/analytics/departments/summary?month=2026-07").json()["items"]
        }
        # Alpha records with work_hours: 8.5 and 4.0 → mean = 6.25
        assert items["Alpha"]["avg_work_hours"] == pytest.approx(6.25, abs=0.01)

    def test_dept_filter(self, client):
        r = client.get("/analytics/departments/summary?month=2026-07&department=Beta")
        items = r.json()["items"]
        assert len(items) == 1
        assert items[0]["department"] == "Beta"

    def test_sorted_by_department(self, client):
        r = client.get("/analytics/departments/summary?month=2026-07")
        depts = [i["department"] for i in r.json()["items"]]
        assert depts == sorted(depts)


# ===========================================================================
# T10 – GET /analytics/leaderboard/late
# ===========================================================================
class TestLeaderboard:
    def _setup(self, client, m):
        """
        July 2026.  Late minutes:
          EMP8001: 35 + 20 = 55  → rank 1
          EMP8002: 55             → rank 1  (tie with EMP8001)
          EMP8003: 30             → rank 3
          EMP8004:  0             → not in leaderboard
        """
        create_emp(client, "EMP8001", dept="Eng")
        create_emp(client, "EMP8002", dept="Eng")
        create_emp(client, "EMP8003", dept="Eng")
        create_emp(client, "EMP8004", dept="Eng")
        col = m.attendance_col
        seed_log(col, "EMP8001", "2026-07-06", "PRESENT",
                 punch_in_dt=ist(2026,7,6,10,5), late_minutes=35)
        seed_log(col, "EMP8001", "2026-07-07", "PRESENT",
                 punch_in_dt=ist(2026,7,7,9,50), late_minutes=20)
        seed_log(col, "EMP8002", "2026-07-06", "PRESENT",
                 punch_in_dt=ist(2026,7,6,10,25), late_minutes=55)
        seed_log(col, "EMP8003", "2026-07-06", "PRESENT",
                 punch_in_dt=ist(2026,7,6,10,0), late_minutes=30)
        seed_log(col, "EMP8004", "2026-07-06", "PRESENT",
                 punch_in_dt=ist(2026,7,6,9,28), late_minutes=0)

    def test_tie_at_rank_1_both_returned(self, client):
        import app.main as m
        self._setup(client, m)
        r = client.get("/analytics/leaderboard/late?month=2026-07&limit=1")
        assert r.status_code == 200
        items = r.json()["items"]
        # Both EMP8001 and EMP8002 have 55 total late minutes → both rank 1.
        # limit=1 but both tied at rank 1 → both returned.
        ranks = [i["rank"] for i in items]
        assert all(rk == 1 for rk in ranks)
        codes = {i["emp_code"] for i in items}
        assert "EMP8001" in codes
        assert "EMP8002" in codes

    def test_rank_3_after_tie(self, client):
        r = client.get("/analytics/leaderboard/late?month=2026-07&limit=3")
        items = r.json()["items"]
        by_code = {i["emp_code"]: i for i in items}
        assert by_code["EMP8003"]["rank"] == 3   # skips rank 2

    def test_zero_late_employee_excluded(self, client):
        r = client.get("/analytics/leaderboard/late?month=2026-07")
        codes = {i["emp_code"] for i in r.json()["items"]}
        assert "EMP8004" not in codes

    def test_secondary_sort_by_emp_code(self, client):
        """Tied employees are sub-sorted by emp_code ascending."""
        r = client.get("/analytics/leaderboard/late?month=2026-07&limit=2")
        codes = [i["emp_code"] for i in r.json()["items"] if i["rank"] == 1]
        assert codes == sorted(codes)

    def test_department_filter(self, client):
        """department filter scopes the ranking to that department."""
        import app.main as m
        create_emp(client, "EMP8010", dept="Sales")
        m.attendance_col.insert_one({
            "emp_code": "EMP8010", "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": ist(2026,7,6,10,5), "punch_out": None,
            "work_hours": None, "late_minutes": 100,
            "overtime_minutes": 0, "half_day": False, "history": [],
        })
        r = client.get("/analytics/leaderboard/late?month=2026-07&department=Sales&limit=1")
        items = r.json()["items"]
        assert len(items) == 1
        assert items[0]["emp_code"] == "EMP8010"
        assert items[0]["rank"]     == 1


# ===========================================================================
# T11 – GET /analytics/departments/{department}/trend
# ===========================================================================
class TestDepartmentTrend:
    def _setup(self, client, m):
        """
        Dept "Gamma": EMP9001 joined 2026-07-01.
        Logs for a 5-day window Mon–Fri 2026-07-07 to 2026-07-11:
          Tue 2026-07-07: PRESENT (full day)
          Wed 2026-07-08: — no log
          Thu 2026-07-09: PRESENT (half_day)
          Fri 2026-07-10: ABSENT (no punch)
          Sat 2026-07-11: PRESENT (weekend)
        """
        create_emp(client, "EMP9001", dept="Gamma")
        col = m.attendance_col
        seed_log(col, "EMP9001", "2026-07-07", "PRESENT",
                 punch_in_dt=ist(2026,7,7,9,0), punch_out_dt=ist(2026,7,7,18,0),
                 work_hours=9.0, late_minutes=0)
        # 2026-07-08: intentionally no log (gap-fill test)
        seed_log(col, "EMP9001", "2026-07-09", "PRESENT",
                 punch_in_dt=ist(2026,7,9,9,0), punch_out_dt=ist(2026,7,9,13,0),
                 work_hours=4.0, late_minutes=0, half_day=True)
        seed_log(col, "EMP9001", "2026-07-10", "ABSENT")
        seed_log(col, "EMP9001", "2026-07-11", "PRESENT",   # Saturday
                 punch_in_dt=ist(2026,7,11,9,0), punch_out_dt=ist(2026,7,11,18,0),
                 work_hours=9.0, late_minutes=0)

    def test_exactly_one_row_per_day_including_no_log_day(self, client):
        import app.main as m
        self._setup(client, m)
        r = client.get(
            "/analytics/departments/Gamma/trend?from=2026-07-07&to=2026-07-11"
        )
        assert r.status_code == 200
        items = r.json()["items"]
        # Must have exactly 5 rows (Tue–Sat inclusive).
        assert len(items) == 5
        dates = [i["date"] for i in items]
        assert "2026-07-08" in dates   # the no-log day must be present

    def test_gap_day_has_zero_counts(self, client):
        r = client.get(
            "/analytics/departments/Gamma/trend?from=2026-07-07&to=2026-07-11"
        )
        by_date = {i["date"]: i for i in r.json()["items"]}
        gap = by_date["2026-07-08"]
        assert gap["present_count"] == 0.0
        assert gap["late_count"]    == 0

    def test_is_working_day_flags(self, client):
        r = client.get(
            "/analytics/departments/Gamma/trend?from=2026-07-07&to=2026-07-11"
        )
        by_date = {i["date"]: i for i in r.json()["items"]}
        assert by_date["2026-07-07"]["is_working_day"] is True
        assert by_date["2026-07-11"]["is_working_day"] is False  # Saturday

    def test_attendance_rate_null_on_weekend(self, client):
        r = client.get(
            "/analytics/departments/Gamma/trend?from=2026-07-07&to=2026-07-11"
        )
        by_date = {i["date"]: i for i in r.json()["items"]}
        assert by_date["2026-07-11"]["attendance_rate"] is None

    def test_attendance_rate_working_day_with_records(self, client):
        r = client.get(
            "/analytics/departments/Gamma/trend?from=2026-07-07&to=2026-07-11"
        )
        by_date = {i["date"]: i for i in r.json()["items"]}
        # July 7: 1 present out of 1 headcount → rate = 1.0
        assert by_date["2026-07-07"]["attendance_rate"] == pytest.approx(1.0, abs=0.0001)
        # July 8 (no log, gap): 0 present / 1 headcount → rate = 0.0
        assert by_date["2026-07-08"]["attendance_rate"] == pytest.approx(0.0, abs=0.0001)

    def test_half_day_counts_as_0_5_in_present_count(self, client):
        r = client.get(
            "/analytics/departments/Gamma/trend?from=2026-07-07&to=2026-07-11"
        )
        by_date = {i["date"]: i for i in r.json()["items"]}
        assert by_date["2026-07-09"]["present_count"] == pytest.approx(0.5, abs=0.01)

    def test_moving_avg_window_does_not_include_pre_range_rows(self, client):
        """
        The first row's moving_avg_7d must be computed from only the rows
        in the requested range — not from any earlier dates.
        """
        r = client.get(
            "/analytics/departments/Gamma/trend?from=2026-07-07&to=2026-07-11"
        )
        items = r.json()["items"]
        # First row: only one non-null rate in the window (itself).
        # moving_avg_7d = attendance_rate of that first row (if non-null).
        first = items[0]
        if first["attendance_rate"] is not None:
            assert first["moving_avg_7d"] == pytest.approx(first["attendance_rate"], abs=0.0001)

    def test_404_unknown_department(self, client):
        r = client.get(
            "/analytics/departments/NoSuchDept/trend?from=2026-07-07&to=2026-07-11"
        )
        assert r.status_code == 404

    def test_422_to_before_from(self, client):
        r = client.get(
            "/analytics/departments/Gamma/trend?from=2026-07-10&to=2026-07-07"
        )
        assert r.status_code == 422

    def test_422_range_over_92_days(self, client):
        r = client.get(
            "/analytics/departments/Gamma/trend?from=2026-01-01&to=2026-04-05"
        )
        assert r.status_code == 422

    def test_headcount_mid_month_joiner(self, client):
        """
        Employee who joins on July 9 must NOT be counted in headcount for July 7/8,
        but MUST be counted from July 9 onward.
        """
        import app.main as m
        create_emp(client, "EMP9002", dept="Gamma", joined_on="2026-07-09")
        r = client.get(
            "/analytics/departments/Gamma/trend?from=2026-07-07&to=2026-07-11"
        )
        by_date = {i["date"]: i for i in r.json()["items"]}
        assert by_date["2026-07-07"]["headcount"] == 1   # only EMP9001
        assert by_date["2026-07-08"]["headcount"] == 1   # only EMP9001
        assert by_date["2026-07-09"]["headcount"] == 2   # EMP9001 + EMP9002


# ===========================================================================
# T12 – /admin/explain (IXSCAN check)
# ===========================================================================
class TestExplain:
    def test_attendance_list_ixscan(self, client):
        create_emp(client, "EMP8001")
        punch(client, "EMP8001", ist_ms(2026, 7, 6, 9, 0))
        r = client.get(
            "/admin/explain/attendance_list"
            "?date_from=2026-07-01&date_to=2026-07-31"
        )
        assert r.status_code == 200
        explain_str = str(r.json()["explain"])
        assert "IXSCAN" in explain_str
        assert "COLLSCAN" not in explain_str

    def test_employee_monthly_ixscan(self, client):
        r = client.get(
            "/admin/explain/employee_monthly"
            "?emp_code=EMP8001&month=2026-07"
        )
        assert r.status_code == 200
        explain_str = str(r.json()["explain"])
        assert "IXSCAN" in explain_str

    def test_missing_required_param_422(self, client):
        r = client.get("/admin/explain/employee_monthly")  # missing month & emp_code
        assert r.status_code == 422
