"""SQL Server connection, query, batching, and MERGE helpers.

Kestra usage::

    from python_toolkit.sqlserver.sqlserver import SqlServer
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from typing import Any, Generator, Iterable, Optional, Sequence, TypeVar

from python_toolkit.common.validation import Validation

T = TypeVar("T")

_TABLE_NAME_RE = re.compile(r"^[A-Za-z0-9_.\[\] ]+$")


class SqlServer:
    """SQL Server helpers with a single public entry point."""

    @staticmethod
    def connection_string(
        *,
        server: str,
        database: str,
        username: Optional[str] = None,
        password: Optional[str] = None,
        port: Optional[int] = None,
    ) -> str:
        """Build a ``server=;port=;user=;password=;database=`` connection string."""
        Validation.require_non_empty(server, "server")
        Validation.require_non_empty(database, "database")

        parts = [f"server={server}"]
        if port:
            parts.append(f"port={port}")
        if username and password:
            parts.extend([f"user={username}", f"password={password}"])
        parts.append(f"database={database}")

        return ";".join(parts)

    @staticmethod
    def connection_kwargs(conn_str: str) -> dict[str, Any]:
        """Parse a connection string into ``pymssql.connect`` keyword arguments.

        Accepts ``server=;port=;user=;password=;database=`` strings and ODBC-style
        ``SERVER=host,port;UID=;PWD=;DATABASE=`` strings (``DRIVER`` and other keys are ignored).
        Without a user, pymssql uses integrated (Windows/Kerberos) authentication.
        """
        Validation.require_non_empty(conn_str, "conn_str")

        parts: dict[str, str] = {}
        for segment in conn_str.split(";"):
            if "=" in segment:
                key, value = segment.split("=", 1)
                parts[key.strip().lower()] = value.strip()

        server = parts.get("server") or parts.get("host") or parts.get("data source") or ""
        server = Validation.require_non_empty(server.removeprefix("tcp:"), "server")
        port = parts.get("port")
        if "," in server:
            server, port = (value.strip() for value in server.split(",", 1))

        kwargs: dict[str, Any] = {"server": server}
        if port:
            kwargs["port"] = port
        user = parts.get("user") or parts.get("uid")
        if user:
            kwargs["user"] = user
            kwargs["password"] = parts.get("password") or parts.get("pwd") or ""
        database = parts.get("database") or parts.get("initial catalog")
        if database:
            kwargs["database"] = database

        return kwargs

    @staticmethod
    def validate_table_name(table_name: str, name: str = "table_name") -> str:
        """Validate a table name before it is placed into SQL text."""
        table_name = Validation.require_non_empty(table_name, name)
        if not _TABLE_NAME_RE.fullmatch(table_name):
            raise ValueError(
                f"{name} must contain only letters, numbers, underscores, "
                "dots, spaces, and square brackets"
            )
        return table_name

    @staticmethod
    @contextmanager
    def connect(conn_str: str, **kwargs: Any) -> Generator[Any, None, None]:
        """Open a pymssql connection as a context manager.

        ``kwargs`` are passed to ``pymssql.connect`` and override values from ``conn_str``.
        """
        pymssql = SqlServer._load_pymssql()
        conn = pymssql.connect(**{**SqlServer.connection_kwargs(conn_str), **kwargs})
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
            cursor.execute(sql, tuple(params))
        else:
            cursor.execute(sql)

    @staticmethod
    def _load_pymssql() -> Any:
        """Load the optional pymssql dependency only when a connection is opened."""
        try:
            import pymssql
        except ImportError as exc:
            raise ImportError(
                "pymssql is required for SQL Server support. Install with: pip install pymssql"
            ) from exc
        return pymssql
