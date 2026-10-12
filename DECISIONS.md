# DECISIONS.md

**Indexes.** Unique `emp_code` on employees and unique `(emp_code, date)` on logs make identity and one-row-per-day real database rules. `(date, emp_code)` serves the attendance list (filter + sort). `(emp_code, punch_in)` serves punch-out. `(department, joined_on)` serves headcount. I considered and rejected an index on `status` alone; five values would rarely beat a collection scan enough to pay for extra writes.

**Punch-in race.** Both requests compute the same IST attendance date and `insert_one`. The unique index lets one commit; the other gets `DuplicateKeyError` and we return 409. There is no find-then-insert window.

**Ties.** `$rank` is on `total_late_minutes` only (1, 2, 2, 4). After ranking we keep every row with `rank <= limit`, so a tie at the cutoff is kept even if that exceeds `limit`. We then sort by late minutes desc, `emp_code` asc.

**Headcount.** Summary starts from employees with `joined_on` on or before month-end, looks up logs, and keeps empty log arrays. Those people still count in `$addToSet` of `emp_code`. Later joiners never enter the pipeline.

**At 100x.** Pre-compute one daily department document so trend and summary are not `$densify` plus per-day `$lookup` on raw logs.
