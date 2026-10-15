# REVIEW.md

Honest assessment of the current implementation — what works, what is rough, and what I would improve given more time.

---

## What works

- All 11 endpoints from the OpenAPI contract are implemented and return the correct shapes.
- Business rules R1–R10 are implemented: IST date resolution, overnight shifts, 10-minute grace period, half-up rounding, half-day threshold, working-day counting from `joined_on`, standard competition ranking.
- Concurrent punch-in duplicates are prevented by the unique MongoDB index (no check-then-insert race).
- Concurrent punch-out duplicates are prevented by the atomic `find_one_and_update` on `punch_out: null`.
- Concurrent regularization conflicts are handled by an optimistic retry loop.
- Every query uses an explicit index hint so the planner never falls back to a collection scan.
- `$setWindowFields` with `$rank` gives correct competition ranking (ties share a rank, next rank is skipped).
- The department trend fills gaps with `$unionWith` + `$range` so every calendar day in the range appears even when there are no records.

---

## Known rough edges

**`history.at` is stored as epoch milliseconds, not a BSON date.**
`now_utc_naive()` currently returns an integer (milliseconds) rather than a `datetime`. The DATA_MODEL says `at` should be a BSON date in MongoDB and epoch milliseconds in the API response. The `attendance_out` serialiser does not convert `history[].at`, so the raw integer leaks into the response. Fix: store `datetime` and convert in the serialiser.

**`history[].changes` punch times are not converted to epoch milliseconds in the response.**
`punch_in` and `punch_out` inside `changes` are stored as `datetime` objects. The serialiser returns them as-is, which PyMongo renders as ISO strings rather than integers. Fix: walk `changes` in `attendance_out` and convert any `datetime` values.

**`AttendancePatch` uses `corrected_by` but the spec uses `regularized_by`.**
The request model accepts `corrected_by` (with an alias for `by`), but the OpenAPI contract names the field `regularized_by`. This will cause 422 errors for clients that follow the spec exactly.

**`punch_out` endpoint finds the most recent open record by `punch_out: null` without filtering by date.**
If an employee has two open records (data anomaly), the wrong one could be closed. The spec says "the most recent record whose `punch_in` is at or before `punched_at`". The current implementation does not enforce the 24-hour window either.

**No input validation on `emp_code` path parameters.**
Path parameters like `emp_code` in `PATCH /attendance/{emp_code}/{date}` are not length-validated. A very long string would hit MongoDB without being rejected at the API layer.

---

## What I would improve with more time

1. **Fix the `history` serialisation issues** described above — they are straightforward but fiddly.
2. **Align `regularized_by` field name** with the OpenAPI spec.
3. **Tighten punch-out logic** to find the record whose `punch_in <= punched_at` and enforce the 24-hour window.
4. **Add integration tests** against a real MongoDB instance (pytest + mongomock or a local container) covering the edge cases: overnight shifts, ties in the leaderboard, employees with no logs, mid-month joiners.
5. **Split the file** into modules (`routes/`, `models.py`, `db.py`, `analytics/`) once the single-file constraint is lifted — the current file is long and hard to navigate.
6. **Structured logging** with request IDs to make debugging easier in production.
