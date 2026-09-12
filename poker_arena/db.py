from __future__ import annotations

import os
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

SCHEMA = Path(__file__).with_name("schema.sql")


def connect(url: str | None = None) -> psycopg.Connection:
    return psycopg.connect(url or os.environ["ARENA_DATABASE_URL"], row_factory=dict_row)


def init_schema(conn: psycopg.Connection) -> None:
    conn.execute(SCHEMA.read_text())
    conn.commit()
