# Architectural & Design Decisions

### 1. Database Indexes
We established targeted single-field and compound indexes across both collections:
- **`employees`**:
  - `emp_code` *(unique)*: Enforces global uniqueness of human employee IDs; guarantees HTTP 409 on duplicate creations.
  - `(department, 1), (joined_on, 1)`: Enables `IXSCAN` coverage when filtering department rosters and calculating period headcounts.
  - `department` and `joined_on`: Support standalone filtering and sort stages.
- **`attendance_logs`**:
  - `(emp_code, 1), (date, 1)` *(unique)*: Guarantees at most one log per employee per attendance day; eliminates punch-in race conditions.
  - `(date, -1), (emp_code, 1)`: Serves the primary list query order directly from the index without in-memory sorting.
  - `(date, 1), (late_minutes, 1)`: Accelerates late leaderboard range matching.
  - `(emp_code, 1), (punch_in, -1)`: Optimizes lookup of open punch-ins for punch-out requests.
- **Considered & Rejected**: Single-field index on `history` was rejected as the audit trail is only appended and read with the parent document, never searched independently.

---

### 2. Punch-in Concurrency & Race Conditions
- Two identical punch-in requests arriving simultaneously both calculate the resolved IST `attendance_date`.
- Instead of vulnerable check-then-insert logic (`find_one` followed by `insert_one`), both attempt an atomic `insert_one`.
- MongoDB's unique compound index `(emp_code, date)` accepts exactly one document.
- The winner receives **HTTP 201 Created**.
- The loser encounters a `DuplicateKeyError`, caught cleanly by the handler to return **HTTP 409 Conflict**.

---

### 3. Tie-Breaking & Leaderboard Cutoff
- **Ranking Strategy**: Uses standard competition ranking (`1, 2, 2, 4`), calculated directly via MongoDB's `$rank` window function.
- **Cutoff Rule**: The `limit` filter is applied on `rank <= limit`, rather than an arbitrary array slice.
- **Result**: If two employees tie at the cutoff rank (e.g. rank 10), both are returned. Tied rows are ordered by `emp_code` ascending.

---

### 4. Department Headcount with Zero Logs
- Aggregation begins on the **`employees`** collection (filtering `joined_on <= end_of_month`), never on `attendance_logs`.
- Employee logs are joined using a `$lookup` pipeline.
- Employees with zero attendance logs have an empty logs array but still contribute `1` to `headcount`, ensuring unrecorded days or new joiners are accurately represented.

---

### 5. Scaling for 100x Data Volume
- At 100x volume (~10M records), on-the-fly cross-collection `$lookup` joins and `$densify` aggregations would incur memory and CPU bottlenecks.
- **Architectural Change**: Implement pre-computed daily rollups and monthly materialized views updated via MongoDB Change Streams or incremental background workers.
