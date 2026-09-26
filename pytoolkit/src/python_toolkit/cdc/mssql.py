"""SQL Server CDC functions for Kestra workflows.

Kestra usage::

    from python_toolkit.cdc.mssql import MssqlCdc

    result = MssqlCdc.get_next_lsn(
        "{{ envs.sql_conn_master }}",
        database_name=config["database_name"],
        capture_instance=config["capture_name"],
        last_start_lsn=config.get("last_start_lsn"),
        last_seqval=config.get("last_seqval"),
    )

The connection string is generic and may point to the master database. The target database
is passed to each CDC method::

    server=host;port=1433;user=sa;password=secret;database=master

Supported keys: ``server`` (or ``host``, ``data source``), ``port``, ``user`` (or ``uid``),
``password`` (or ``pwd``), optional ``database`` (or ``initial catalog``).

KV watermark: 20-char hex **without** ``0x`` — ``last_start_lsn`` and ``last_seqval`` from the
last fetched row. Omit both on first run (uses ``fn_cdc_get_min_lsn``). Batched reads filter
``__$seqval`` in SQL because ``fn_cdc_get_all_changes`` ranges on ``__$start_lsn`` only.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Generator

__all__ = ["MssqlCdc"]

_CAPTURE_INSTANCE_RE = re.compile(r"^[A-Za-z0-9_.]+$")
_ZERO_LSN_STORAGE = (b"\x00" * 10).hex()


def _parse_conn_str(conn_str: str) -> dict[str, Any]:
    """Parse a semicolon-delimited SQL Server connection string."""
    if not conn_str or not conn_str.strip():
        raise ValueError("conn_str must not be empty")

    parts: dict[str, str] = {}
    for segment in conn_str.split(";"):
        segment = segment.strip()
        if not segment or "=" not in segment:
            continue
        key, value = segment.split("=", 1)
        parts[key.strip().lower()] = value.strip()

    server = parts.get("server") or parts.get("host") or parts.get("data source")
    user = parts.get("user") or parts.get("uid")
    password = parts.get("password") or parts.get("pwd")
    database = parts.get("database") or parts.get("initial catalog")
    port_raw = parts.get("port")

    if not server:
        raise ValueError("conn_str must include server")
    if not user:
        raise ValueError("conn_str must include user")
    if password is None:
        raise ValueError("conn_str must include password")
    connect_kwargs: dict[str, Any] = {
        "server": server,
        "user": user,
        "password": password,
    }
    if database:
        connect_kwargs["database"] = database
    if port_raw:
        connect_kwargs["port"] = int(port_raw)

    return connect_kwargs


@contextmanager
def _connect(conn_str: str, database_name: str) -> Generator[Any, None, None]:
    """Open a pymssql connection as a context manager."""
    try:
        import pymssql
    except ImportError as exc:
        raise ImportError(
            "pymssql is required for SQL Server support. Install with: pip install pymssql"
        ) from exc

    connect_kwargs = _parse_conn_str(conn_str)
    connect_kwargs["database"] = _validate_identifier(database_name, "database_name")
    conn = pymssql.connect(**connect_kwargs)
    try:
        yield conn
    finally:
        conn.close()


def _lsn_to_storage_hex(value: Any) -> str:
    """Convert a SQL Server LSN to 20-char hex without 0x (KV format)."""
    if value is None:
        raise ValueError("LSN value must not be None")

    if isinstance(value, str):
        stripped = value.strip().lower()
        if stripped.startswith("0x"):
            stripped = stripped[2:]
        if not stripped:
            return _ZERO_LSN_STORAGE
        if not re.fullmatch(r"[0-9a-f]+", stripped):
            raise ValueError(f"LSN hex string contains invalid characters: {value!r}")
        try:
            # Canonical 10-byte form — always 20 hex chars; not a second hex encode.
            return int(stripped, 16).to_bytes(10, "big").hex()
        except OverflowError as exc:
            raise ValueError(f"LSN hex exceeds 10 bytes: {value!r}") from exc

    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()

    raise TypeError(f"Unsupported LSN value type: {type(value)!r}")


def _hex_to_bytes(hex_lsn: str) -> bytes:
    """Convert a storage or SQL hex LSN string to bytes."""
    return bytes.fromhex(_lsn_to_storage_hex(hex_lsn))


def _compare_lsn_values(left: Any, right: Any) -> int:
    """Compare two LSN values. Returns -1, 0, or 1."""
    left_bytes = _hex_to_bytes(_lsn_to_storage_hex(left))
    right_bytes = _hex_to_bytes(_lsn_to_storage_hex(right))
    if left_bytes < right_bytes:
        return -1
    if left_bytes > right_bytes:
        return 1
    return 0


def _normalize_kv_lsn(last_lsn: str | None) -> str | None:
    """Treat empty/null KV values as missing."""
    if last_lsn is None:
        return None
    stripped = str(last_lsn).strip()
    if not stripped or stripped.lower() == "null":
        return None
    return _lsn_to_storage_hex(stripped)


def _validate_capture_instance(capture_instance: str) -> str:
    """Validate capture instance name for safe use in CDC function names."""
    capture_instance = capture_instance.strip()
    if not capture_instance:
        raise ValueError("capture_instance must not be empty")
    if not _CAPTURE_INSTANCE_RE.fullmatch(capture_instance):
        raise ValueError(
            "capture_instance must contain only letters, numbers, underscores, and dots"
        )
    return capture_instance


def _validate_identifier(value: str, name: str) -> str:
    """Validate a database, schema, or table identifier used by the API."""
    value = value.strip()
    if not value:
        raise ValueError(f"{name} must not be empty")
    if not _CAPTURE_INSTANCE_RE.fullmatch(value):
        raise ValueError(
            f"{name} must contain only letters, numbers, underscores, and dots"
        )
    return value


def _resolve_capture_instance(
    schema_name: str | None,
    table_name: str | None,
    capture_instance: str | None,
) -> str:
    """Use the standard CDC capture name unless a custom name is supplied."""
    if capture_instance:
        return _validate_capture_instance(capture_instance)
    if schema_name is None or table_name is None:
        raise ValueError(
            "capture_instance is required when schema_name and table_name are not supplied"
        )
    schema_name = _validate_identifier(schema_name, "schema_name")
    table_name = _validate_identifier(table_name, "table_name")
    return _validate_capture_instance(f"{schema_name}_{table_name}")


def _lsn_sql_literal(storage_lsn: str) -> str:
    return "0x" + _lsn_to_storage_hex(storage_lsn)


def _serialize_value(value: Any) -> Any:
    """Convert a SQL row value to a JSON-friendly Python value."""
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "0x" + bytes(value).hex()
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def _serialize_row(row: dict[str, Any]) -> dict[str, Any]:
    return {key: _serialize_value(value) for key, value in row.items()}


class MssqlCdc:
    """SQL Server CDC helpers."""

    @staticmethod
    def get_lsn_bounds(
        conn_str: str,
        database_name: str,
        capture_instance: str | None = None,
        *,
        schema_name: str | None = None,
        table_name: str | None = None,
    ) -> dict[str, str]:
        """Return the min and max LSN available for a capture instance."""
        database_name = _validate_identifier(database_name, "database_name")
        capture_instance = _resolve_capture_instance(
            schema_name, table_name, capture_instance
        )

        with _connect(conn_str, database_name) as conn:
            min_lsn, max_lsn = MssqlCdc._fetch_lsn_bounds(conn, capture_instance)

        return {
            "capture_instance": capture_instance,
            "min_lsn": min_lsn,
            "max_lsn": max_lsn,
        }

    @staticmethod
    def get_next_lsn(
        conn_str: str,
        database_name: str,
        capture_instance: str | None = None,
        last_start_lsn: str | None = None,
        last_seqval: str | None = None,
        *,
        schema_name: str | None = None,
        table_name: str | None = None,
    ) -> dict[str, Any]:
        """Return from/to LSN values for the next CDC poll.

        First run (no KV watermark): ``from_lsn`` is ``fn_cdc_get_min_lsn``, no seqval filter.
        Later runs: ``from_lsn`` is ``last_start_lsn`` (or min if missing) and
        ``after_seqval`` is ``last_seqval`` for the batched fetch filter.
        ``to_lsn`` is always ``fn_cdc_get_max_lsn()``.
        """
        database_name = _validate_identifier(database_name, "database_name")
        capture_instance = _resolve_capture_instance(
            schema_name, table_name, capture_instance
        )
        normalized_start = _normalize_kv_lsn(last_start_lsn)
        normalized_seqval = _normalize_kv_lsn(last_seqval)
        is_first_run = normalized_seqval is None

        with _connect(conn_str, database_name) as conn:
            if is_first_run:
                from_lsn = MssqlCdc._fetch_min_lsn(conn, capture_instance)
                from_lsn_hex = _lsn_to_storage_hex(from_lsn)
                after_seqval = None
            else:
                if normalized_start:
                    from_lsn_hex = normalized_start
                else:
                    from_lsn = MssqlCdc._fetch_min_lsn(conn, capture_instance)
                    from_lsn_hex = _lsn_to_storage_hex(from_lsn)
                after_seqval = normalized_seqval
            to_lsn = MssqlCdc._fetch_max_lsn(conn)

        has_changes = (
            to_lsn is not None
            and _compare_lsn_values(from_lsn_hex, to_lsn) <= 0
        )

        return {
            "capture_instance": capture_instance,
            "last_start_lsn": normalized_start,
            "last_seqval": normalized_seqval,
            "from_lsn": from_lsn_hex,
            "after_seqval": after_seqval,
            "to_lsn": _lsn_to_storage_hex(to_lsn),
            "is_first_run": is_first_run,
            "has_changes": has_changes,
        }

    @staticmethod
    def fetch_changes(
        conn_str: str,
        database_name: str,
        capture_instance: str | None,
        from_lsn: str,
        to_lsn: str,
        *,
        after_seqval: str | None = None,
        batch_size: int = 500,
        schema_name: str | None = None,
        table_name: str | None = None,
    ) -> dict[str, Any]:
        """Fetch CDC change rows between two LSN values."""
        database_name = _validate_identifier(database_name, "database_name")
        capture_instance = _resolve_capture_instance(
            schema_name, table_name, capture_instance
        )
        from_lsn = _lsn_to_storage_hex(from_lsn)
        to_lsn = _lsn_to_storage_hex(to_lsn)
        after_seqval = _normalize_kv_lsn(after_seqval)

        if batch_size <= 0:
            raise ValueError("batch_size must be greater than 0")

        with _connect(conn_str, database_name) as conn:
            rows = MssqlCdc._fetch_change_rows(
                conn,
                capture_instance,
                from_lsn,
                to_lsn,
                batch_size,
                after_seqval=after_seqval,
            )

        commit_position = (
            MssqlCdc._commit_position_from_rows(rows) if rows else None
        )
        serialized_rows = [_serialize_row(row) for row in rows]
        row_count = len(serialized_rows)
        print(f"Fetched {row_count} changed row(s).")

        result: dict[str, Any] = {
            "capture_name": capture_instance,
            "from_lsn": from_lsn,
            "to_lsn": to_lsn,
            "after_seqval": after_seqval,
            "row_count": row_count,
            "should_persist": row_count > 0,
            "rows": serialized_rows,
        }
        if commit_position:
            result.update(commit_position)
        return result

    @staticmethod
    def build_kv_config(
        capture_name: str,
        last_start_lsn: str,
        last_seqval: str,
    ) -> dict[str, str]:
        """Build the KV config object stored in Kestra."""
        return {
            "capture_name": _validate_capture_instance(capture_name),
            "last_start_lsn": _lsn_to_storage_hex(last_start_lsn),
            "last_seqval": _lsn_to_storage_hex(last_seqval),
        }

    @staticmethod
    def _last_processed_row(rows: list[dict[str, Any]]) -> dict[str, Any]:
        """Return the last CDC row in SQL Server sort order."""
        return max(
            rows,
            key=lambda row: (
                _hex_to_bytes(_lsn_to_storage_hex(row["__$start_lsn"])),
                _hex_to_bytes(_lsn_to_storage_hex(row["__$seqval"])),
            ),
        )

    @staticmethod
    def _commit_position_from_rows(rows: list[dict[str, Any]]) -> dict[str, str]:
        """Return the last row's start_lsn and seqval for the KV watermark."""
        last_row = MssqlCdc._last_processed_row(rows)
        return {
            "last_start_lsn": _lsn_to_storage_hex(last_row["__$start_lsn"]),
            "last_seqval": _lsn_to_storage_hex(last_row["__$seqval"]),
        }

    @staticmethod
    def _fetch_lsn_bounds(conn: Any, capture_instance: str) -> tuple[str, str]:
        cursor = conn.cursor(as_dict=True)
        cursor.execute(
            """
            SELECT
                sys.fn_cdc_get_min_lsn(%s) AS min_lsn,
                sys.fn_cdc_get_max_lsn() AS max_lsn
            """,
            (capture_instance,),
        )
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError("Failed to read LSN bounds from SQL Server")

        return _lsn_to_storage_hex(row["min_lsn"]), _lsn_to_storage_hex(row["max_lsn"])

    @staticmethod
    def _fetch_change_rows(
        conn: Any,
        capture_instance: str,
        from_lsn: str,
        to_lsn: str,
        batch_size: int,
        *,
        after_seqval: str | None = None,
    ) -> list[dict[str, Any]]:
        capture_instance = _validate_capture_instance(capture_instance)
        cursor = conn.cursor(as_dict=True)

        after_seqval_sql = (
            f"CONVERT(binary(10), '{after_seqval}', 2)"
            if after_seqval
            else "NULL"
        )

        # fn_cdc_get_all_changes ranges on __$start_lsn; filter __$seqval for batched reads.
        sql = f"""
            DECLARE @from_lsn binary(10) = CONVERT(binary(10), '{from_lsn}', 2);
            DECLARE @to_lsn   binary(10) = CONVERT(binary(10), '{to_lsn}', 2);
            DECLARE @filter   nvarchar(30) = N'all';
            DECLARE @after_seqval binary(10) = {after_seqval_sql};

            SELECT TOP ({batch_size}) *
            FROM (
                SELECT *
                FROM cdc.[fn_cdc_get_all_changes_{capture_instance}](
                    @from_lsn, @to_lsn, @filter
                )
            ) AS changes
            WHERE @after_seqval IS NULL OR changes.[__$seqval] > @after_seqval
            ORDER BY changes.[__$start_lsn], changes.[__$seqval];
            """
        cursor.execute(sql)
        rows = cursor.fetchall()
        return list(rows or [])

    @staticmethod
    def _fetch_min_lsn(conn: Any, capture_instance: str) -> Any:
        cursor = conn.cursor(as_dict=True)
        cursor.execute(
            "SELECT sys.fn_cdc_get_min_lsn(%s) AS from_lsn",
            (capture_instance,),
        )
        row = cursor.fetchone()
        if row is None or row["from_lsn"] is None:
            raise RuntimeError(
                f"No min LSN for capture instance {capture_instance!r}"
            )
        return row["from_lsn"]

    @staticmethod
    def _fetch_incremented_lsn(conn: Any, storage_lsn: str) -> Any:
        cursor = conn.cursor(as_dict=True)
        sql_literal = _lsn_sql_literal(storage_lsn)
        cursor.execute(f"SELECT sys.fn_cdc_increment_lsn({sql_literal}) AS from_lsn")
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError("Failed to increment LSN")
        return row["from_lsn"]

    @staticmethod
    def _fetch_max_lsn(conn: Any) -> Any:
        cursor = conn.cursor(as_dict=True)
        cursor.execute("SELECT sys.fn_cdc_get_max_lsn() AS to_lsn")
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError("Failed to read max LSN")
        return row["to_lsn"]
