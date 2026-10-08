from __future__ import annotations

from pathlib import Path

from . import config

SCHEMA = (Path(__file__).parent / "schema.sql").read_text()


def connect(dsn: str | None = None, autocommit: bool = False):
    import psycopg
    from psycopg.rows import dict_row

    return psycopg.connect(dsn or config.DATABASE_URL, row_factory=dict_row, autocommit=autocommit)


def migrate(conn) -> None:
    conn.execute(SCHEMA)
    conn.commit()
