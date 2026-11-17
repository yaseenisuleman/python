"""SQL Server to SQL Server data transfer helpers.

Kestra usage::

    from python_toolkit.sqlserver.transfer import SqlServerTransfer
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import date, datetime, time
from decimal import Decimal
from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import Any, Optional

from python_toolkit.common.dates import Dates
from python_toolkit.common.logging import ToolkitLogger
from python_toolkit.common.validation import Validation
from python_toolkit.sqlserver.sqlserver import SqlServer

# SQL Server allows at most 1000 rows in one INSERT ... VALUES statement.
_MAX_ROWS_PER_INSERT = 1000

_COLUMN_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")


def _json_value(value: Any) -> Any:
    """Convert a SQL value to a JSON-friendly Python value."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "0x" + bytes(value).hex()
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, (Decimal, uuid.UUID)):
        return str(value)
    return value


def _json_default(value: Any) -> Any:
    """``json.dumps`` fallback: the ``_json_value`` conversions, else the usual ``TypeError``."""
    converted = _json_value(value)
    if converted is value:
        raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")
    return converted


def _column_name(column: str, name: str) -> str:
    """Validate a column name (letters, digits and ``_``) and return it wrapped in ``[ ]``."""
    column = Validation.require_non_empty(column, name)
    if not _COLUMN_NAME_RE.fullmatch(column):
        raise ValueError(f"{name} must contain only letters, numbers and underscores")
    return f"[{column}]"


def _param_values(params: Any) -> tuple[Any, ...]:
    """Turn ``params`` into a tuple: a list or tuple as-is, any other value as one parameter."""
    if params is None or isinstance(params, (list, tuple)):
        return tuple(params or ())
    return (params,)


def _insert_rows(
    cursor: Any,
    destination_table: str,
    rows: Sequence[Sequence[Any]],
    date_created: datetime,
    process_guid: str,
) -> int:
    """Insert rows positionally, each followed by ``date_created`` and ``process_guid``.

    Sends multi-row ``INSERT INTO <table> VALUES (...), ...`` statements of up to
    ``_MAX_ROWS_PER_INSERT`` rows. Does not commit; returns the number of rows inserted.
    """
    batch = [(*row, date_created, process_guid) for row in rows]
    for chunk in SqlServer.iter_batches(batch, _MAX_ROWS_PER_INSERT):
        insert_sql = f"INSERT INTO {destination_table} VALUES " + ", ".join(
            "(" + ", ".join(["%s"] * len(row)) + ")" for row in chunk
        )
        cursor.execute(insert_sql, tuple(value for row in chunk for value in row))
    return len(batch)


def _bulk_value(value: Any) -> Any:
    """Convert a value pymssql's bulk copy cannot bind into one it can.

    Bulk copy binds by Python type and accepts ``str``, ``int``, ``float``, ``bool``,
    ``Decimal``, ``datetime``, ``bytes``/``bytearray`` and ``None``. A ``uuid.UUID`` becomes
    its string, a ``date`` or ``time`` its ISO string, and a ``memoryview`` ``bytes``;
    SQL Server converts them to the column's type.
    """
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, memoryview):
        return bytes(value)
    return value


def _bulk_copy_rows(
    conn: Any,
    destination_table: str,
    rows: Iterable[Sequence[Any]],
    date_created: datetime,
    process_guid: str,
    batch_size: int,
) -> int:
    """Bulk copy rows positionally, each followed by ``date_created`` and ``process_guid``.

    Uses pymssql's ``Connection.bulk_copy`` (one ``INSERT BULK`` stream, ``batch_size``
    rows per batch). ``rows`` may be any iterable; it is read once, as it is sent.
    Does not commit; returns the number of rows sent.
    """
    row_count = 0

    def elements() -> Iterator[tuple[Any, ...]]:
        nonlocal row_count
        for row in rows:
            row_count += 1
            yield (*(_bulk_value(value) for value in row), date_created, process_guid)

    conn.bulk_copy(destination_table, elements(), batch_size=batch_size)
    return row_count


def _rollback_quietly(conn: Any, logger: Any) -> None:
    """Roll back, logging (not raising) a failure, so the original error is the one raised."""
    try:
        conn.rollback()
    except Exception as exc:  # the connection may be unusable after a failed bulk copy
        logger.warning(f"Rollback after failed bulk copy also failed: {type(exc).__name__}: {exc}")


