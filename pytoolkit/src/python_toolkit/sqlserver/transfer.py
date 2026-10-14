"""SQL Server to SQL Server data transfer helpers.

Kestra usage::

    from python_toolkit.sqlserver.transfer import SqlServerTransfer
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Optional, Sequence

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
        params: Optional[Sequence[Any]] = None,
    ) -> dict[str, Any]:
        """Run a stored procedure (or query) and return its rows as JSON-ready records.

        Each record is a dict keyed by the result set's column names. Dates and times
        become ISO 8601 strings, ``Decimal`` and ``uniqueidentifier`` values become
        strings, and binary values become ``0x`` hex strings. Only the first result set
        is read. ``params`` fill ``%s`` placeholders in ``sql_query``.
        """
        Validation.require_non_empty(sql_query, "sql_query")
        logger = ToolkitLogger.get_logger("SqlServerTransfer")

        with SqlServer.connect(conn_str) as conn:
            cursor = conn.cursor()
            try:
                if params:
                    cursor.execute(sql_query, tuple(params))
                else:
                    cursor.execute(sql_query)
                if not cursor.description:
                    raise ValueError(
                        "sql_query did not return a result set "
                        "(for a stored procedure, add SET NOCOUNT ON)"
                    )

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
            finally:
                cursor.close()

        logger.info(f"Fetched {len(records)} records")

        return {
            "records": records,
            "row_count": len(records),
            "columns": columns,
            "timestamp": Dates.utc_now().isoformat(),
        }
