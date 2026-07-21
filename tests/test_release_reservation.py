"""Tests for the reservation-release endpoint (HYRAX-667).

Key scenarios:
- Happy path: UPDATE succeeds, DELETE runs, transaction commits atomically.
- Restore-fails path: UPDATE affects 0 rows (item missing); transaction rolls
  back, reservation is preserved, 409 is returned.
- DB-error path: UPDATE raises a DB exception; transaction rolls back,
  reservation is preserved, exception propagates.
- Reservation-not-found path: 404 returned before any DB write is attempted.
"""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app, raise_server_exceptions=False)

HEADERS = {"x-tenant-id": "tenant1"}


def _mock_cursor(rowcount: int = 1, raise_on_execute: Exception | None = None):
    """Return a mock cursor whose first execute() sets rowcount."""
    cur = MagicMock()
    if raise_on_execute is not None:
        cur.execute.side_effect = raise_on_execute
    else:
        # First call (UPDATE) sets rowcount; subsequent calls (DELETE) succeed.
        def _execute_side_effect(sql, params):
            if "UPDATE items" in sql:
                cur.rowcount = rowcount

        cur.execute.side_effect = _execute_side_effect
        cur.rowcount = rowcount
    return cur


def _make_transaction_cm(cur):
    """Build a context manager that yields a connection using *cur*."""

    @contextmanager
    def _txn():
        conn = MagicMock()
        conn.cursor.return_value = cur
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
        finally:
            conn.close()

    return _txn


class TestReleaseReservation:
    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _patch_fetch_one(self, return_value):
        return patch("app.routes.sync.fetch_one", return_value=return_value)

    def _patch_transaction(self, cur):
        return patch("app.routes.sync.transaction", _make_transaction_cm(cur))

    def _patch_cache(self):
        return patch("app.routes.sync.cache")

    # ------------------------------------------------------------------
    # Happy path
    # ------------------------------------------------------------------

    def test_happy_path_commits_both_statements(self):
        """UPDATE + DELETE both execute and the transaction is committed."""
        res_row = {"sku": "SKU1", "warehouse_id": "WH1", "quantity": 5}
        cur = _mock_cursor(rowcount=1)

        with (
            self._patch_fetch_one(res_row),
            self._patch_transaction(cur),
            self._patch_cache(),
        ):
            response = client.post("/reservations/ORDER1/release", headers=HEADERS)

        assert response.status_code == 200
        data = response.json()
        assert data["order_id"] == "ORDER1"
        assert data["released"] == 5

        # Both UPDATE and DELETE must have been executed.
        calls = [str(call.args[0]) for call in cur.execute.call_args_list]
        assert any("UPDATE items" in c for c in calls)
        assert any("DELETE FROM reservations" in c for c in calls)
        cur.__class__  # just ensure mock not errored

    # ------------------------------------------------------------------
    # Restore-fails path (HYRAX-667 core case)
    # ------------------------------------------------------------------

    def test_update_zero_rows_returns_409_and_rolls_back(self):
        """If the UPDATE touches 0 rows, 409 is returned and the transaction
        is rolled back so the reservation row is NOT deleted."""
        res_row = {"sku": "SKU1", "warehouse_id": "WH1", "quantity": 5}
        cur = _mock_cursor(rowcount=0)

        with (
            self._patch_fetch_one(res_row),
            self._patch_transaction(cur),
            self._patch_cache(),
        ):
            response = client.post("/reservations/ORDER1/release", headers=HEADERS)

        assert response.status_code == 409
        # DELETE must NOT have been called.
        calls = [str(call.args[0]) for call in cur.execute.call_args_list]
        assert not any("DELETE FROM reservations" in c for c in calls), (
            "DELETE should not run when UPDATE affects 0 rows"
        )

    def test_update_zero_rows_rollback_called(self):
        """Verify the connection is explicitly rolled back when UPDATE hits 0 rows."""
        res_row = {"sku": "SKU1", "warehouse_id": "WH1", "quantity": 3}
        cur = _mock_cursor(rowcount=0)

        rollback_called = []

        @contextmanager
        def _tracked_txn():
            conn = MagicMock()
            conn.cursor.return_value = cur
            try:
                yield conn
            except Exception:
                rollback_called.append(True)
                conn.rollback()
                raise
            else:
                conn.commit()
            finally:
                conn.close()

        with (
            self._patch_fetch_one(res_row),
            patch("app.routes.sync.transaction", _tracked_txn),
            self._patch_cache(),
        ):
            response = client.post("/reservations/ORDER2/release", headers=HEADERS)

        assert response.status_code == 409
        assert rollback_called, "rollback() must be called when restore fails"

    # ------------------------------------------------------------------
    # DB-exception path
    # ------------------------------------------------------------------

    def test_db_exception_on_update_rolls_back(self):
        """A DB-level exception during UPDATE causes rollback; 500 returned."""
        import psycopg2

        res_row = {"sku": "SKU1", "warehouse_id": "WH1", "quantity": 2}
        db_error = psycopg2.OperationalError("deadlock detected")
        cur = _mock_cursor(raise_on_execute=db_error)

        rollback_called = []

        @contextmanager
        def _tracked_txn():
            conn = MagicMock()
            conn.cursor.return_value = cur
            try:
                yield conn
            except Exception:
                rollback_called.append(True)
                conn.rollback()
                raise
            else:
                conn.commit()
            finally:
                conn.close()

        with (
            self._patch_fetch_one(res_row),
            patch("app.routes.sync.transaction", _tracked_txn),
            self._patch_cache(),
        ):
            response = client.post("/reservations/ORDER3/release", headers=HEADERS)

        assert response.status_code == 500
        assert rollback_called, "rollback() must be called when UPDATE raises"

        # DELETE must never have been called.
        calls = [str(call.args[0]) for call in cur.execute.call_args_list]
        assert not any("DELETE FROM reservations" in c for c in calls)

    # ------------------------------------------------------------------
    # Reservation-not-found path
    # ------------------------------------------------------------------

    def test_missing_reservation_returns_404(self):
        """404 is returned before any DB write when the reservation doesn't exist."""
        cur = _mock_cursor(rowcount=1)

        with (
            self._patch_fetch_one(None),
            self._patch_transaction(cur),
            self._patch_cache(),
        ):
            response = client.post("/reservations/GHOST/release", headers=HEADERS)

        assert response.status_code == 404
        # No write statements should have run.
        cur.execute.assert_not_called()
