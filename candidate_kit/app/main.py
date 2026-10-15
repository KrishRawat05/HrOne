"""
Employee Attendance & Analytics API
====================================
Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI  – MongoDB connection string  (required, no default)
      MONGO_DB   – database name              (required, no default)

All application code lives in this single file per the assignment brief.
Design notes → DECISIONS.md   |   Phase 1 defect catalogue → REVIEW.md
"""

# ---------------------------------------------------------------------------
# Standard library
# ---------------------------------------------------------------------------
import os
import re
import datetime as _datetime_module   # module alias; avoids clash with path-param `date`
from contextlib import asynccontextmanager
from datetime import datetime, date, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Optional

# Stable alias for `datetime.date` so regularize_attendance can call
# _date.fromisoformat() even though its path parameter is also named `date`.
_date = _datetime_module.date

# ---------------------------------------------------------------------------
# Third-party
# ---------------------------------------------------------------------------
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

# ---------------------------------------------------------------------------
# Configuration  (no hard-coded fallback; missing var → clear KeyError)
# ---------------------------------------------------------------------------
load_dotenv()   # honours already-set process env vars (override=False)

_MONGO_URI = os.environ["MONGO_URI"]
_MONGO_DB  = os.environ["MONGO_DB"]

# ---------------------------------------------------------------------------
# Timezone constants
# ---------------------------------------------------------------------------
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

# ---------------------------------------------------------------------------
# MongoDB client
# tz_aware=True → PyMongo returns UTC-aware datetimes → no naive/aware mix.
# ---------------------------------------------------------------------------
client = MongoClient(_MONGO_URI, tz_aware=True, tzinfo=UTC)
db     = client[_MONGO_DB]

employees_col  = db["employees"]
attendance_col = db["attendance_logs"]


# ===========================================================================
# Startup: create indexes idempotently
# ===========================================================================

@asynccontextmanager
async def lifespan(_app: FastAPI):
    # ── employees ────────────────────────────────────────────────────────────

    # 1. Unique on emp_code
    #    • POST /employees race guard (DuplicateKeyError → 409).
    #    • Every find_one({"emp_code": …}) lookup in punch-in, punch-out,
    #      regularize, and all analytics.
    employees_col.create_index(
        [("emp_code", ASCENDING)],
        unique=True,
        name="emp_code_unique",
    )

    # 2. Compound (department ASC, joined_on ASC)
    #    • department_summary $match {"joined_on": {$lte:…}, "department":…}
    #      → index supports both predicates in one scan, no COLLSCAN.
    #    • department_trend find({"department": dept}) headcount query.
    #    • R9: counting employees whose joined_on ≤ end of period.
    employees_col.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)],
        name="dept_joined_on",
    )

    # 2b. Standalone joined_on ASC
    #    • department_summary with NO department filter: $match is only on
    #      joined_on.  The compound (department, joined_on) can't help here
    #      because department is absent; a standalone index eliminates the
    #      COLLSCAN on employees.
    employees_col.create_index(
        [("joined_on", ASCENDING)],
        name="joined_on_asc",
    )

    # ── attendance_logs ───────────────────────────────────────────────────────

    # 3. Unique compound (emp_code ASC, date ASC)
    #    • Punch-in race guard: concurrent inserts for same (emp_code, date)
    #      → DuplicateKeyError → 409, no read-then-insert step needed.
    #    • regularize find_one({"emp_code":…, "date":…}).
    #    • employee_monthly $match: uses both fields → IXSCAN, not COLLSCAN.
    attendance_col.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="emp_date_unique",
    )

    # 4. Compound (date DESC, emp_code ASC)
    #    • GET /attendance default sort: served entirely from the index
    #      → no blocking in-memory sort on 100k rows.
    #    • Date-range filters on the same leading field.
    attendance_col.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="date_desc_emp_asc",
    )

    # 5. Compound (date ASC, late_minutes ASC)
    #    • Leaderboard $match {date:{$gte,lte}, late_minutes:{$gt:0}}
    #      → eliminates COLLSCAN on 100k rows; late_minutes in the key
    #      lets MongoDB skip zero-late docs cheaply.
    attendance_col.create_index(
        [("date", ASCENDING), ("late_minutes", ASCENDING)],
        name="date_late_minutes",
    )

    # 6. Compound (date ASC, status ASC)
    #    • GET /attendance filtered by status + date range.
    attendance_col.create_index(
        [("date", ASCENDING), ("status", ASCENDING)],
        name="date_status",
    )

    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)

_FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
app.mount("/assets", StaticFiles(directory=_FRONTEND_DIR), name="frontend-assets")


@app.get("/", include_in_schema=False)
def frontend():
    return FileResponse(_FRONTEND_DIR / "index.html")


# ===========================================================================
# Epoch-millisecond helpers
# ===========================================================================

_MS_MIN = 100_000_000_000    # 2001-03-03 — below this looks like seconds
_MS_MAX = 4_102_444_800_000  # 2100-01-01