def _delete_process_rows(
    conn_str: str,
    destination_table: str,
    process_id_column: str,
    process_guid: str,
    logger: Any,
) -> None:
    """After a failed bulk copy, delete any rows it left behind, on a new connection.

    Logs (does not raise) a failure, so the caller can re-raise the original error.
    """
    try:
        with SqlServer.connect(conn_str) as conn:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    f"DELETE FROM {destination_table} WHERE {process_id_column} = %s",
                    (process_guid,),
                )
                deleted = cursor.rowcount
                conn.commit()
            finally:
                cursor.close()
        logger.warning(
            f"Bulk copy failed; deleted {deleted} rows from {destination_table} "
            f"where {process_id_column} = {process_guid}"
        )
    except Exception as exc:
        logger.error(
            f"Bulk copy failed and the cleanup delete from {destination_table} "
            f"(process {process_guid}) also failed: {type(exc).__name__}: {exc}"
        )


def _record_values(record: Any, index: int) -> tuple[Any, ...]:
    """Return a record's values: a mapping's values in order, or a list/tuple as-is."""
    if isinstance(record, Mapping):
        return tuple(record.values())
    if isinstance(record, Sequence) and not isinstance(record, (str, bytes, bytearray)):
        return tuple(record)
    raise ValueError(f"Record at index {index} must be a dict, list or tuple")


