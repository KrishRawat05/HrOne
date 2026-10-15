# Employee Attendance & Analytics API

A FastAPI service backed by MongoDB for employee records, daily attendance, corrections, and attendance analytics. The API treats employee `emp_code` as its public identifier and returns instants as epoch milliseconds.

## Start Here

For a detailed, step-by-step guide to setting up and building the project, see [BUILD_GUIDE.md](BUILD_GUIDE.md). The HTTP contract is in [openapi.yaml](openapi.yaml), MongoDB document shapes are in [DATA_MODEL.md](DATA_MODEL.md), and implementation trade-offs are in [DECISIONS.md](DECISIONS.md).

## Requirements

- Python 3.11 or newer
- MongoDB 6.0 or newer, which supports the aggregation stages used by this project (`$setWindowFields`, `$densify`, and `$fill`)
- MongoDB reachable from this machine

## Quick Start

Run these commands from the `candidate_kit` project directory.

### 1. Create a virtual environment and install packages

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

### 2. Configure MongoDB

PowerShell:

```powershell
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_db"
```

Linux or macOS:

```bash
export MONGO_URI="mongodb://localhost:27017"
export MONGO_DB="attendance_db"
```

Alternatively, create a `.env` file in the project directory with these keys. Process environment variables take precedence. Both values are required when the app imports `app.main`; missing values cause startup to fail instead of silently choosing a database.

### 3. Start and check the API

```powershell
python -m uvicorn app.main:app --reload --port 8000
```

The application creates seven MongoDB indexes on startup. Index creation is idempotent. Check readiness and open the API documentation:

- `http://localhost:8000/health` should return `{"status":"ok"}` when MongoDB is reachable.
- `http://localhost:8000/docs` opens Swagger UI.
- `http://localhost:8000/redoc` opens ReDoc.

### 4. Load optional sample data

```powershell
python sample_seed.py
```

This replaces all existing documents in the `employees` and `attendance_logs` collections in the configured database. Use a disposable database if it contains data you need to keep. The samples demonstrate document shapes; they are not a substitute for analytics edge-case testing.

### 5. Run tests

```powershell
pytest tests/test_api.py -v
```

The test fixture connects to `mongodb://localhost:27017`, creates a temporary database per test class, and drops that database afterward. Start a local MongoDB server before running the suite.

## API Overview

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Check MongoDB connectivity |
| `POST` | `/employees` | Create an employee |
| `GET` | `/employees` | List employees, optionally filtered by department |
| `POST` | `/attendance/punch-in` | Create a daily attendance record |
| `POST` | `/attendance/punch-out` | Close the most recent open attendance record |
| `GET` | `/attendance` | Filter and paginate attendance records |
| `PATCH` | `/attendance/{emp_code}/{date}` | Correct a record and append audit history |
| `GET` | `/analytics/employees/{emp_code}/monthly` | Monthly employee summary |
| `GET` | `/analytics/departments/summary` | Monthly department summary |
| `GET` | `/analytics/leaderboard/late` | Rank employees by monthly late minutes |
| `GET` | `/analytics/departments/{department}/trend` | Daily department attendance trend |
| `GET` | `/admin/explain/{endpoint}` | Inspect MongoDB query execution statistics |

## Project Files

- [app/main.py](app/main.py): application, validation, attendance calculations, MongoDB access, and routes
- [tests/test_api.py](tests/test_api.py): endpoint, rule, concurrency, and analytics tests
- [sample_data/](sample_data/): small Extended JSON fixtures
- [sample_seed.py](sample_seed.py): load sample fixtures (replaces collection contents)
- [seed_100k.py](seed_100k.py): generate a large performance-test dataset
- [verify_explain.py](verify_explain.py): check explain output for index scans and collection scans
- [openapi.yaml](openapi.yaml): API contract and business rules
- [DATA_MODEL.md](DATA_MODEL.md): MongoDB data model and examples
- [REVIEW.md](REVIEW.md): current project review and historical starter defect catalogue

## Performance Checks

For an isolated performance database, set `MONGO_DB` to a disposable name and seed approximately 100,000 attendance records:

```powershell
python seed_100k.py --drop
```

`--drop` drops the seed script's target collections before regenerating data. Do not point this command at a database whose contents must be preserved. Start the API against the same database, then run:

```powershell
python verify_explain.py
```

`verify_explain.py` uses the optional `requests` package, which is not in `requirements.txt`; install it in the active environment with `python -m pip install requests` if needed. The checker calls all five explain routes and expects to find index scans without collection scans for its selected query shapes.

## Important Constraints

- **No authentication or authorization:** all routes, including record correction and explain routes, are public. Do not expose this service to untrusted networks as-is.
- **No holiday calendar:** working days mean Monday through Friday; public holidays are not excluded.
- **Synchronous MongoDB driver:** the app uses PyMongo. FastAPI runs synchronous handlers in its thread pool; concurrency and throughput should be load-tested for the target deployment. Multiple Uvicorn workers are one scaling option.
- **Department trend headcount:** attendance aggregation is performed in MongoDB, but headcount per day is computed in Python by scanning the department's employees for each date. See [DECISIONS.md](DECISIONS.md).
- **MongoDB version:** the trend route depends on `$densify` and `$fill`; MongoDB versions without these stages cannot run that endpoint.