def _validate_epoch_ms(value: Any) -> int:
    """Reject floats, strings, booleans, out-of-range integers → ValueError → 422."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("timestamp must be an integer epoch-milliseconds value")
    if not (_MS_MIN <= value <= _MS_MAX):
        raise ValueError(
            f"timestamp {value} is out of range or looks like epoch-seconds "
            f"(supply milliseconds: {_MS_MIN}–{_MS_MAX})"
        )
    return value


def ms_to_dt(ms: int) -> datetime:
    """Epoch-ms → UTC-aware datetime, truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def dt_to_ms(dt: datetime) -> int:
    """UTC-aware (or naive-UTC) datetime → epoch milliseconds."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


# ===========================================================================
# Business-rule helpers  (all inputs are UTC-aware, truncated to seconds)
# ===========================================================================

def _shift_start_utc(punch_in_utc: datetime, shift_start: str) -> datetime:
    """Shift-start instant (UTC) on the IST calendar date of punch_in_utc."""
    h, m = map(int, shift_start.split(":"))
    punch_ist = punch_in_utc.astimezone(IST)
    return punch_ist.replace(hour=h, minute=m, second=0, microsecond=0).astimezone(UTC)


def compute_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1 — attendance date = IST calendar date of punch_in, except for overnight
    shifts (shift_end <= shift_start): a punch whose IST time < shift_end belongs
    to the shift that started the PREVIOUS IST day.
    """
    punch_ist = punch_in_utc.astimezone(IST)
    sh, sm    = map(int, shift_start.split(":"))
    eh, em    = map(int, shift_end.split(":"))

    if (eh * 60 + em) <= (sh * 60 + sm):          # overnight shift
        if punch_ist.hour * 60 + punch_ist.minute < eh * 60 + em:
            return (punch_ist.date() - timedelta(days=1)).isoformat()

    return punch_ist.date().isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start: str) -> int:
    """
    R2 — late only when diff > 600 s (strictly more than 10:00 after shift_start).
    Returns floor(diff_seconds / 60) i.e. minutes since shift_start, not since
    end of the grace window.
      09:40:00 (600 s) → 0;  09:40:01 (601 s) → 10;  10:15:59 (2759 s) → 45.
    """
    diff = int((punch_in_utc - _shift_start_utc(punch_in_utc, shift_start)).total_seconds())
    return 0 if diff <= 600 else diff // 60


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """R4 — seconds / 3600, HALF-UP to 2 d.p. (Decimal, not Python round())."""
    s = int((punch_out_utc - punch_in_utc).total_seconds())
    return float((Decimal(s) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime_minutes(punch_out_utc: datetime, shift_end_utc: datetime) -> int:
    """R3 — floor minutes beyond shift_end, only when ≥ 30; else 0."""
    diff = int((punch_out_utc - shift_end_utc).total_seconds())
    return 0 if diff < 1800 else diff // 60


def _build_shift_end_utc(att_date_str: str, shift_start: str, shift_end: str) -> datetime:
    """Shift-end instant (UTC) for att_date_str; overnight → next calendar day."""
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    att = _date.fromisoformat(att_date_str)
    end_date = att + timedelta(days=1) if (eh * 60 + em) <= (sh * 60 + sm) else att
    return datetime(end_date.year, end_date.month, end_date.day,
                    eh, em, 0, tzinfo=IST).astimezone(UTC)


def _d2(v: float) -> float:
    return float(Decimal(str(v)).quantize(Decimal("0.01"),   rounding=ROUND_HALF_UP))


def _d4(v: float) -> float:
    return float(Decimal(str(v)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


# ===========================================================================
# Serialisation helpers
# ===========================================================================

def _fmt_employee(doc: dict) -> dict:
    """Raw employee doc → API shape (no _id; created_at as epoch ms)."""
    return {
        "emp_code":    doc["emp_code"],
        "name":        doc["name"],
        "email":       doc["email"],
        "department":  doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end":   doc["shift_end"],
        "joined_on":   doc["joined_on"],
        "created_at":  dt_to_ms(doc["created_at"]),
    }


def _fmt_record(doc: dict) -> dict:
    """Raw attendance doc → API shape (no _id; datetimes as epoch ms)."""

    def _m(v):
        return None if v is None else dt_to_ms(v)

    def _fmt_hist(e: dict) -> dict:
        ch = {}
        for f, d in e.get("changes", {}).items():
            fv, tv = d.get("from"), d.get("to")
            if f in ("punch_in", "punch_out"):
                fv, tv = _m(fv), _m(tv)
            ch[f] = {"from": fv, "to": tv}
        return {"at": dt_to_ms(e["at"]), "by": e["by"], "reason": e["reason"], "changes": ch}

    return {
        "emp_code":         doc["emp_code"],
        "date":             doc["date"],
        "status":           doc["status"],
        "punch_in":         _m(doc.get("punch_in")),
        "punch_out":        _m(doc.get("punch_out")),
        "work_hours":       doc.get("work_hours"),
        "late_minutes":     doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day":         doc.get("half_day", False),
        "history":          [_fmt_hist(e) for e in doc.get("history", [])],
    }


# ===========================================================================
# Pydantic request models
# ===========================================================================

class EmployeeIn(BaseModel):
    emp_code:    str
    name:        str
    email:       str
    department:  str
    shift_start: str = "09:30"
    shift_end:   str = "18:30"
    joined_on:   str

    @field_validator("emp_code")
    @classmethod
    def _emp_code(cls, v: str) -> str:
        if not re.fullmatch(r"EMP\d{4,6}", v):
            raise ValueError("emp_code must be EMP followed by 4–6 digits")
        return v

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not 1 <= len(v) <= 100:
            raise ValueError("name: 1–100 chars")
        return v

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        if len(v) > 120 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v):
            raise ValueError("invalid email (max 120 chars)")
        return v

    @field_validator("department")
    @classmethod
    def _dept(cls, v: str) -> str:
        if not 1 <= len(v) <= 50:
            raise ValueError("department: 1–50 chars")
        return v

    @field_validator("shift_start", "shift_end")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v):
            raise ValueError("shift time must be HH:MM (00:00–23:59)")
        return v

    @field_validator("joined_on")
    @classmethod
    def _joined(cls, v: str) -> str:
        try:
            _date.fromisoformat(v)
        except ValueError:
            raise ValueError("joined_on must be YYYY-MM-DD")
        return v

    @model_validator(mode="after")
    def _shift_ne(self) -> "EmployeeIn":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code:   str
    punched_at: Optional[int] = None
    status:     str = "PRESENT"

    @field_validator("punched_at", mode="before")
    @classmethod
    def _ts(cls, v):
        return None if v is None else _validate_epoch_ms(v)

    @field_validator("status")
    @classmethod
    def _status(cls, v: str) -> str:
        if v not in ("PRESENT", "WFH", "ON_DUTY"):
            raise ValueError("status must be PRESENT, WFH, or ON_DUTY")
        return v


class PunchOutIn(BaseModel):
    emp_code:   str
    punched_at: Optional[int] = None

    @field_validator("punched_at", mode="before")
    @classmethod
    def _ts(cls, v):
        return None if v is None else _validate_epoch_ms(v)


class RegularizeIn(BaseModel):
    status:         Optional[str] = None
    punch_in:       Optional[int] = None
    punch_out:      Optional[int] = None
    reason:         str
    regularized_by: str

    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def _punches(cls, v):
        return None if v is None else _validate_epoch_ms(v)

    @field_validator("status")
    @classmethod
    def _status(cls, v):
        valid = ("PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE")
        if v is not None and v not in valid:
            raise ValueError(f"status must be one of {valid}")
        return v

    @field_validator("reason")
    @classmethod
    def _reason(cls, v: str) -> str:
        if not 5 <= len(v) <= 200:
            raise ValueError("reason: 5–200 chars")
        return v

    @field_validator("regularized_by")
    @classmethod
    def _by(cls, v: str) -> str:
        if not 1 <= len(v) <= 50:
            raise ValueError("regularized_by: 1–50 chars")
        return v

    @model_validator(mode="after")
    def _absent_no_punches(self) -> "RegularizeIn":
        if self.status in ("ABSENT", "LEAVE"):
            if self.punch_in is not None or self.punch_out is not None:
                raise ValueError(
                    "punch_in/punch_out must be omitted for ABSENT or LEAVE"
                )
        return self


# ===========================================================================
# /health
# ===========================================================================

@app.get("/health", tags=["system"])
def health():
    """Ping MongoDB; 503 if unreachable."""
    try:
        client.admin.command("ping")
    except Exception:
        raise HTTPException(503, "MongoDB is not reachable")
    return {"status": "ok"}


# ===========================================================================
# /employees
# ===========================================================================

@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeIn):
    """Atomic insert; unique index turns duplicate → DuplicateKeyError → 409."""
    doc = {
        "emp_code":    body.emp_code,
        "name":        body.name,
        "email":       body.email,
        "department":  body.department,
        "shift_start": body.shift_start,
        "shift_end":   body.shift_end,
        "joined_on":   body.joined_on,
        "created_at":  datetime.now(UTC),
    }
    try:
        employees_col.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return _fmt_employee(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = None,
    page:       int = Query(default=1,  ge=1),
    page_size:  int = Query(default=20, ge=1, le=100),
):
    """Sorted by emp_code ASC; total counts the filtered set."""
    q = {} if not department else {"department": department}
    total  = employees_col.count_documents(q)
    cursor = (
        employees_col
        .find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [_fmt_employee(d) for d in cursor],
            "total": total, "page": page, "page_size": page_size}


# ===========================================================================
# /attendance/punch-in
# ===========================================================================

@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInIn):
    """
    Creates the day's attendance record.
    Date derived per R1 (IST, overnight handled).
    Race-safe: unique (emp_code, date) index → DuplicateKeyError → 409.
    """
    emp = employees_col.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, f"Employee {body.emp_code!r} not found")

    ts_utc = ms_to_dt(body.punched_at) if body.punched_at is not None \
             else datetime.now(UTC).replace(microsecond=0)

    att_date  = compute_attendance_date(ts_utc, emp["shift_start"], emp["shift_end"])
    late_mins = compute_late_minutes(ts_utc, emp["shift_start"])

    doc = {
        "emp_code":         body.emp_code,
        "date":             att_date,
        "status":           body.status,
        "punch_in":         ts_utc,
        "punch_out":        None,
        "work_hours":       None,
        "late_minutes":     late_mins,
        "overtime_minutes": 0,
        "half_day":         False,
        "history":          [],
    }
    try:
        attendance_col.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "Already punched in for this date")
    return _fmt_record(doc)


