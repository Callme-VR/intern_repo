# DECISIONS.md

### 1. **Indexes.** Which indexes did you create, and which query or guarantee does each one serve? Which one did you consider and reject?
I created unique `emp_code` on employees to stop duplicate IDs, and `(department, joined_on)` for department filtering. On attendance, I added unique `(emp_code, date)` to block duplicate punches per day, `(date, -1, emp_code, 1)` for sorting, and `(date, late_minutes)` for leaderboards. I rejected indexing `history` because it is only read with the log, never searched alone.

### 2. **Punch-in race.** Two identical punch-in requests arrive at the same instant. Walk through exactly what happens in your code.
Both requests compute the same date and try `insert_one()`. MongoDB's unique `(emp_code, date)` index accepts the first request with `201 Created` and rejects the second with a duplicate key error. My code catches this error and returns `409 Conflict`.

### 3. **Ties.** Two employees are tied on late minutes at the cutoff of `limit`. What does your leaderboard return, and why?
I rank employees with `$rank` and filter by `rank <= limit`. If two employees tie at the cutoff, both are returned (ordered by `emp_code`) so tied employees are not unfairly dropped.

### 4. **Headcount.** How does your department summary make sure employees with zero logs are counted?
I query `employees` first and attach logs using `$lookup`. Employees with no logs still match the initial query, so each counts as 1 in headcount.

### 5. **One thing you would change** if this had to serve 100x the data.
I would stop running live `$lookup` queries and instead pre-calculate daily and monthly totals in background summary collections.
