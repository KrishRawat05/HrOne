# Step-by-Step Build Guide

This guide walks through designing, implementing, running, and checking the Employee Attendance & Analytics API in this repository. It is written for the current implementation: FastAPI, Pydantic 2, synchronous PyMongo, and MongoDB.

## 1. Understand the project before coding

Start with the contract and data definitions:

1. Read [openapi.yaml](openapi.yaml) for route names, methods, parameters, response shapes, status codes, and rules R1-R10.
2. Read [DATA_MODEL.md](DATA_MODEL.md) for the `employees` and `attendance_logs` collection shapes and BSON types.
3. Read [DECISIONS.md](DECISIONS.md) for index and aggregation design decisions.
4. Treat `emp_code` as the employee's permanent public key. MongoDB `_id` is internal and must not appear in API responses.

The project files are intentionally small: most application behavior lives in [app/main.py](app/main.py), and the integration suite is in [tests/test_api.py](tests/test_api.py).

## 2. Prepare Python and MongoDB

Use Python 3.11+ and a reachable MongoDB 6.0+ server. The analytics use `$setWindowFields`, `$densify`, and `$fill`; MongoDB 6.0 is the supported minimum for the complete set of stages.

From the repository's `candidate_kit` directory, create and activate a virtual environment, then install dependencies.

Windows PowerShell:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Linux or macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

## 3. Configure database access

The app reads `MONGO_URI` and `MONGO_DB` during import. Set both before starting Uvicorn.

```powershell
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_db"
```

Or add them to a project-root `.env` file:

```dotenv
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

`python-dotenv` loads `.env` without replacing process environment variables. Do not commit real credentials or production connection strings. The required variables have no app-level fallback.

## 4. Create the application and database lifecycle

In `app/main.py`, the app is configured once, and a FastAPI lifespan handler creates indexes when the server starts. Use idempotent `create_index` calls so restarting the service does not duplicate indexes.

The current startup creates seven indexes:

| Collection | Index | Purpose |
|---|---|---|
| `employees` | unique `emp_code` | Employee lookups and duplicate-create protection |
| `employees` | `(department, joined_on)` | Department headcount and filtered summaries |
| `employees` | `joined_on` | Unfiltered period headcount queries |
| `attendance_logs` | unique `(emp_code, date)` | One attendance record per employee/day and duplicate-punch protection |
| `attendance_logs` | `(date DESC, emp_code ASC)` | Attendance list date ordering and range filtering |
| `attendance_logs` | `(date, late_minutes)` | Late leaderboard match and aggregation |
| `attendance_logs` | `(date, status)` | Status and date filtering |

The `health` route should ping MongoDB and return `503` if the database cannot be reached. That makes it useful as a readiness probe, not merely as a process-alive response.

## 5. Define storage and API time formats

Keep a clear distinction between instants and calendar values:

- Store instants such as punches and `created_at` as BSON UTC datetimes.
- Accept and return instants as integer epoch milliseconds.
- Store attendance dates and join dates as `YYYY-MM-DD` strings.
- Store shift times as `HH:MM` strings interpreted in IST (UTC+05:30).
- Do not expose MongoDB `_id` in API results.

The app centralizes conversions in `ms_to_dt()` and `dt_to_ms()`. Incoming epoch milliseconds are validated as integers in the accepted range, converted to UTC, and truncated to whole seconds before calculation. Keeping conversion at the API boundary avoids mixing timezone-naive and timezone-aware values.

## 6. Implement input validation

Use Pydantic request models to reject invalid input before database writes. The current models validate:

- Employee codes, name/email/department lengths, date format, and `HH:MM` shifts.
- Different shift start and end values.
- Punch statuses (`PRESENT`, `WFH`, or `ON_DUTY`).
- Epoch-millisecond timestamps; floats, booleans, strings, and seconds-scale values are rejected.
- Regularization status, reason, actor label, and the rule that `ABSENT`/`LEAVE` cannot include punch times.

FastAPI responds with `422` for request validation failures. Keep request validation separate from database conflict responses such as `409`.

## 7. Implement attendance calculations

Keep calculation logic in small helpers so routes and tests can share the same rules. The key rules are:

1. **Attendance date:** use the punch-in's IST date. For an overnight shift (`shift_end <= shift_start`), a punch before the shift end belongs to the shift that began the previous day.
2. **Late minutes:** allow a 10-minute grace period. Exactly 10:00 late is not late; after that, report whole elapsed minutes from shift start.
3. **Overtime:** count whole minutes after shift end only when the duration is at least 30 minutes.
4. **Work hours:** divide whole elapsed seconds by 3,600 and round to two decimals using decimal half-up rounding.
5. **Half day:** set true when the rounded work-hours result is less than 4.50.
6. **Presence:** `PRESENT`, `WFH`, and `ON_DUTY` count as present; `ABSENT` and `LEAVE` do not.

Follow the source-of-truth rule: analytics read stored `work_hours`, `late_minutes`, `overtime_minutes`, and `half_day`; they do not recompute these values from punches.

## 8. Build the HTTP routes in dependency order

Implement the CRUD path before analytics so each later step has reliable records to query.

1. **Readiness and employee creation/listing:** `GET /health`, `POST /employees`, and `GET /employees`. Rely on the unique database index for race-safe duplicate detection. For paginated lists, apply sorting, `skip`, and `limit` in MongoDB and count using the same filter.
2. **Punch-in:** `POST /attendance/punch-in`. Confirm the employee exists, calculate IST attendance date and late minutes, then insert. Map the unique `(emp_code, date)` index conflict to `409`.
3. **Punch-out:** `POST /attendance/punch-out`. Find the latest eligible open record, validate the punch time, compute work hours/overtime/half-day, and update atomically only if `punch_out` is still null.
4. **Attendance list:** `GET /attendance`. Validate date filters and status, sort by date descending then employee code ascending, and paginate in MongoDB rather than loading the full collection.
5. **Regularization:** `PATCH /attendance/{emp_code}/{date}`. Merge only supplied changes, recalculate derived fields, reject no-op/invalid changes, and append one audit-history entry. Use an optimistic concurrency condition so competing updates do not silently overwrite history.
6. **Analytics:** add monthly employee summary, department summary, late leaderboard, and department trend after the write/query routes are stable.
7. **Explain diagnostics:** `GET /admin/explain/{endpoint}` should use the same query or pipeline builders as the live routes, so it explains the real query shape.

The route list and full response contract are in [openapi.yaml](openapi.yaml). Swagger UI at `/docs` is useful for manually exercising routes.

## 9. Build analytics with explicit definitions

Before writing aggregation pipelines, translate each metric into a rule and test input:

- Monthly working days are Monday-Friday from the later of the month start or `joined_on` through month end. No public holiday calendar is applied.
- Present days include weekdays with a presence status; a half-day contributes `0.5`. Weekend records do not add to present days, but still contribute to late/overtime totals where specified.
- Leave days count records with `LEAVE`. A missing record is not the same thing as an `ABSENT` record.
- Department headcount includes employees who joined by the period end, even if they have no attendance documents. For that reason, department summary starts from employees and joins attendance.
- Department average work hours use present-status records with a non-null `work_hours` value.
- Leaderboard ranking uses total late minutes. Ties at the cutoff are included by rank.
- Trend output fills dates without attendance using MongoDB `$densify`/`$fill`; daily headcount includes employees already joined on that date.

Use the existing pipeline builder helpers in `app/main.py` as the pattern: each builder defines the query once and is shared by the route and explain handler.

## 10. Run and smoke-test the service

With MongoDB running and variables set:

```powershell
python -m uvicorn app.main:app --reload --port 8000
```

In another terminal, check health:

```powershell
Invoke-RestMethod http://localhost:8000/health
```

Create an employee and punch in using `curl.exe` (PowerShell's `curl` name may resolve to a different command):

```powershell
curl.exe -X POST http://localhost:8000/employees `
  -H "Content-Type: application/json" `
  -d '{"emp_code":"EMP0001","name":"Asha Rao","email":"asha@example.com","department":"Engineering","shift_start":"09:30","shift_end":"18:30","joined_on":"2026-01-05"}'

curl.exe -X POST http://localhost:8000/attendance/punch-in `
  -H "Content-Type: application/json" `
  -d '{"emp_code":"EMP0001","punched_at":1783312500000,"status":"PRESENT"}'
```

