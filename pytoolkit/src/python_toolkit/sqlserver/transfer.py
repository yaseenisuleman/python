"""SQL Server to SQL Server data transfer helpers.

Kestra usage::

    from python_toolkit.sqlserver.transfer import SqlServerTransfer
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Optional

from python_toolkit.common.dates import Dates
from python_toolkit.common.logging import ToolkitLogger
from python_toolkit.common.validation import Validation
from python_toolkit.sqlserver.sqlserver import SqlServer

# SQL Server allows at most 1000 rows in one INSERT ... VALUES statement.
_MAX_ROWS_PER_INSERT = 1000


def _json_value(value: Any) -> Any:
    """Convert a SQL value to a JSON-friendly Python value."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "0x" + bytes(value).hex()
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, (Decimal, uuid.UUID)):
        return str(value)
    return value


def _param_values(params: Any) -> tuple[Any, ...]:
    """Turn ``params`` into a tuple: a list or tuple as-is, any other value as one parameter."""
    if params is None or isinstance(params, (list, tuple)):
        return tuple(params or ())
    return (params,)


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
    ) -> dict[str, Any]:
        """Run a stored procedure (or query) on the source and insert its rows into a table.

        Each row gets two trailing columns: the UTC load time and ``process_guid``
        (a new UUID when not supplied). The insert is positional, so the destination
        table's columns must match the query's columns followed by those two.
        Rows are streamed in batches and committed in a single transaction. Each batch is
        sent as multi-row ``INSERT ... VALUES`` statements of up to 1000 rows.
        """
        Validation.require_non_empty(sql_query, "sql_query")
        destination_table = SqlServer.validate_table_name(destination_table, "destination_table")
        if batch_size <= 0:
            raise ValueError("batch_size must be greater than 0")

        logger = ToolkitLogger.get_logger("SqlServerTransfer")
        process_guid = (process_guid or "").strip() or str(uuid.uuid4())
        date_created = Dates.utc_now().replace(tzinfo=None)
        row_count = 0

        with SqlServer.connect(source_conn_str) as source_conn, \
                SqlServer.connect(destination_conn_str) as dest_conn:
            source_cursor = source_conn.cursor()
            dest_cursor = dest_conn.cursor()
            try:
                source_cursor.execute(sql_query)
                row_placeholders = None

                while True:
                    rows = source_cursor.fetchmany(batch_size)
                    if not rows:
                        break

                    batch = [(*row, date_created, process_guid) for row in rows]
                    if row_placeholders is None:
                        row_placeholders = "(" + ", ".join(["%s"] * len(batch[0])) + ")"

                    for chunk in SqlServer.iter_batches(batch, _MAX_ROWS_PER_INSERT):
                        insert_sql = (
                            f"INSERT INTO {destination_table} VALUES "
                            + ", ".join([row_placeholders] * len(chunk))
                        )
                        dest_cursor.execute(insert_sql, tuple(value for row in chunk for value in row))
                    row_count += len(batch)

                dest_conn.commit()
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
