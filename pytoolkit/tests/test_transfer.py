"""Tests for SqlServerTransfer row inserts. The connection is mocked; no SQL Server is used.

Run from pytoolkit/:  python3 -m unittest discover -s tests
"""

from __future__ import annotations

import sys
import unittest
import uuid
from contextlib import contextmanager
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from python_toolkit.sqlserver.sqlserver import SqlServer  # noqa: E402
from python_toolkit.sqlserver.transfer import SqlServerTransfer  # noqa: E402

TABLE = "[Data.Staging].[dbo].[CashPositions]"


class _FakeConnections:
    """Patches ``SqlServer.connect`` to hand out mock connections, one per call."""

    def __init__(self, source_rows=None):
        self.source_rows = list(source_rows or [])
        self.connections: list[mock.MagicMock] = []
        self.conn_strs: list[str] = []

    def _new_connection(self) -> mock.MagicMock:
        conn = mock.MagicMock()
        cursor = conn.cursor.return_value
        rows = self.source_rows

        def fetchmany(size):
            batch = rows[:size]
            del rows[:size]
            return batch

        cursor.fetchmany.side_effect = fetchmany

        # Like pymssql, bulk_copy reads its elements iterable as it sends.
        conn.bulk_rows = []

        def bulk_copy(table_name, elements, **kwargs):
            for element in elements:
                conn.bulk_rows.append(element)

        conn.bulk_copy.side_effect = bulk_copy
        return conn

    @contextmanager
    def connect(self, conn_str, **kwargs):
        self.conn_strs.append(conn_str)
        conn = self._new_connection()
        self.connections.append(conn)
        try:
            yield conn
        finally:
            conn.close()

    def patch(self):
        return mock.patch.object(SqlServer, "connect", side_effect=self.connect)


def _executed(conn):
    """Return (sql, params) for each ``cursor.execute`` call on ``conn``."""
    return [c.args for c in conn.cursor.return_value.execute.call_args_list]


