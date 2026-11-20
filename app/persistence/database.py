"""Small psycopg pool and migration runner; no ORM is introduced."""

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


class Database:
    def __init__(self, dsn: str, *, migrations_dir: str | Path | None = None, min_size: int = 1, max_size: int = 8):
        self.dsn = dsn
        self.migrations_dir = Path(migrations_dir or Path(__file__).parents[2] / "migrations")
        self.pool = ConnectionPool(conninfo=dsn, min_size=min_size, max_size=max_size,
                                   kwargs={"row_factory": dict_row}, open=False)

    def open(self) -> None:
        self.pool.open(wait=True)

    def close(self) -> None:
        self.pool.close()

    @contextmanager
    def connection(self) -> Iterator[psycopg.Connection]:
        with self.pool.connection() as conn:
            yield conn

    def migrate(self) -> None:
        scripts = sorted(self.migrations_dir.glob("*.sql"))
        with self.connection() as conn:
            # Serialize schema changes for concurrent application starts.
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (7864161251,))
            conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version integer PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
            applied = {row["version"] for row in conn.execute("SELECT version FROM schema_migrations").fetchall()}
            if 1 not in applied:
                existing = conn.execute("SELECT to_regclass('conversations') AS conversations, to_regclass('workflows') AS workflows").fetchone()
                if existing["conversations"] or existing["workflows"]:
                    raise RuntimeError("existing unversioned agent schema requires an explicit migration; refusing to initialize")
            for script in scripts:
                version = int(script.name.split("_", 1)[0])
                if version in applied:
                    continue
                conn.execute(script.read_text(encoding="utf-8"))
                conn.execute("INSERT INTO schema_migrations(version) VALUES (%s)", (version,))
            conn.commit()

    @contextmanager
    def transaction(self) -> Iterator[psycopg.Connection]:
        with self.connection() as conn:
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
