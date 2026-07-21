# Reservation deleted even when stock restore fails

**Tool:** `mini_audit`
**Severity:** critical
**Category:** correctness
**Location:** `app/routes/sync.py:74`

## What's wrong

The `finally` block always runs the `DELETE FROM reservations` regardless of whether the preceding `UPDATE items SET quantity = quantity + %s` succeeded or raised an exception. If the UPDATE fails (e.g. DB error, deadlock), the reservation row is deleted but the quantity is never restored, permanently losing stock.

**Fix:** Replace the `try/finally` pattern with a single transaction (use `db.transaction()`) that atomically does both the UPDATE and the DELETE, so either both succeed or both roll back.