# ===========================================================================
# /attendance/punch-out
# ===========================================================================

@app.post("/attendance/punch-out", status_code=200, tags=["attendance"])
def punch_out(body: PunchOutIn):
    """
    Closes the most-recent open record (punch_out is null).

    Concurrency: the find_one_and_update filter includes {punch_out: null}.
    The second simultaneous request will find punch_out already set and
    receive result=None → 409, not 404.

    The "already punched-out" case (sequential second punch-out on the same
    record) is also detected here: the open-record query returns None because
    no record has punch_out=null any more, so we return 409.
    """
    emp = employees_col.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, f"Employee {body.emp_code!r} not found")

    ts_utc = ms_to_dt(body.punched_at) if body.punched_at is not None \
             else datetime.now(UTC).replace(microsecond=0)

    # Does a closed record exist for today?  If so: 409 (already punched out).
    # This must be checked BEFORE the open-record query so sequential calls
    # also get 409 (not 404).
    # Strategy: look for the most-recent record whose punch_in ≤ ts_utc,
    # regardless of punch_out state.
    any_record = attendance_col.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": ts_utc},
            "status":   {"$in": ["PRESENT", "WFH", "ON_DUTY"]},
        },
        sort=[("punch_in", DESCENDING)],
    )

    if any_record is None:
        future_record = attendance_col.find_one(
            {
                "emp_code": body.emp_code,
                "punch_in": {"$gt": ts_utc},
                "status":   {"$in": ["PRESENT", "WFH", "ON_DUTY"]},
            },
            sort=[("punch_in", ASCENDING)],
        )
        if future_record is not None:
            raise HTTPException(422, "punched_at must be after punch_in")
        raise HTTPException(404, "No punch-in record found for this employee")

    # If that record is already closed, it was punched out → 409.
    if any_record.get("punch_out") is not None:
        raise HTTPException(409, "Record already punched out")

    punch_in_utc = any_record["punch_in"]

    # Validate timing.
    diff = int((ts_utc - punch_in_utc).total_seconds())
    if diff <= 0:
        raise HTTPException(422, "punched_at must be after punch_in")
    if diff > 86400:
        raise HTTPException(422, "punched_at is more than 24 hours after punch_in")

    shift_end_utc = _build_shift_end_utc(
        any_record["date"], emp["shift_start"], emp["shift_end"]
    )
    wh   = compute_work_hours(punch_in_utc, ts_utc)
    ot   = compute_overtime_minutes(ts_utc, shift_end_utc)
    hday = wh < 4.50

    # Atomic: only wins if punch_out is still null (race guard).
    result = attendance_col.find_one_and_update(
        {"_id": any_record["_id"], "punch_out": None},
        {"$set": {"punch_out": ts_utc, "work_hours": wh,
                  "overtime_minutes": ot, "half_day": hday}},
        return_document=True,
    )
    if result is None:
        raise HTTPException(409, "Record already punched out (concurrent request)")
    return _fmt_record(result)


