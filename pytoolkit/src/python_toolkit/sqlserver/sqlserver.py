"""SQL Server connection, query, batching, and MERGE helpers.

Kestra usage::

    from python_toolkit.sqlserver.sqlserver import SqlServer
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Generator, Iterable, Optional, Sequence, TypeVar

from python_toolkit.common.validation import Validation

T = TypeVar("T")


class SqlServer:
    """SQL Server helpers with a single public entry point."""

    @staticmethod
    def connection_string(
        *,
        server: str,
        database: str,
        username: Optional[str] = None,
        password: Optional[str] = None,
        driver: str = "ODBC Driver 18 for SQL Server",
        trust_server_certificate: bool = True,
        extra: Optional[str] = None,
    ) -> str:
        """Build a pyodbc connection string."""
        Validation.require_non_empty(server, "server")
        Validation.require_non_empty(database, "database")

        parts = [
            f"DRIVER={{{driver}}}",
            f"SERVER={server}",
            f"DATABASE={database}",
        ]

        if username and password:
            parts.extend([f"UID={username}", f"PWD={password}"])
        else:
            parts.append("Trusted_Connection=yes")

        if trust_server_certificate:
            parts.append("TrustServerCertificate=yes")

        if extra:
            parts.append(extra)

        return ";".join(parts)

    @staticmethod
    @contextmanager
    def connect(conn_str: str, **kwargs: Any) -> Generator[Any, None, None]:
        """Open a pyodbc connection as a context manager."""
        pyodbc = SqlServer._load_pyodbc()
        conn = pyodbc.connect(conn_str, **kwargs)
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def execute(
        connection_or_cursor: Any,
        sql: str,
        params: Optional[Sequence[Any]] = None,
    ) -> None:
        """Execute a SQL statement and close an internally created cursor."""
        with SqlServer._cursor(connection_or_cursor) as cursor:
            SqlServer._execute(cursor, sql, params)

    @staticmethod
    def fetch_all(
        connection_or_cursor: Any,
        sql: str,
        params: Optional[Sequence[Any]] = None,
    ) -> list[dict[str, Any]]:
        """Execute a query and return rows as dictionaries."""
        with SqlServer._cursor(connection_or_cursor) as cursor:
            SqlServer._execute(cursor, sql, params)
            columns = [column[0] for column in cursor.description or []]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]

    @staticmethod
    def fetch_in_batches(
        connection_or_cursor: Any,
        sql: str,
        *,
        batch_size: int = 1000,
        params: Optional[Sequence[Any]] = None,
    ) -> Iterable[list[dict[str, Any]]]:
        """Stream query results in batches as dictionaries."""
        with SqlServer._cursor(connection_or_cursor) as cursor:
            SqlServer._execute(cursor, sql, params)
            columns = [column[0] for column in cursor.description or []]

            while True:
                rows = cursor.fetchmany(batch_size)
                if not rows:
                    break
                yield [dict(zip(columns, row)) for row in rows]

    @staticmethod
    def iter_batches(
        items: Iterable[T],
        batch_size: int,
    ) -> Generator[list[T], None, None]:
        """Yield successive fixed-size batches from an iterable."""
        if batch_size <= 0:
            raise ValueError("batch_size must be greater than 0")

        batch: list[T] = []
        for item in items:
            batch.append(item)
            if len(batch) >= batch_size:
                yield batch
                batch = []

        if batch:
            yield batch

    @staticmethod
    def build_merge_sql(
        *,
        target_table: str,
        source_table: str,
        key_columns: Sequence[str],
        update_columns: Sequence[str],
        insert_columns: Sequence[str] | None = None,
    ) -> str:
        """Build a T-SQL MERGE statement for an upsert operation."""
        if not key_columns:
            raise ValueError("key_columns must not be empty")

        insert_columns = list(insert_columns or update_columns)
        on_clause = " AND ".join(
            f"target.[{column}] = source.[{column}]" for column in key_columns
        )
        update_set = ", ".join(
            f"target.[{column}] = source.[{column}]" for column in update_columns
        )
        insert_cols = ", ".join(f"[{column}]" for column in insert_columns)
        insert_vals = ", ".join(f"source.[{column}]" for column in insert_columns)

        return f"""
MERGE INTO {target_table} AS target
USING {source_table} AS source
ON {on_clause}
WHEN MATCHED THEN
    UPDATE SET {update_set}
WHEN NOT MATCHED THEN
    INSERT ({insert_cols})
    VALUES ({insert_vals});
""".strip()

    @staticmethod
    @contextmanager
    def _cursor(connection_or_cursor: Any) -> Generator[Any, None, None]:
        """Yield a cursor and close it only when the library created it."""
        if SqlServer._is_cursor(connection_or_cursor):
            yield connection_or_cursor
            return

        cursor = connection_or_cursor.cursor()
        try:
            yield cursor
        finally:
            cursor.close()

    @staticmethod
    def _is_cursor(value: Any) -> bool:
        """Return whether a value exposes the cursor execution interface."""
        return hasattr(value, "execute")

    @staticmethod
    def _execute(
        cursor: Any,
        sql: str,
        params: Optional[Sequence[Any]] = None,
    ) -> None:
        """Execute SQL on an already-owned cursor."""
        if params:
            cursor.execute(sql, params)
        else:
            cursor.execute(sql)

    @staticmethod
    def _load_pyodbc() -> Any:
        """Load the optional pyodbc dependency only when a connection is opened."""
        try:
            import pyodbc
        except ImportError as exc:
            raise ImportError(
                "pyodbc is required for SQL Server support. Install with: pip install pyodbc"
            ) from exc
        return pyodbc