class RecordsToSqlTableTests(unittest.TestCase):
    def test_dict_records_use_values_in_order_plus_trailing_columns(self):
        fake = _FakeConnections()
        records = [{"TerminalId": "T001", "Amount": 10}, {"TerminalId": "T002", "Amount": 20}]
        with fake.patch():
            result = SqlServerTransfer.records_to_sql_table(
                "conn", records, TABLE, process_guid="abc"
            )

        conn = fake.connections[0]
        (sql, params), = _executed(conn)
        self.assertEqual(sql, f"INSERT INTO {TABLE} VALUES (%s, %s, %s, %s), (%s, %s, %s, %s)")
        self.assertEqual(params[0:2], ("T001", 10))
        self.assertEqual(params[4:6], ("T002", 20))
        load_time = params[2]
        self.assertIsInstance(load_time, datetime)
        self.assertIsNone(load_time.tzinfo)
        self.assertEqual(params[6], load_time)  # one load time for the whole call
        self.assertEqual((params[3], params[7]), ("abc", "abc"))
        conn.commit.assert_called_once()
        conn.rollback.assert_not_called()
        self.assertEqual(result["destination_table"], TABLE)
        self.assertEqual(result["row_count"], 2)
        self.assertEqual(result["process_guid"], "abc")
        self.assertIn("timestamp", result)

    def test_tuple_and_list_records(self):
        fake = _FakeConnections()
        with fake.patch():
            SqlServerTransfer.records_to_sql_table("conn", [("A", 1), ["B", 2]], TABLE, process_guid="g")
        (sql, params), = _executed(fake.connections[0])
        self.assertEqual(params[0:2], ("A", 1))
        self.assertEqual(params[3], "g")
        self.assertEqual(params[4:6], ("B", 2))
        self.assertEqual(params[7], "g")

    def test_new_process_guid_when_empty(self):
        for value in (None, "", "  "):
            fake = _FakeConnections()
            with fake.patch():
                result = SqlServerTransfer.records_to_sql_table("conn", [(1,)], TABLE, process_guid=value)
            uuid.UUID(result["process_guid"])
            (sql, params), = _executed(fake.connections[0])
            self.assertEqual(params[2], result["process_guid"])

    def test_chunks_of_at_most_1000_rows(self):
        fake = _FakeConnections()
        records = [{"Id": i} for i in range(2500)]
        with fake.patch():
            result = SqlServerTransfer.records_to_sql_table("conn", records, TABLE, batch_size=5000)
        calls = _executed(fake.connections[0])
        self.assertEqual([len(params) // 3 for _, params in calls], [1000, 1000, 500])
        self.assertEqual([sql.count("(%s, %s, %s)") for sql, _ in calls], [1000, 1000, 500])
        self.assertEqual(calls[2][1][-3], 2499)
        self.assertEqual(result["row_count"], 2500)
        fake.connections[0].commit.assert_called_once()

    def test_batch_size_below_1000_sets_rows_per_statement(self):
        fake = _FakeConnections()
        with fake.patch():
            SqlServerTransfer.records_to_sql_table("conn", [(i,) for i in range(5)], TABLE, batch_size=2)
        calls = _executed(fake.connections[0])
        self.assertEqual([len(params) // 3 for _, params in calls], [2, 2, 1])

    def test_empty_records_do_not_connect(self):
        fake = _FakeConnections()
        with fake.patch():
            result = SqlServerTransfer.records_to_sql_table("conn", [], TABLE, process_guid="g")
        self.assertEqual(fake.connections, [])
        self.assertEqual(result["row_count"], 0)
        self.assertEqual(result["process_guid"], "g")

    def test_rolls_back_and_raises_on_error(self):
        fake = _FakeConnections()
        original = fake._new_connection

        def failing_connection():
            conn = original()
            conn.cursor.return_value.execute.side_effect = [None, RuntimeError("boom")]
            return conn

        fake._new_connection = failing_connection
        with fake.patch(), self.assertRaisesRegex(RuntimeError, "boom"):
            SqlServerTransfer.records_to_sql_table("conn", [(i,) for i in range(1500)], TABLE)
        conn = fake.connections[0]
        conn.commit.assert_not_called()
        conn.rollback.assert_called_once()
        conn.cursor.return_value.close.assert_called_once()

    def test_validation(self):
        fake = _FakeConnections()
        with fake.patch():
            with self.assertRaises(ValueError):
                SqlServerTransfer.records_to_sql_table("conn", [(1,)], TABLE, batch_size=0)
            with self.assertRaises(ValueError):
                SqlServerTransfer.records_to_sql_table("conn", [(1,)], "bad; DROP TABLE x")
            with self.assertRaises(ValueError):
                SqlServerTransfer.records_to_sql_table("conn", ["abc"], TABLE)
            with self.assertRaises(ValueError):
                SqlServerTransfer.records_to_sql_table("conn", {"a": 1}, TABLE)
        self.assertEqual(fake.connections, [])


class SqlSpToSqlTableTests(unittest.TestCase):
    def test_streams_source_rows_into_destination(self):
        fake = _FakeConnections(source_rows=[(i, f"T{i}") for i in range(2500)])
        with fake.patch():
            result = SqlServerTransfer.sql_sp_to_sql_table(
                "src", "dst", "EXEC [dbo].[GetRows]", TABLE, process_guid="g", batch_size=1500
            )
        source, dest = fake.connections
        self.assertEqual(fake.conn_strs, ["src", "dst"])
        source.cursor.return_value.execute.assert_called_once_with("EXEC [dbo].[GetRows]")
        self.assertEqual(
            [c.args[0] for c in source.cursor.return_value.fetchmany.call_args_list], [1500, 1500, 1500]
        )
        calls = _executed(dest)
        # fetchmany batches of 1500 and 1000, each split into statements of at most 1000 rows
        self.assertEqual([len(params) // 4 for _, params in calls], [1000, 500, 1000])
        sql, params = calls[0]
        self.assertTrue(sql.startswith(f"INSERT INTO {TABLE} VALUES (%s, %s, %s, %s), "))
        self.assertEqual(params[0:2], (0, "T0"))
        self.assertIsInstance(params[2], datetime)
        self.assertEqual(params[3], "g")
        self.assertEqual({p[2] for _, ps in calls for p in [ps[i:i + 4] for i in range(0, len(ps), 4)]},
                         {params[2]})
        dest.commit.assert_called_once()
        self.assertEqual(result["row_count"], 2500)
        self.assertEqual(set(result), {"destination_table", "row_count", "process_guid", "timestamp"})

    def test_rolls_back_and_raises_on_error(self):
        fake = _FakeConnections(source_rows=[(1,)])
        original = fake._new_connection

        def failing_connection():
            conn = original()
            if fake.connections:  # the destination connection
                conn.cursor.return_value.execute.side_effect = RuntimeError("boom")
            return conn

        fake._new_connection = failing_connection
        with fake.patch(), self.assertRaisesRegex(RuntimeError, "boom"):
            SqlServerTransfer.sql_sp_to_sql_table("src", "dst", "EXEC x", TABLE)
        dest = fake.connections[1]
        dest.commit.assert_not_called()
        dest.rollback.assert_called_once()


def _failing_bulk_copy(fake, after_rows):
    """Make the first connection's bulk_copy raise after sending ``after_rows`` rows."""
    original = fake._new_connection

    def new_connection():
        conn = original()
        if not fake.connections:
            def bulk_copy(table_name, elements, **kwargs):
                for element in elements:
                    if len(conn.bulk_rows) == after_rows:
                        raise RuntimeError("bcp boom")
                    conn.bulk_rows.append(element)

            conn.bulk_copy.side_effect = bulk_copy
        return conn

    fake._new_connection = new_connection


class RecordsToSqlTableBulkCopyTests(unittest.TestCase):
    def test_bulk_copy_sends_rows_with_trailing_columns(self):
        fake = _FakeConnections()
        records = [{"TerminalId": "T001", "Amount": 10}, {"TerminalId": "T002", "Amount": None}]
        with fake.patch():
            result = SqlServerTransfer.records_to_sql_table(
                "conn", records, TABLE, process_guid="abc", batch_size=500, bulk_copy=True
            )
        conn, = fake.connections
        conn.bulk_copy.assert_called_once()
        args, kwargs = conn.bulk_copy.call_args
        self.assertEqual(args[0], TABLE)
        self.assertEqual(kwargs, {"batch_size": 500})
        load_time = conn.bulk_rows[0][2]
        self.assertIsInstance(load_time, datetime)
        self.assertIsNone(load_time.tzinfo)
        self.assertEqual(conn.bulk_rows, [("T001", 10, load_time, "abc"), ("T002", None, load_time, "abc")])
        conn.cursor.return_value.execute.assert_not_called()
        conn.commit.assert_called_once()
        conn.rollback.assert_not_called()
        self.assertEqual(
            result,
            {"destination_table": TABLE, "row_count": 2, "process_guid": "abc",
             "timestamp": result["timestamp"]},
        )

    def test_bulk_copy_converts_types_it_cannot_bind(self):
        fake = _FakeConnections()
        guid = uuid.UUID("12345678-1234-5678-1234-567812345678")
        stamp = datetime(2026, 1, 2, 3, 4, 5, 678000)
        row = (guid, date(2026, 1, 2), time(3, 4, 5), stamp, Decimal("1.50"), memoryview(b"\x01"),
               b"\x02", True, 1.5, "naïve")
        with fake.patch():
            SqlServerTransfer.records_to_sql_table("conn", [row], TABLE, process_guid="g", bulk_copy=True)
        sent, = fake.connections[0].bulk_rows
        self.assertEqual(
            sent[:10],
            (str(guid), "2026-01-02", "03:04:05", stamp, Decimal("1.50"), b"\x01", b"\x02", True, 1.5, "naïve"),
        )
        self.assertEqual(sent[11], "g")

    def test_bulk_copy_new_process_guid_when_empty(self):
        fake = _FakeConnections()
        with fake.patch():
            result = SqlServerTransfer.records_to_sql_table("conn", [(1,)], TABLE, bulk_copy=True)
        uuid.UUID(result["process_guid"])
        self.assertEqual(fake.connections[0].bulk_rows[0][2], result["process_guid"])

    def test_bulk_copy_empty_records_do_not_connect(self):
        fake = _FakeConnections()
        with fake.patch():
            result = SqlServerTransfer.records_to_sql_table("conn", [], TABLE, bulk_copy=True)
        self.assertEqual(fake.connections, [])
        self.assertEqual(result["row_count"], 0)

    def test_bulk_copy_failure_rolls_back_deletes_and_raises(self):
        fake = _FakeConnections()
        _failing_bulk_copy(fake, after_rows=3)
        with fake.patch(), self.assertRaisesRegex(RuntimeError, "bcp boom"):
            SqlServerTransfer.records_to_sql_table(
                "conn", [(i,) for i in range(10)], TABLE, process_guid="g", bulk_copy=True
            )
        load, cleanup = fake.connections
        self.assertEqual(fake.conn_strs, ["conn", "conn"])
        load.commit.assert_not_called()
        load.rollback.assert_called_once()
        load.close.assert_called_once()
        self.assertEqual(
            _executed(cleanup), [(f"DELETE FROM {TABLE} WHERE [ProcessId] = %s", ("g",))]
        )
        cleanup.commit.assert_called_once()

    def test_bulk_copy_custom_process_id_column(self):
        fake = _FakeConnections()
        _failing_bulk_copy(fake, after_rows=0)
        with fake.patch(), self.assertRaises(RuntimeError):
            SqlServerTransfer.records_to_sql_table(
                "conn", [(1,)], TABLE, process_guid="g", bulk_copy=True, process_id_column="LoadId"
            )
        self.assertEqual(_executed(fake.connections[1])[0][0], f"DELETE FROM {TABLE} WHERE [LoadId] = %s")

    def test_bulk_copy_no_cleanup_when_process_id_column_is_none(self):
        fake = _FakeConnections()
        _failing_bulk_copy(fake, after_rows=1)
        with fake.patch(), self.assertRaises(RuntimeError):
            SqlServerTransfer.records_to_sql_table(
                "conn", [(1,), (2,)], TABLE, bulk_copy=True, process_id_column=None
            )
        self.assertEqual(len(fake.connections), 1)
        fake.connections[0].rollback.assert_called_once()

    def test_bulk_copy_original_error_raised_when_rollback_and_cleanup_fail(self):
        fake = _FakeConnections()
        original = fake._new_connection

        def new_connection():
            conn = original()
            if not fake.connections:
                conn.bulk_copy.side_effect = RuntimeError("bcp boom")
                conn.rollback.side_effect = RuntimeError("rollback boom")
            else:
                conn.cursor.return_value.execute.side_effect = RuntimeError("delete boom")
            return conn

        fake._new_connection = new_connection
        with fake.patch(), self.assertRaisesRegex(RuntimeError, "bcp boom"):
            SqlServerTransfer.records_to_sql_table("conn", [(1,)], TABLE, bulk_copy=True)
        fake.connections[1].commit.assert_not_called()

    def test_bulk_copy_validates_process_id_column_before_connecting(self):
        fake = _FakeConnections()
        with fake.patch():
            for bad in ("Process Id", "x]; DROP TABLE y--", ""):
                with self.assertRaises(ValueError):
                    SqlServerTransfer.records_to_sql_table(
                        "conn", [(1,)], TABLE, bulk_copy=True, process_id_column=bad
                    )
            # Not used, so not checked, without bulk_copy.
            SqlServerTransfer.records_to_sql_table("conn", [(1,)], TABLE, process_id_column="Process Id")
        self.assertEqual(len(fake.connections), 1)
        fake.connections[0].bulk_copy.assert_not_called()


class SqlSpToSqlTableBulkCopyTests(unittest.TestCase):
    def test_streams_source_rows_into_one_bulk_copy(self):
        fake = _FakeConnections(source_rows=[(i, f"T{i}") for i in range(2500)])
        with fake.patch():
            result = SqlServerTransfer.sql_sp_to_sql_table(
                "src", "dst", "EXEC [dbo].[GetRows]", TABLE, process_guid="g", batch_size=1500,
                bulk_copy=True,
            )
        source, dest = fake.connections
        source.cursor.return_value.execute.assert_called_once_with("EXEC [dbo].[GetRows]")
        self.assertEqual(
            [c.args[0] for c in source.cursor.return_value.fetchmany.call_args_list], [1500, 1500, 1500]
        )
        dest.bulk_copy.assert_called_once()
        self.assertEqual(dest.bulk_copy.call_args.args[0], TABLE)
        self.assertEqual(dest.bulk_copy.call_args.kwargs, {"batch_size": 1500})
        self.assertEqual(len(dest.bulk_rows), 2500)
        load_time = dest.bulk_rows[0][2]
        self.assertEqual(dest.bulk_rows[0], (0, "T0", load_time, "g"))
        self.assertEqual(dest.bulk_rows[-1], (2499, "T2499", load_time, "g"))
        dest.cursor.return_value.execute.assert_not_called()
        dest.commit.assert_called_once()
        self.assertEqual(result["row_count"], 2500)
        self.assertEqual(set(result), {"destination_table", "row_count", "process_guid", "timestamp"})

    def test_failure_rolls_back_and_deletes_on_destination(self):
        fake = _FakeConnections(source_rows=[(i,) for i in range(5)])
        original = fake._new_connection

        def new_connection():
            conn = original()
            if len(fake.connections) == 1:  # the destination connection
                conn.bulk_copy.side_effect = RuntimeError("bcp boom")
            return conn

        fake._new_connection = new_connection
        with fake.patch(), self.assertRaisesRegex(RuntimeError, "bcp boom"):
            SqlServerTransfer.sql_sp_to_sql_table("src", "dst", "EXEC x", TABLE, process_guid="g", bulk_copy=True)
        source, dest, cleanup = fake.connections
        self.assertEqual(fake.conn_strs, ["src", "dst", "dst"])
        dest.commit.assert_not_called()
        dest.rollback.assert_called_once()
        source.cursor.return_value.close.assert_called_once()
        self.assertEqual(_executed(cleanup), [(f"DELETE FROM {TABLE} WHERE [ProcessId] = %s", ("g",))])
        cleanup.commit.assert_called_once()

    def test_default_path_does_not_bulk_copy(self):
        fake = _FakeConnections(source_rows=[(1,)])
        with fake.patch():
            SqlServerTransfer.sql_sp_to_sql_table("src", "dst", "EXEC x", TABLE)
        fake.connections[1].bulk_copy.assert_not_called()
        self.assertEqual(len(_executed(fake.connections[1])), 1)


if __name__ == "__main__":
    unittest.main()