# ===========================================================================
# /attendance  (list)
# ===========================================================================

@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code:  Optional[str] = None,
    date_from: Optional[str] = None,
    date_to:   Optional[str] = None,
    status:    Optional[str] = None,
    page:      int = Query(default=1,  ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """DB-side sort/skip/limit; never loads the full collection."""
    for label, val in (("date_from", date_from), ("date_to", date_to)):
        if val:
            try:
                _date.fromisoformat(val)
            except ValueError:
                raise HTTPException(422, f"{label} must be YYYY-MM-DD")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be later than date_to")
    if status and status not in ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"):
        raise HTTPException(422, "invalid status value")

    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    total  = attendance_col.count_documents(q)
    cursor = (
        attendance_col
        .find(q, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [_fmt_record(d) for d in cursor],
            "total": total, "page": page, "page_size": page_size}


# ===========================================================================
# PATCH /attendance/{emp_code}/{date}  — regularize
# ===========================================================================

@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    """
    Manually correct one record.  Recomputes all derived fields, appends one
    history entry containing only the fields that actually changed.

    Concurrency guard: the update filter includes
      {history.<N>: {$exists: false}}
    where N = history length at read time.  A concurrent PATCH appends first,
    making index N exist, so our filter fails → 409 (not 422 "no change").
    """
    try:
        _date.fromisoformat(date)
    except ValueError:
        raise HTTPException(422, "date must be YYYY-MM-DD")

    emp = employees_col.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, f"Employee {emp_code!r} not found")

    record = attendance_col.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(404, "No attendance record found for this employee/date")

    # ── Merge ────────────────────────────────────────────────────────────────
    new_status = body.status if body.status is not None else record["status"]
    old_pi, old_po = record.get("punch_in"), record.get("punch_out")

    new_pi = ms_to_dt(body.punch_in)  if body.punch_in  is not None else old_pi
    new_po = ms_to_dt(body.punch_out) if body.punch_out is not None else old_po

    if new_status in ("ABSENT", "LEAVE"):
        new_pi = new_po = None

    if new_status in ("PRESENT", "WFH", "ON_DUTY") and new_pi is None:
        raise HTTPException(422, "A presence status requires punch_in")

    if new_pi is not None:
        if compute_attendance_date(new_pi, emp["shift_start"], emp["shift_end"]) != record["date"]:
            raise HTTPException(422, "punch_in would move record to a different date (R1)")

    if new_po is not None and new_pi is not None:
        d = int((new_po - new_pi).total_seconds())
        if d <= 0:
            raise HTTPException(422, "punch_out must be after punch_in")
        if d > 86400:
            raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    # ── Recompute ────────────────────────────────────────────────────────────
    new_late = compute_late_minutes(new_pi, emp["shift_start"]) if new_pi else 0
    if new_pi and new_po:
        new_wh   = compute_work_hours(new_pi, new_po)
        new_ot   = compute_overtime_minutes(
            new_po,
            _build_shift_end_utc(record["date"], emp["shift_start"], emp["shift_end"]),
        )
        new_hday = new_wh < 4.50
    else:
        new_wh = None; new_ot = 0; new_hday = False

    # ── Detect changes ───────────────────────────────────────────────────────
    old_status = record["status"]
    old_wh     = record.get("work_hours")
    old_late   = record.get("late_minutes", 0)
    old_ot     = record.get("overtime_minutes", 0)
    old_hday   = record.get("half_day", False)

    changes: dict = {}
    if new_status != old_status:
        changes["status"]       = {"from": old_status, "to": new_status}
    if new_pi != old_pi:
        changes["punch_in"]     = {"from": old_pi,  "to": new_pi}
    if new_po != old_po:
        changes["punch_out"]    = {"from": old_po,  "to": new_po}
    if new_wh != old_wh:
        changes["work_hours"]   = {"from": old_wh,  "to": new_wh}
    if new_late != old_late:
        changes["late_minutes"] = {"from": old_late, "to": new_late}
    if new_ot != old_ot:
        changes["overtime_minutes"] = {"from": old_ot,  "to": new_ot}
    if new_hday != old_hday:
        changes["half_day"]     = {"from": old_hday, "to": new_hday}

    if not changes:
        requested_values = {}
        if body.status is not None:
            requested_values["status"] = body.status
        if body.punch_in is not None:
            requested_values["punch_in"] = ms_to_dt(body.punch_in)
        if body.punch_out is not None:
            requested_values["punch_out"] = ms_to_dt(body.punch_out)

        history = record.get("history", [])
        if history and requested_values:
            previous = history[-1]
            previous_changes = previous.get("changes", {})
            is_duplicate = (
                previous.get("by") == body.regularized_by
                and previous.get("reason") == body.reason
                and all(
                    field in previous_changes
                    and previous_changes[field].get("to") == value
                    for field, value in requested_values.items()
                )
            )
            if is_duplicate:
                raise HTTPException(409, "Duplicate or concurrent correction")
        raise HTTPException(422, "Request changes nothing")

    hist_entry = {
        "at":      datetime.now(UTC).replace(microsecond=0),
        "by":      body.regularized_by,
        "reason":  body.reason,
        "changes": changes,   # punch_in/punch_out stored as BSON dates; _fmt_record converts
    }

    # Optimistic concurrency: history length acts as a version token.
    hist_len = len(record.get("history", []))
    result = attendance_col.find_one_and_update(
        {"_id": record["_id"], f"history.{hist_len}": {"$exists": False}},
        {
            "$set": {
                "status":           new_status,
                "punch_in":         new_pi,
                "punch_out":        new_po,
                "work_hours":       new_wh,
                "late_minutes":     new_late,
                "overtime_minutes": new_ot,
                "half_day":         new_hday,
            },
            "$push": {"history": hist_entry},
        },
        return_document=True,
    )
    if result is None:
        raise HTTPException(409, "Concurrent edit detected; please retry")

    return _fmt_record(result)


# ===========================================================================
# Shared month helpers
# ===========================================================================

def _month_range(month: str) -> tuple[str, str]:
    """(first_day_iso, last_day_iso) for a YYYY-MM string."""
    y, m = map(int, month.split("-"))
    start = _date(y, m, 1)
    end   = _date(y + 1, 1, 1) - timedelta(days=1) if m == 12 \
            else _date(y, m + 1, 1) - timedelta(days=1)
    return start.isoformat(), end.isoformat()


def _count_working_days(start: date, end: date) -> int:
    """Mon–Fri count, start..end inclusive."""
    n, cur = 0, start
    while cur <= end:
        if cur.weekday() < 5:
            n += 1
        cur += timedelta(days=1)
    return n


# ===========================================================================
# Pipeline / query BUILDER functions
# ===========================================================================
# Each builder returns everything the endpoint needs to execute its main DB
# operation.  Both the real endpoint and /admin/explain call the same builder,
# so explain always reflects the actual query.
# ===========================================================================

def _build_attendance_list_query(
    emp_code:  Optional[str],
    date_from: Optional[str],
    date_to:   Optional[str],
    status:    Optional[str],
) -> dict:
    """MongoDB filter dict for GET /attendance."""
    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    return q


def _build_employee_monthly_pipeline(emp_code: str, month: str) -> list:
    """
    Aggregation pipeline for GET /analytics/employees/{emp_code}/monthly.

    Uses $dateFromString + $isoDayOfWeek to filter Mon–Fri inside MongoDB
    (our 'date' field is a string so $dayOfWeek won't work directly).
    $isoDayOfWeek: 1=Mon … 7=Sun → Mon–Fri = 1–5.
    Index hit: leading $match on (emp_code, date) uses the unique compound index.
    """
    start, end = _month_range(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {"$addFields": {
            "_diso": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}
        }},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {
                "$cond": [
                    {"$and": [
                        {"$in":  ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$lte": [{"$isoDayOfWeek": "$_diso"}, 5]},
                    ]},
                    {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1.0]},
                    0,
                ]
            }},
            "leave_days":             {"$sum": {"$cond": [{"$eq":  ["$status", "LEAVE"]}, 1, 0]}},
            "late_count":             {"$sum": {"$cond": [{"$gt":  ["$late_minutes", 0]}, 1, 0]}},
            "total_late_minutes":     {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def _build_department_summary_pipeline(month: str, department: Optional[str]) -> list:
    """
    Aggregation pipeline for GET /analytics/departments/summary.

    Starts from `employees` → zero-log employees are counted in headcount (R9).
    $lookup inner pipeline uses $expr to correlate on emp_code; MongoDB can
    use the (emp_code, date) index on attendance_logs for this join.
    $isoDayOfWeek filters Mon–Fri for present_days inside the pipeline.
    """
    start, end = _month_range(month)
    emp_match: dict = {"joined_on": {"$lte": end}}
    if department:
        emp_match["department"] = department

    # Sentinel ISODate used when logs._date_iso is null (employee has no logs):
    # Monday 2000-01-03 → $isoDayOfWeek = 1 (Monday) which is ≤ 5, so the
    # outer $cond on status="" prevents it counting anyway.
    _mon = datetime(2000, 1, 3, tzinfo=UTC)

    return [
        {"$match": emp_match},
        {"$lookup": {
            "from": "attendance_logs",
            "let":  {"ec": "$emp_code"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq":  ["$emp_code", "$$ec"]},
                    {"$gte": ["$date",     start]},
                    {"$lte": ["$date",     end]},
                ]}}},
                {"$addFields": {
                    "_diso": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}
                }},
            ],
            "as": "logs",
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id":       "$department",
            "headcount": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {
                "$cond": [
                    {"$and": [
                        {"$in":  [{"$ifNull": ["$logs.status", ""]}, ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$lte": [{"$isoDayOfWeek": {"$ifNull": ["$logs._diso", _mon]}}, 5]},
                    ]},
                    {"$cond": [{"$eq": [{"$ifNull": ["$logs.half_day", False]}, True]}, 0.5, 1.0]},
                    0,
                ]
            }},
            "wh_sum":   {"$sum": {
                "$cond": [
                    {"$and": [
                        {"$in": [{"$ifNull": ["$logs.status", ""]}, ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$gt": [{"$ifNull": ["$logs.work_hours", None]}, None]},
                    ]},
                    "$logs.work_hours", 0,
                ]
            }},
            "wh_count": {"$sum": {
                "$cond": [
                    {"$and": [
                        {"$in": [{"$ifNull": ["$logs.status", ""]}, ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$gt": [{"$ifNull": ["$logs.work_hours", None]}, None]},
                    ]},
                    1, 0,
                ]
            }},
            "late_count":         {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count":        {"$sum": {"$cond": [{"$eq": [{"$ifNull": ["$logs.status", ""]}, "LEAVE"]},    1, 0]}},
            "on_duty_count":      {"$sum": {"$cond": [{"$eq": [{"$ifNull": ["$logs.status", ""]}, "ON_DUTY"]},  1, 0]}},
        }},
        {"$sort": {"_id": ASCENDING}},
    ]


def _build_leaderboard_pipeline(
    month:      str,
    limit:      int,
    department: Optional[str],
) -> list:
    """
    Pipeline for GET /analytics/leaderboard/late.

    Uses $setWindowFields with $rank (standard competition ranking: 1,2,2,4…).
    $rank requires no partitionBy key (omit it entirely — passing null errors
    in some MongoDB versions).
    Index hit: leading $match on (date, late_minutes) uses date_late_minutes index.
    """
    start, end = _month_range(month)
    pipeline: list = [
        {"$match": {
            "date":         {"$gte": start, "$lte": end},
            "late_minutes": {"$gt": 0},
        }},
        {"$group": {
            "_id":                "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count":         {"$sum": 1},
        }},
        {"$lookup": {
            "from":         "employees",
            "localField":   "_id",
            "foreignField": "emp_code",
            "as":           "emp",
        }},
        {"$match": {"emp": {"$ne": []}}},
        {"$unwind": "$emp"},
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline += [
        {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
        # $setWindowFields with single-field sortBy: $rank requires exactly
        # one sort key.  The secondary tie-break (_id ASC) is applied in the
        # final $sort after ranking, which is correct: ranks are assigned by
        # total_late_minutes only (matching the spec), and the row order
        # within a tie is resolved by the last $sort.
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$project": {
            "_id":                0,
            "rank":               1,
            "emp_code":           "$_id",
            "name":               "$emp.name",
            "department":         "$emp.department",
            "total_late_minutes": 1,
            "late_count":         1,
        }},
        {"$sort": {"total_late_minutes": DESCENDING, "emp_code": ASCENDING}},
    ]
    return pipeline


def _build_trend_pipeline(
    from_date: str,
    to_date:   str,
    department: str,
    from_dt:   datetime,
    to_dt:     datetime,
) -> list:
    """
    Pipeline for GET /analytics/departments/{department}/trend.

    Gap-fill strategy:
    1. $match date-range → $lookup employees to filter by dept.
    2. $addFields converts string date → ISODate → enables $densify.
    3. $group per ISODate day.
    4. $densify fills missing calendar days in [from_dt, to_dt].
    5. $fill sets present_count_raw=0 and late_count=0 on densified gaps.
    6. Sort ascending so the Python loop that adds headcount + rates is in order.
    """
    return [
        {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
        {"$lookup": {
            "from":         "employees",
            "localField":   "emp_code",
            "foreignField": "emp_code",
            "as":           "emp_doc",
        }},
        {"$unwind": "$emp_doc"},
        {"$match": {"emp_doc.department": department}},
        {"$addFields": {
            "_diso": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}
        }},
        {"$group": {
            "_id": "$_diso",
            "present_count_raw": {"$sum": {
                "$cond": [
                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1.0]},
                    0,
                ]
            }},
            "late_count": {"$sum": {
                "$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]
            }},
        }},
        {"$densify": {
            "field": "_id",
            "range": {"step": 1, "unit": "day", "bounds": [from_dt, to_dt]},
        }},
        {"$fill": {
            "output": {
                "present_count_raw": {"value": 0},
                "late_count":        {"value": 0},
            },
        }},
        {"$addFields": {
            "_date_str": {
                "$dateToString": {"format": "%Y-%m-%d", "date": "$_id", "timezone": "UTC"}
            }
        }},
        {"$sort": {"_id": ASCENDING}},
    ]


# ===========================================================================
# GET /analytics/employees/{emp_code}/monthly
# ===========================================================================

@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str,
    month:    str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = employees_col.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, f"Employee {emp_code!r} not found")

    _, month_end  = _month_range(month)
    month_start, _ = _month_range(month)
    joined_on       = _date.fromisoformat(emp["joined_on"])
    eff_start       = max(_date.fromisoformat(month_start), joined_on)
    working_days    = _count_working_days(eff_start, _date.fromisoformat(month_end))

    pipeline = _build_employee_monthly_pipeline(emp_code, month)
    rows = list(attendance_col.aggregate(pipeline))

    if rows:
        r = rows[0]
        raw_present            = r.get("present_days", 0)
        leave_days             = r.get("leave_days", 0)
        late_count             = r.get("late_count", 0)
        total_late_minutes     = int(r.get("total_late_minutes", 0))
        total_overtime_minutes = int(r.get("total_overtime_minutes", 0))
    else:
        raw_present = leave_days = late_count = 0
        total_late_minutes = total_overtime_minutes = 0

    present_days = _d2(raw_present)
    attendance_pct = (
        None if working_days == 0
        else float(
            (Decimal(str(present_days)) / Decimal(working_days) * 100)
            .quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        )
    )

    return {
        "emp_code":               emp_code,
        "month":                  month,
        "working_days":           working_days,
        "present_days":           present_days,
        "leave_days":             leave_days,
        "late_count":             late_count,
        "total_late_minutes":     total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct":         attendance_pct,
    }


