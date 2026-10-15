# DECISIONS.md

## 1. Indexes

Seven created at startup: three on `employees` and four on `attendance_logs`. `employees.emp_code` (unique) is the lookup fast-path and concurrent-insert race guard. `employees.(department, joined_on)` covers summary and trend headcount when a department filter is present; `employees.joined_on` alone covers the same without a department filter, because the compound index cannot help when its leading key is absent. `attendance_logs.(emp_code, date)` (unique) is the monthly analytics match and punch-in duplicate guard. `attendance_logs.(date DESC, emp_code ASC)` serves the attendance list sort without an in-memory stage. `attendance_logs.(date ASC, late_minutes ASC)` lets the leaderboard skip zero-late documents cheaply. `attendance_logs.(date ASC, status ASC)` supports status-and-date filtered attendance queries.

## 2. Simultaneous punch-ins

Both requests compute the same attendance date then call `insert_one`. MongoDB serialises the writes. The first succeeds; the second hits the unique `(emp_code, date)` index, raises `DuplicateKeyError`, and is returned as 409. No read-then-check step exists, so both cannot succeed.

## 3. Ties at the cutoff

`$rank` uses standard competition ranking. The filter is `rank <= limit`, so every employee tied at the cutoff rank is returned, even if that exceeds `limit` rows.

## 4. Headcount with no logs

`department_summary` starts from `employees` and joins with `preserveNullAndEmptyArrays: true`, so employees with zero logs still contribute to the `$addToSet` headcount.

## 5. What I would change for 100× the data

The trend counts `joined_on <= day` in a Python loop over fetched employees. At 100× I would replace it with a server-side `$lookup` + `$sum` on the `(department, joined_on)` index, eliminating the Python loop.
