"""Thin database access helpers.

Connections are short-lived: each helper opens, runs, and closes its own
connection. All SQL is parameterized -- callers pass values via ``params``,
never via string interpolation.
"""

from contextlib import contextmanager

import psycopg2
import psycopg2.extras

from app import config


def get_connection():
    return psycopg2.connect(
        host=config.DB_HOST,
        user=config.DB_USER,
        password=config.DB_PASSWORD,
        dbname=config.DB_NAME,
    )


@contextmanager
def transaction():
    """Yield a connection inside a single committed transaction."""
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def fetch_all(sql: str, params: tuple = ()):
    """Run a SELECT and return a list of dict rows."""
    conn = get_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params)
        return cur.fetchall()
    finally:
        conn.close()


def fetch_one(sql: str, params: tuple = ()):
    """Run a SELECT and return the first dict row, or None."""
    rows = fetch_all(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: tuple = ()):
    """Run a write statement in its own transaction; return affected rowcount."""
    with transaction() as conn:
        cur = conn.cursor()
        cur.execute(sql, params)
        return cur.rowcount
