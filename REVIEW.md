# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `health` | Returned 200 without pinging MongoDB. The runner polls this and expects 503 when the database is down. | Stop MongoDB and `GET /health` still returned `{"status":"ok"}`. | Ping with `db.command("ping")`; 503 if it fails. |
| 2 | `compute_late_minutes` | Built shift start by `replace()` on the punch datetime, so UTC instants were treated as if they were already IST. Overnight shifts were ignored. `minutes > 10` used a float divide, not "strictly more than 10:00" then floor minutes from shift start (R2: `09:40:01` must be 10). | Punch 09:40:01 IST against 09:30; overnight 22:00 start. | Compute shift start on the attendance date in IST; late only if delta > 600s; `int(delta // 60)`. |
| 3 | `compute_overtime` | Combined `date_str` (naive) with IST `tzinfo` and did not move overnight `shift_end` to the next day. Also returned overtime below the 30-minute minimum (R3). | Sample EMP0005 (22:00–06:00) or 5 minutes past shift end. | Shift end on the attendance date, +1 day if overnight; 0 unless minutes >= 30. |
| 4 | `compute_work_hours` | Used binary float `/ 3600` and `round()`, which is banker's rounding, not half-up (R4). Did not truncate to whole seconds first (R1). | Intervals whose true hours are `x.xx5`. | Truncate instants; `Decimal` seconds/3600 with `ROUND_HALF_UP` to 2 places. |
| 5 | `create_employee` | Duplicate `emp_code` was a find-then-insert race. `created_at` was naive local time and returned as a datetime, not epoch ms. `_id` could leak. `shift_start == shift_end` was allowed. | Two concurrent POSTs with the same code; response JSON types. | Unique index + `DuplicateKeyError` → 409; UTC truncated `created_at`; serialize to epoch ms; reject equal shifts. |
| 6 | `list_employees` | `skip = page * page_size` skipped the first page. `total` counted the whole collection, not the filter. No sort by `emp_code`. `page_size` uncapped. Datetimes not converted. | `GET /employees?page=1` returned no rows once there were 20 employees. | `skip = (page-1)*page_size`; `count_documents(q)`; sort `emp_code`; `page_size` max 100; serialize. |
| 7 | `punch_in` | Crashed on unknown employee (`emp["shift_start"]`). Attendance `date` used the naive local calendar day, not R1/IST/overnight. Check-then-insert race. Returned Mongo `id` (not in the contract). Instants not truncated; naive `datetime.now()`. | Missing emp → 500; night-shift punch after midnight; two simultaneous punch-ins. | 404 if missing; R1 date; unique `(emp_code, date)` + DuplicateKeyError → 409; no `_id` in the body. |
| 8 | `list_attendance` | Loaded the whole collection, sorted in Python. Did not convert BSON dates to epoch ms. Missing `history` / `half_day` not defaulted. `date_from > date_to` not rejected. Sort ignored `emp_code`. | ~100k rows: timeout / memory. Seeded sample 9 missing `history`. | Mongo `find` + `sort` + `skip`/`limit` + `count_documents`; serialize with defaults; 422 if the date range is inverted. |

Also note anything you looked at and decided was **not** a defect, and why.

- Returning FastAPI's default `{"detail": ...}` error envelope is required, so the starter's `HTTPException(409, "...")` shape is fine.
- Defaulting punch status to `PRESENT` matches the contract.
- Using `python-dotenv` without `override=True` is correct: real environment variables must win.
