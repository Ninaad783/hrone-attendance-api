# DECISIONS.md

1. **Indexes.** In `employees`: unique `(emp_code)` enforces ID uniqueness on creation; `(department, joined_on)` and `(joined_on)` accelerate department filtering and joiner cutoffs. In `attendance_logs`: unique `(emp_code, date)` guarantees the one-record-per-day rule under concurrency; `(date DESC, emp_code ASC)` powers pagination without memory sorting; `(emp_code, date DESC)` speeds monthly summaries; `(emp_code, punch_in DESC)` backs punch-out lookups; and `(date, late_minutes, emp_code)` covers late leaderboard aggregation. We rejected indexing `status` alone due to low cardinality (5 values) and poor selectivity.

2. **Punch-in race.** Both requests compute shift rules and attempt insertion into `attendance_logs`. The unique index on `(emp_code, date)` guarantees atomic serialization at the database engine level: one insert succeeds with 201, while the concurrent duplicate raises `DuplicateKeyError`. The loser catches this error and returns 409 without check-then-insert races.

3. **Ties.** The aggregation computes competition ranks via `$setWindowFields` with `$rank` (1, 2, 2, 4) and filters `{ rank: { $lte: limit } }`. Both tied employees share the cutoff rank, so both are returned (ordered by `emp_code` ascending). The result therefore contains more than `limit` rows, adhering strictly to the contract.

4. **Headcount.** The aggregation starts on `employees` (filtering `joined_on <= period_end`), counting all active department members, then joins `attendance_logs` using `$lookup`. Employees with zero logs produce an empty array but remain counted.

5. **One thing you would change for 100x data.** We would pre-aggregate daily and monthly metrics into roll-up collections via MongoDB change streams or background workers, eliminating real-time multi-stage aggregations across millions of raw logs during read requests.