# ===========================================================================
# GET /analytics/departments/summary
# ===========================================================================

@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month:      str           = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    pipeline = _build_department_summary_pipeline(month, department)
    items = []
    for row in employees_col.aggregate(pipeline):
        dept      = row["_id"]
        headcount = len(row["headcount"])
        if headcount == 0:
            continue
        wh_count = row.get("wh_count", 0)
        items.append({
            "department":         dept,
            "headcount":          headcount,
            "present_days":       _d2(row.get("present_days", 0)),
            "avg_work_hours":     (
                _d2(row["wh_sum"] / wh_count) if wh_count > 0 else None
            ),
            "late_count":         int(row.get("late_count", 0)),
            "total_late_minutes": int(row.get("total_late_minutes", 0)),
            "leave_count":        int(row.get("leave_count", 0)),
            "on_duty_count":      int(row.get("on_duty_count", 0)),
        })
    return {"month": month, "items": items}


# ===========================================================================
# GET /analytics/leaderboard/late
# ===========================================================================

@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month:      str           = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit:      int           = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    pipeline = _build_leaderboard_pipeline(month, limit, department)
    return {"month": month, "items": list(attendance_col.aggregate(pipeline))}


# ===========================================================================
# GET /analytics/departments/{department}/trend
# ===========================================================================

