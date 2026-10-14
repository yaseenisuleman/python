"""SQL Server to SQL Server data transfer helpers.

Kestra usage::

    from python_toolkit.sqlserver.transfer import SqlServerTransfer
"""

from __future__ import annotations

import uuid
from typing import Any, Optional

from python_toolkit.common.dates import Dates
from python_toolkit.common.logging import ToolkitLogger
from python_toolkit.common.validation import Validation
from python_toolkit.sqlserver.sqlserver import SqlServer

# SQL Server allows at most 1000 rows in one INSERT ... VALUES statement.
_MAX_ROWS_PER_INSERT = 1000


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
