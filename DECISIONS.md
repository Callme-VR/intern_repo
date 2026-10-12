# Architectural Decisions

### 1. Indexes
On `employees`, I created unique `emp_code` to ensure unique IDs and `(department, joined_on)` for roster filtering. On `attendance_logs`, I added unique `(emp_code, date)` to guarantee one log per day, `(date, -1, emp_code, 1)` for default listing sort, and `(date, late_minutes)` for the leaderboard. I considered indexing `history` but rejected it because it's only read embedded, never queried directly.

### 2. Punch-in race
Both simultaneous requests compute the same date and attempt `insert_one()`. MongoDB's unique index on `(emp_code, date)` atomically accepts one and rejects the second with a `DuplicateKeyError`. I catch that error and return HTTP 409, while the first gets HTTP 201.

### 3. Ties
I calculate rankings using MongoDB's `$rank` and filter with `rank <= limit`. If two employees tie at the cutoff, both are returned (ordered by `emp_code`) because standard competition ranking shouldn't arbitrarily drop tied participants.

### 4. Headcount
I start the aggregation on `employees` instead of `attendance_logs`, joining logs via `$lookup`. Employees with zero logs still match the initial pipeline stage and count as 1 toward their department's headcount.

### 5. One change for 100x data
I would replace heavy on-the-fly `$lookup` aggregations with pre-aggregated daily summaries and materialized views updated asynchronously via change streams.