@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str,
    from_date:  str = Query(..., alias="from"),
    to_date:    str = Query(..., alias="to"),
):
    try:
        from_obj = _date.fromisoformat(from_date)
        to_obj   = _date.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(422, "`from` and `to` must be YYYY-MM-DD")

    if to_obj < from_obj:
        raise HTTPException(422, "`to` must not be before `from`")
    if (to_obj - from_obj).days > 91:
        raise HTTPException(422, "Date range must not exceed 92 days")

    if not employees_col.find_one({"department": department}):
        raise HTTPException(404, f"Department {department!r} not found")

    from_dt = datetime(from_obj.year, from_obj.month, from_obj.day, tzinfo=UTC)
    to_dt   = datetime(to_obj.year,   to_obj.month,   to_obj.day,   tzinfo=UTC)

    # Fetch dept employees once for headcount-per-day calculation.
    dept_emps = list(
        employees_col.find(
            {"department": department},
            {"emp_code": 1, "joined_on": 1, "_id": 0},
        )
    )

    pipeline  = _build_trend_pipeline(from_date, to_date, department, from_dt, to_dt)
    raw_rows  = list(attendance_col.aggregate(pipeline))

    trend_rows = []
    for row in raw_rows:
        d_str     = row["_date_str"]
        d_obj     = _date.fromisoformat(d_str)
        is_wday   = d_obj.weekday() < 5
        headcount = sum(1 for e in dept_emps if e["joined_on"] <= d_str)
        present   = _d2(row.get("present_count_raw", 0))
        lc        = int(row.get("late_count", 0))
        att_rate  = (
            _d4(present / headcount) if is_wday and headcount > 0 else None
        )
        trend_rows.append({
            "date":            d_str,
            "is_working_day":  is_wday,
            "headcount":       headcount,
            "present_count":   present,
            "late_count":      lc,
            "attendance_rate": att_rate,
            "moving_avg_7d":   None,
        })

    # 7-day trailing moving average over non-null attendance_rate values.
    for i, row in enumerate(trend_rows):
        window = [
            trend_rows[j]["attendance_rate"]
            for j in range(max(0, i - 6), i + 1)
            if trend_rows[j]["attendance_rate"] is not None
        ]
        if window:
            row["moving_avg_7d"] = _d4(sum(window) / len(window))

    return {"department": department, "items": trend_rows}