class SqlServerTransfer:
    """Move data between SQL Server databases."""

    @staticmethod
    def sql_sp_to_sql_table(
        source_conn_str: str,
        destination_conn_str: str,
        sql_query: str,
        destination_table: str,
        *,
        process_guid: Optional[str] = None,
        batch_size: int = 1000,
        bulk_copy: bool = False,
        process_id_column: Optional[str] = "ProcessId",
    ) -> dict[str, Any]:
        """Run a stored procedure (or query) on the source and insert its rows into a table.

        Each row gets two trailing columns: the UTC load time and ``process_guid``
        (a new UUID when not supplied). The insert is positional, so the destination
        table's columns must match the query's columns followed by those two.
        Rows are streamed in batches and committed in a single transaction. Each batch is
        sent as multi-row ``INSERT ... VALUES`` statements of up to 1000 rows.

        With ``bulk_copy=True`` the rows are streamed into the table with pymssql's bulk
        copy (``INSERT BULK``, ``batch_size`` rows per batch) instead, which is much faster
        for wide tables or many rows; see ``records_to_sql_table`` for how a failure is
        cleaned up with ``process_id_column``.
        """
        Validation.require_non_empty(sql_query, "sql_query")
        destination_table = SqlServer.validate_table_name(destination_table, "destination_table")
        if batch_size <= 0:
            raise ValueError("batch_size must be greater than 0")
        if bulk_copy and process_id_column is not None:
            process_id_column = _column_name(process_id_column, "process_id_column")

        logger = ToolkitLogger.get_logger("SqlServerTransfer")
        process_guid = (process_guid or "").strip() or str(uuid.uuid4())
        date_created = Dates.utc_now().replace(tzinfo=None)
        row_count = 0

        if bulk_copy:
            try:
                with SqlServer.connect(source_conn_str) as source_conn, \
                        SqlServer.connect(destination_conn_str) as dest_conn:
                    source_cursor = source_conn.cursor()
                    try:
                        source_cursor.execute(sql_query)

                        def source_rows() -> Iterator[Sequence[Any]]:
                            while True:
                                rows = source_cursor.fetchmany(batch_size)
                                if not rows:
                                    return
                                yield from rows

                        row_count = _bulk_copy_rows(
                            dest_conn, destination_table, source_rows(),
                            date_created, process_guid, batch_size,
                        )
                        dest_conn.commit()
                    except Exception:
                        _rollback_quietly(dest_conn, logger)
                        raise
                    finally:
                        source_cursor.close()
            except Exception:
                if process_id_column is not None:
                    _delete_process_rows(
                        destination_conn_str, destination_table, process_id_column, process_guid, logger
                    )
                raise

            logger.info(f"Inserted {row_count} rows into {destination_table}")
            return {
                "destination_table": destination_table,
                "row_count": row_count,
                "process_guid": process_guid,
                "timestamp": Dates.utc_now().isoformat(),
            }

        with SqlServer.connect(source_conn_str) as source_conn, \
                SqlServer.connect(destination_conn_str) as dest_conn:
            source_cursor = source_conn.cursor()
            dest_cursor = dest_conn.cursor()
            try:
                source_cursor.execute(sql_query)

                while True:
                    rows = source_cursor.fetchmany(batch_size)
                    if not rows:
                        break

                    row_count += _insert_rows(
                        dest_cursor, destination_table, rows, date_created, process_guid
                    )

                dest_conn.commit()
            except Exception:
                dest_conn.rollback()
                raise
            finally:
                source_cursor.close()
                dest_cursor.close()

        logger.info(f"Inserted {row_count} rows into {destination_table}")

        return {
            "destination_table": destination_table,
            "row_count": row_count,
            "process_guid": process_guid,
            "timestamp": Dates.utc_now().isoformat(),
        }

    @staticmethod
    def records_to_sql_table(
        conn_str: str,
        records: Sequence[Mapping[str, Any] | Sequence[Any]],
        destination_table: str,
        *,
        process_guid: Optional[str] = None,
        batch_size: int = 1000,
        bulk_copy: bool = False,
        process_id_column: Optional[str] = "ProcessId",
    ) -> dict[str, Any]:
        """Insert records already in memory into a table, as ``sql_sp_to_sql_table`` does.

        A record is a dict (its values are used in insertion order) or a list/tuple, e.g.
        the rows from ``SqlServer.fetch_all``. Each row gets two trailing columns: the UTC
        load time and ``process_guid`` (a new UUID when empty). The insert is positional,
        so the destination table's columns must be the record's columns followed by those
        two. Rows are sent ``batch_size`` at a time as multi-row ``INSERT ... VALUES``
        statements of up to 1000 rows and committed in one transaction; on error
        everything is rolled back and the error is raised. With no records, the database
        is not contacted.

        With ``bulk_copy=True`` the rows are loaded with pymssql's bulk copy
        (``Connection.bulk_copy``: one ``INSERT BULK`` stream, ``batch_size`` rows per
        batch) instead of ``INSERT ... VALUES``, which is much faster for wide tables or
        many rows. Requires pymssql 2.4.5+ for ``nvarchar`` text to arrive intact. Same row
        shape and commit: pymssql keeps a transaction open, so the load is committed once at
        the end and rolled back on error. As a safeguard, if the load fails, rows whose
        ``process_id_column`` (default ``ProcessId``; letters, digits and ``_``) equals
        ``process_guid`` are then deleted on a new connection before the error is raised,
        so ``process_guid`` must be unique to this load. Pass ``process_id_column=None``
        to skip that delete.
        """
        destination_table = SqlServer.validate_table_name(destination_table, "destination_table")
        if batch_size <= 0:
            raise ValueError("batch_size must be greater than 0")
        if isinstance(records, (str, bytes, bytearray, Mapping)) or not isinstance(records, Sequence):
            raise ValueError("records must be a list of dicts, lists or tuples")
        if bulk_copy and process_id_column is not None:
            process_id_column = _column_name(process_id_column, "process_id_column")
        rows = [_record_values(record, index) for index, record in enumerate(records)]

        logger = ToolkitLogger.get_logger("SqlServerTransfer")
        process_guid = (process_guid or "").strip() or str(uuid.uuid4())
        date_created = Dates.utc_now().replace(tzinfo=None)
        row_count = 0

        if rows and bulk_copy:
            try:
                with SqlServer.connect(conn_str) as conn:
                    try:
                        row_count = _bulk_copy_rows(
                            conn, destination_table, rows, date_created, process_guid, batch_size
                        )
                        conn.commit()
                    except Exception:
                        _rollback_quietly(conn, logger)
                        raise
            except Exception:
                if process_id_column is not None:
                    _delete_process_rows(
                        conn_str, destination_table, process_id_column, process_guid, logger
                    )
                raise
        elif rows:
            with SqlServer.connect(conn_str) as conn:
                cursor = conn.cursor()
                try:
                    for batch in SqlServer.iter_batches(rows, batch_size):
                        row_count += _insert_rows(
                            cursor, destination_table, batch, date_created, process_guid
                        )
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
                finally:
                    cursor.close()

        logger.info(f"Inserted {row_count} rows into {destination_table}")

        return {
            "destination_table": destination_table,
            "row_count": row_count,
            "process_guid": process_guid,
            "timestamp": Dates.utc_now().isoformat(),
        }

    @staticmethod
    def sql_sp_to_json(
        conn_str: str,
        sql_query: str,
        *,
        params: Optional[Any] = None,
        commit: bool = True,
    ) -> dict[str, Any]:
        """Run a stored procedure (or query) and return its rows as JSON-ready records.

        Each record is a dict keyed by the result set's column names. Dates and times
        become ISO 8601 strings, ``Decimal`` and ``uniqueidentifier`` values become
        strings, and binary values become ``0x`` hex strings. Only the first result set
        is returned. ``params`` fill ``%s`` placeholders in ``sql_query``: pass a list or
        tuple, or a single value for one placeholder. With ``commit`` (the default),
        changes made by the procedure are committed once it has finished without error;
        with ``commit=False`` they are rolled back.
        """
        Validation.require_non_empty(sql_query, "sql_query")
        logger = ToolkitLogger.get_logger("SqlServerTransfer")

        with SqlServer.connect(conn_str) as conn:
            columns, records, _ = SqlServerTransfer._run_sql(conn, sql_query, params, fetch=True)
            if columns is None:
                raise ValueError(
                    "sql_query did not return a result set "
                    "(for a stored procedure, add SET NOCOUNT ON)"
                )
            if commit:
                conn.commit()

        logger.info(f"Fetched {len(records)} records")

        return {
            "records": records,
            "row_count": len(records),
            "columns": columns,
            "timestamp": Dates.utc_now().isoformat(),
        }

    @staticmethod
    def sql_sp_execute(
        conn_str: str,
        sql_query: str,
        *,
        params: Optional[Any] = None,
        return_results: bool = False,
        commit: bool = True,
        raise_on_error: bool = True,
    ) -> dict[str, Any]:
        """Run a stored procedure (or any SQL) and report whether it succeeded.

        The procedure is read to the end, so an error raised after earlier statements
        still counts as a failure, and nothing is committed unless it finishes without
        error. ``params`` fill ``%s`` placeholders in ``sql_query`` (a list or tuple, or a
        single value). With ``return_results``, the first result set is returned as
        JSON-ready ``records`` (as in ``sql_sp_to_json``); a procedure with no result
        set gives empty ``records``. With ``commit=False`` changes are rolled back.

        Returns ``success``, ``sql_query``, ``rows_affected``, ``duration_seconds`` and
        ``timestamp``. ``rows_affected`` is the count SQL Server reports for the call, or
        ``None`` when it reports none (e.g. a procedure that uses ``SET NOCOUNT ON``).

        On failure the error is raised, or with ``raise_on_error=False`` returned as
        ``{"success": False, "error": ...}`` after the changes are rolled back.
        """
        Validation.require_non_empty(sql_query, "sql_query")
        logger = ToolkitLogger.get_logger("SqlServerTransfer")
        started_at = Dates.utc_now()

        try:
            with SqlServer.connect(conn_str) as conn:
                columns, records, rows_affected = SqlServerTransfer._run_sql(
                    conn, sql_query, params, fetch=return_results
                )
                if commit:
                    conn.commit()
        except Exception as exc:
            if raise_on_error:
                raise
            error = f"{type(exc).__name__}: {exc}"
            logger.error(f"SQL failed: {error}")
            return {
                "success": False,
                "sql_query": sql_query,
                "error": error,
                "rows_affected": None,
                "duration_seconds": (Dates.utc_now() - started_at).total_seconds(),
                "timestamp": Dates.utc_now().isoformat(),
            }

        logger.info(
            f"SQL executed successfully{' and committed' if commit else ''}; "
            f"rows affected: {rows_affected if rows_affected is not None else 'not reported'}"
        )

        result: dict[str, Any] = {
            "success": True,
            "sql_query": sql_query,
            "rows_affected": rows_affected,
            "duration_seconds": (Dates.utc_now() - started_at).total_seconds(),
            "timestamp": Dates.utc_now().isoformat(),
        }
        if return_results:
            result.update(records=records, row_count=len(records), columns=columns or [])
        return result

    @staticmethod
    def records_to_json_staging(
        conn_str: str,
        records: list[dict[str, Any]],
        destination_table: str,
        *,
        import_id: Optional[str] = None,
        batch_size: int = 500,
        content_column: str = "Content",
        date_column: str = "DtAdded",
        import_id_column: str = "ImportId",
    ) -> dict[str, Any]:
        """Insert records into a JSON staging table, one row per record.

        Each row holds the record as JSON in ``content_column``, ``GETDATE()`` (set by
        the server) in ``date_column`` and ``import_id`` (a new UUID when empty) in
        ``import_id_column``. JSON values are made safe as in ``sql_sp_to_json``: dates
        and times become ISO 8601 strings, ``Decimal`` and UUID values become strings.

        Rows are inserted with named columns, ``batch_size`` rows (at most 1000) per
        ``INSERT ... VALUES`` statement, and committed in one transaction; on error
        everything is rolled back and the error is raised. With no records, the
        database is not contacted.
        """
        destination_table = SqlServer.validate_table_name(destination_table, "destination_table")
        columns = ", ".join(
            [
                _column_name(content_column, "content_column"),
                _column_name(date_column, "date_column"),
                _column_name(import_id_column, "import_id_column"),
            ]
        )
        if (
            not isinstance(batch_size, int)
            or isinstance(batch_size, bool)
            or not 0 < batch_size <= _MAX_ROWS_PER_INSERT
        ):
            raise ValueError(f"batch_size must be between 1 and {_MAX_ROWS_PER_INSERT}")
        if not isinstance(records, list):
            raise ValueError("records must be a list of dicts")
        for index, record in enumerate(records):
            if not isinstance(record, dict):
                raise ValueError(f"Record at index {index} must be a dict")

        logger = ToolkitLogger.get_logger("SqlServerTransfer")
        import_id = (import_id or "").strip() or str(uuid.uuid4())

        if records:
            rows = [
                (json.dumps(record, ensure_ascii=False, default=_json_default), import_id)
                for record in records
            ]
            with SqlServer.connect(conn_str) as conn:
                cursor = conn.cursor()
                try:
                    for chunk in SqlServer.iter_batches(rows, batch_size):
                        insert_sql = (
                            f"INSERT INTO {destination_table} ({columns}) VALUES "
                            + ", ".join(["(%s, GETDATE(), %s)"] * len(chunk))
                        )
                        cursor.execute(insert_sql, tuple(value for row in chunk for value in row))
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
                finally:
                    cursor.close()

        logger.info(f"Inserted {len(records)} rows into {destination_table} (import {import_id})")

        return {
            "destination_table": destination_table,
            "row_count": len(records),
            "import_id": import_id,
            "timestamp": Dates.utc_now().isoformat(),
        }

    @staticmethod
    def _run_sql(
        conn: Any,
        sql_query: str,
        params: Optional[Any],
        *,
        fetch: bool,
    ) -> tuple[Optional[list[str]], list[dict[str, Any]], Optional[int]]:
        """Execute SQL and read it to the end.

        Returns the first result set's columns and records, and the rows-affected count
        the driver reports for the call. ``columns`` is ``None`` when nothing was fetched
        or there was no result set; the count is ``None`` when none was reported.
        """
        cursor = conn.cursor()
        try:
            values = _param_values(params)
            if values:
                cursor.execute(sql_query, values)
            else:
                cursor.execute(sql_query)
            rows_affected = cursor.rowcount if cursor.rowcount >= 0 else None

            columns = None
            records: list[dict[str, Any]] = []
            if fetch and cursor.description:
                columns = [column[0] for column in cursor.description]
                unnamed = [str(index + 1) for index, column in enumerate(columns) if not column]
                if unnamed:
                    raise ValueError(f"Result set columns {', '.join(unnamed)} have no name; alias them in sql_query")
                duplicates = sorted({column for column in columns if columns.count(column) > 1})
                if duplicates:
                    raise ValueError(f"Result set has duplicate column names: {', '.join(duplicates)}")

                records = [
                    {column: _json_value(value) for column, value in zip(columns, row)}
                    for row in cursor.fetchall()
                ]

            # Read to the end so an error raised later in the procedure surfaces before any commit.
            while cursor.nextset():
                pass

            return columns, records, rows_affected
        finally:
            cursor.close()
