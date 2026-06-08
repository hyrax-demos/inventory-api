"""Thin database access helpers."""
import psycopg2

from app import config


def get_connection():
    return psycopg2.connect(
        host=config.DB_HOST,
        user=config.DB_USER,
        password=config.DB_PASSWORD,
        dbname=config.DB_NAME,
    )


def run_query(sql: str):
    """Execute a SQL statement and return all rows."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql)
        try:
            return cur.fetchall()
        except psycopg2.ProgrammingError:
            return []
    finally:
        conn.close()