# ===========================================================================
# GET /admin/explain/{endpoint}
# ===========================================================================
# Shared pipeline builders mean there is NO duplicated logic here.
# Both the real endpoint and explain call the identical builder function.
# ===========================================================================

def _agg_explain(col_name: str, pipeline: list) -> dict:
    """
    Run aggregate explain with executionStats verbosity.

    PyMongo ≥ 4.x / MongoDB ≥ 5.0 correct form:
      db.command("explain",
                 {"aggregate": col, "pipeline": [...], "cursor": {}},
                 verbosity="executionStats")
    The 'verbosity' field belongs to the outer 'explain' command, not
    inside 'aggregate', so we cannot pass it as a kwarg to
    db.command("aggregate", ...).
    """
    return db.command(
        "explain",
        {
            "aggregate": col_name,
            "pipeline":  pipeline,
            "cursor":    {},
        },
        verbosity="executionStats",
    )


def _find_explain(col, query: dict, sort: list, skip: int, limit: int) -> dict:
    """
    Run find+sort+skip+limit explain with executionStats verbosity.

    PyMongo 4.x Cursor.explain() accepts no positional arguments;
    the verbosity must go through db.command("explain", {...}).
    """
    return db.command(
        "explain",
        {
            "find":   col.name,
            "filter": query,
            "sort":   {k: v for k, v in sort},
            "skip":   skip,
            "limit":  limit,
        },
        verbosity="executionStats",
    )


