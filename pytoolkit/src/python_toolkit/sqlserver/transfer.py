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
        Rows are streamed in batches and committed in a single transaction.
        """
        Validation.require_non_empty(sql_query, "sql_query")
        destination_table = SqlServer.validate_table_name(destination_table, "destination_table")
        if batch_size <= 0:
            raise ValueError("batch_size must be greater than 0")

        logger = ToolkitLogger.get_logger("SqlServerTransfer")
        process_guid = (process_guid or "").strip() or str(uuid.uuid4())
        date_created = Dates.utc_now().replace(tzinfo=None)
        row_count = 0

        source_conn_str = SqlServer.to_odbc_connection_string(source_conn_str)
        destination_conn_str = SqlServer.to_odbc_connection_string(destination_conn_str)

        with SqlServer.connect(source_conn_str) as source_conn, \
                SqlServer.connect(destination_conn_str) as dest_conn:
            source_cursor = source_conn.cursor()
            dest_cursor = dest_conn.cursor()
            try:
                source_cursor.execute(sql_query)
                dest_cursor.fast_executemany = True
                insert_sql = None

                while True:
                    rows = source_cursor.fetchmany(batch_size)
                    if not rows:
                        break

                    batch = [(*row, date_created, process_guid) for row in rows]
                    if insert_sql is None:
                        placeholders = ", ".join(["?"] * len(batch[0]))
                        insert_sql = f"INSERT INTO {destination_table} VALUES ({placeholders})"

                    dest_cursor.executemany(insert_sql, batch)
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