Use a timestamp appropriate for your test date. `punched_at` is epoch milliseconds, not seconds. If omitted, punch-in/out use the server clock.

## 11. Run automated tests

Start a local MongoDB at `mongodb://localhost:27017`, activate the virtual environment, and run:

```powershell
pytest tests/test_api.py -v
```

The tests cover validation, time calculations, overnight shifts, pagination, race conditions, audit history, monthly analytics, and other contract cases. Each test class uses a uniquely named database and drops it during teardown. The fixture's MongoDB URI is hard-coded to localhost, so setting `MONGO_URI` does not redirect this test suite.

## 12. Check query plans at larger scale

For performance work, use a disposable database. Configure `MONGO_URI` and a dedicated `MONGO_DB`, then seed roughly 100,000 attendance records:

```powershell
python seed_100k.py --drop
```

The `--drop` option deletes the seed target collections before rebuilding. Start the app using the same environment, then:

```powershell
python verify_explain.py
```

The verifier checks the five supported explain cases: `attendance_list`, `employee_monthly`, `department_summary`, `late_leaderboard`, and `department_trend`. It requires `requests`, which is not part of the app's `requirements.txt`; install it with `python -m pip install requests` if needed. Explain outcomes depend on query shape, indexes, server version, and data distribution, so use them as evidence for the tested filters rather than a universal performance guarantee.

## 13. Know the operating limits

- No authentication or authorization is implemented; protect the service behind an appropriate trusted boundary before deployment.
- There is no holiday calendar; working-day logic is weekdays only.
- The application uses synchronous PyMongo. Load-test request concurrency and worker settings for the intended environment.
- The trend endpoint loads department employees into Python and computes per-day headcount there. This can become expensive for large departments or long date ranges; the API limits trends to at most 92 days. See [DECISIONS.md](DECISIONS.md).
- Sample and bulk seed scripts write to MongoDB. Always select a disposable database for data-loading experiments.

## Related Documents

- [README.md](README.md): concise setup and command reference
- [openapi.yaml](openapi.yaml): API contract
- [DATA_MODEL.md](DATA_MODEL.md): stored document model
- [DECISIONS.md](DECISIONS.md): design trade-offs
- [REVIEW.md](REVIEW.md): project review and historical defect catalogue