@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint:   str,
    emp_code:   Optional[str] = None,
    month:      Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit:      int           = Query(default=10, ge=1, le=50),
    date_from:  Optional[str] = None,
    date_to:    Optional[str] = None,
    status:     Optional[str] = None,
    from_date:  Optional[str] = Query(default=None, alias="from"),
    to_date:    Optional[str] = Query(default=None, alias="to"),
    page:       int           = Query(default=1,  ge=1),
    page_size:  int           = Query(default=20, ge=1, le=100),
):
    """
    Returns MongoDB executionStats explain for the named endpoint's main query.
    Uses the SAME builder functions the real endpoints use — no duplicated logic.
    Unknown endpoint → 404 (per openapi.yaml).
    """
    valid = {
        "attendance_list", "employee_monthly", "department_summary",
        "late_leaderboard", "department_trend",
    }
    if endpoint not in valid:
        raise HTTPException(404, f"Unknown explain endpoint {endpoint!r}. "
                                 f"Valid: {sorted(valid)}")

    # ── attendance_list ──────────────────────────────────────────────────────
    if endpoint == "attendance_list":
        q    = _build_attendance_list_query(emp_code, date_from, date_to, status)
        sort = [("date", DESCENDING), ("emp_code", ASCENDING)]
        skip = (page - 1) * page_size
        ex   = _find_explain(attendance_col, q, sort, skip, page_size)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": ex}

    # All remaining endpoints need `month`.
    if endpoint in ("employee_monthly", "department_summary", "late_leaderboard"):
        if not month:
            raise HTTPException(422, "`month` is required for this endpoint")

    # ── employee_monthly ─────────────────────────────────────────────────────
    if endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(422, "`emp_code` is required for employee_monthly")
        pipeline = _build_employee_monthly_pipeline(emp_code, month)
        ex = _agg_explain("attendance_logs", pipeline)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": ex}

    # ── department_summary ───────────────────────────────────────────────────
    if endpoint == "department_summary":
        pipeline = _build_department_summary_pipeline(month, department)
        ex = _agg_explain("employees", pipeline)
        return {"endpoint": endpoint, "collection": "employees", "explain": ex}

    # ── late_leaderboard ─────────────────────────────────────────────────────
    if endpoint == "late_leaderboard":
        pipeline = _build_leaderboard_pipeline(month, limit, department)
        ex = _agg_explain("attendance_logs", pipeline)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": ex}

    # ── department_trend ─────────────────────────────────────────────────────
    if endpoint == "department_trend":
        if not department:
            raise HTTPException(422, "`department` is required for department_trend")
        if not from_date or not to_date:
            raise HTTPException(422, "`from` and `to` are required for department_trend")
        try:
            from_obj = _date.fromisoformat(from_date)
            to_obj   = _date.fromisoformat(to_date)
        except ValueError:
            raise HTTPException(422, "`from` and `to` must be YYYY-MM-DD")
        from_dt = datetime(from_obj.year, from_obj.month, from_obj.day, tzinfo=UTC)
        to_dt   = datetime(to_obj.year,   to_obj.month,   to_obj.day,   tzinfo=UTC)
        pipeline = _build_trend_pipeline(from_date, to_date, department, from_dt, to_dt)
        ex = _agg_explain("attendance_logs", pipeline)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": ex}

    raise HTTPException(404, "Unknown endpoint")  # unreachable
