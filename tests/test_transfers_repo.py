"""Unit tests for app.transfers_repo.

These use a fake connection that records every statement. They check what
the repo layer is responsible for: tenant scoping, the conditional
decrement, the upsert, the keyset predicate/order, and that no function
commits or closes the caller's connection.
"""

from datetime import datetime, timezone

from app import transfers_repo


class RecordingCursor:
    def __init__(self, conn):
        self._conn = conn
        self.rowcount = conn.next_rowcount
        self._rows = list(conn.next_rows)

    def execute(self, sql, params=()):
        self._conn.statements.append((sql, tuple(params)))

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class RecordingConnection:
    def __init__(self, rowcount=0, rows=()):
        self.statements: list[tuple[str, tuple]] = []
        self.next_rowcount = rowcount
        self.next_rows = rows
        self.committed = False
        self.closed = False

    def cursor(self, cursor_factory=None):
        return RecordingCursor(self)

    def commit(self):
        self.committed = True

    def rollback(self):
        pass

    def close(self):
        self.closed = True


def _assert_caller_owns_conn(conn):
    assert not conn.committed
    assert not conn.closed


def test_decrement_stock_succeeds_when_row_updated():
    conn = RecordingConnection(rowcount=1)
    assert transfers_repo.decrement_stock(conn, "tenant-a", "w1", "WIDGET", 3) is True
    sql, params = conn.statements[0]
    assert sql.startswith("UPDATE items SET quantity = quantity - %s")
    assert "quantity >= %s" in sql
    assert "tenant_id = %s" in sql
    assert params == (3, "tenant-a", "w1", "WIDGET", 3)
    _assert_caller_owns_conn(conn)


def test_decrement_stock_reports_failure_when_insufficient_or_missing():
    conn = RecordingConnection(rowcount=0)
    assert transfers_repo.decrement_stock(conn, "tenant-a", "w1", "WIDGET", 3) is False


def test_increment_stock_upserts_tenant_scoped_row():
    conn = RecordingConnection()
    transfers_repo.increment_stock(conn, "tenant-a", "w2", "WIDGET", 4)
    sql, params = conn.statements[0]
    assert sql.startswith("INSERT INTO items")
    assert "ON CONFLICT (tenant_id, warehouse_id, sku)" in sql
    assert "quantity = items.quantity + EXCLUDED.quantity" in sql
    assert params[:3] == ("tenant-a", "w2", "WIDGET")
    assert params[-1] == 4
    # Name/price lookups for a new row come only from the same tenant.
    assert params.count("tenant-a") == 3
    _assert_caller_owns_conn(conn)


def test_insert_transfer_returns_row():
    created = datetime(2024, 1, 1, tzinfo=timezone.utc)
    row = {
        "id": "t1",
        "tenant_id": "tenant-a",
        "sku": "WIDGET",
        "source_warehouse_id": "w1",
        "destination_warehouse_id": "w2",
        "quantity": 2,
        "created_at": created,
    }
    conn = RecordingConnection(rows=[row])
    out = transfers_repo.insert_transfer(conn, "tenant-a", "WIDGET", "w1", "w2", 2)
    assert out == row
    sql, params = conn.statements[0]
    assert sql.startswith("INSERT INTO transfers")
    assert "RETURNING" in sql
    assert params == ("tenant-a", "WIDGET", "w1", "w2", 2)
    _assert_caller_owns_conn(conn)


def test_list_transfers_first_page_is_tenant_scoped_and_newest_first():
    conn = RecordingConnection(rows=[])
    assert transfers_repo.list_transfers(conn, "tenant-a", 21) == []
    sql, params = conn.statements[0]
    assert "WHERE tenant_id = %s" in sql
    assert "ORDER BY created_at DESC, id DESC LIMIT %s" in sql
    assert "OFFSET" not in sql
    assert "(created_at, id) <" not in sql
    assert params == ("tenant-a", 21)


def test_list_transfers_with_cursor_is_strictly_after():
    created = datetime(2024, 1, 1, tzinfo=timezone.utc)
    conn = RecordingConnection(rows=[])
    transfers_repo.list_transfers(conn, "tenant-a", 5, after=(created, "t9"))
    sql, params = conn.statements[0]
    assert "(created_at, id) < (%s, %s)" in sql
    assert params == ("tenant-a", created, "t9", 5)


def test_insert_movement_records_reason_and_transfer_reference():
    conn = RecordingConnection()
    transfers_repo.insert_movement(
        conn,
        "tenant-a",
        "WIDGET",
        "w1",
        -2,
        transfers_repo.MOVEMENT_TRANSFER_OUT,
        "t1",
    )
    sql, params = conn.statements[0]
    assert sql.startswith("INSERT INTO movements")
    assert params == ("tenant-a", "WIDGET", "w1", -2, "transfer_out", "t1")
    _assert_caller_owns_conn(conn)


def test_functions_compose_on_one_connection():
    conn = RecordingConnection(rowcount=1, rows=[{"id": "t1"}])
    assert transfers_repo.decrement_stock(conn, "tenant-a", "w1", "WIDGET", 1)
    transfers_repo.increment_stock(conn, "tenant-a", "w2", "WIDGET", 1)
    transfers_repo.insert_transfer(conn, "tenant-a", "WIDGET", "w1", "w2", 1)
    transfers_repo.insert_movement(
        conn, "tenant-a", "WIDGET", "w1", -1, "transfer_out", "t1"
    )
    transfers_repo.insert_movement(
        conn, "tenant-a", "WIDGET", "w2", 1, "transfer_in", "t1"
    )
    assert len(conn.statements) == 5
    _assert_caller_owns_conn(conn)
