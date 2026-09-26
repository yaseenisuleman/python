"""Parquet writer using PyArrow.

Kestra usage::

    from python_toolkit.parquet.writer import ParquetWriter
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Sequence

import pyarrow as pa
import pyarrow.parquet as pq

from python_toolkit.common.validation import Validation


class ParquetWriter:
    """Write row dictionaries to Parquet files."""

    @staticmethod
    def write(
        rows: Sequence[dict[str, Any]],
        path: str | Path,
        *,
        compression: str = "snappy",
    ) -> Path:
        """Write a list of row dicts to a Parquet file."""
        output = Path(path)
        if not rows:
            raise ValueError("rows must not be empty")

        table = pa.Table.from_pylist(list(rows))
        pq.write_table(table, output, compression=compression)
        return output

    @staticmethod
    def write_batches(
        batches: Iterable[Sequence[dict[str, Any]]],
        path: str | Path,
        *,
        compression: str = "snappy",
    ) -> Path:
        """Write multiple row batches to a single Parquet file."""
        Validation.require_non_empty(str(path), "path")
        output = Path(path)
        writer: pq.ParquetWriter | None = None

        try:
            for batch in batches:
                if not batch:
                    continue
                table = pa.Table.from_pylist(list(batch))
                if writer is None:
                    writer = pq.ParquetWriter(output, table.schema, compression=compression)
                writer.write_table(table)
        finally:
            if writer is not None:
                writer.close()

        if writer is None:
            raise ValueError("batches contained no data")

        return output
