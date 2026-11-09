"""Dataverse (Dynamics 365) to SQL Server import, page by page.

Kestra usage::

    from python_toolkit.api.dataverse_import import DataverseImport

    result = DataverseImport.import_entity(
        conn_str, resource_url, token, "accounts",
        "[Data.Staging].[DS.CRM].[Imports.Account]",
        "EXEC [DS.CRM].[Import].[Upsert.Account] %s, %s",
        import_id=import_id, merge_params=[0],
    )

Wraps ``DynamicsCrmClient.iter_entity_pages`` and the ``SqlServerTransfer`` staging and
procedure helpers, so an import flow is a single call. Needs the ``api`` and
``sqlserver`` extras.
"""

from __future__ import annotations

import re
import uuid
from decimal import Decimal
from typing import Any, Optional

import requests

from python_toolkit.api.dynamics_crm import DynamicsCrmClient
from python_toolkit.common.dates import Dates
from python_toolkit.common.logging import ToolkitLogger
from python_toolkit.common.validation import Validation
from python_toolkit.sqlserver.sqlserver import SqlServer
from python_toolkit.sqlserver.transfer import SqlServerTransfer

__all__ = ["DataverseImport"]

_KEY_COLUMN_RE = re.compile(r"^[A-Za-z0-9_]+$")


def _is_number(value: Any) -> bool:
    """True for int, float and Decimal values; False for bool and anything else."""
    return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)


class DataverseImport:
    """Import Dataverse entity sets into SQL Server through a JSON staging table."""

    @staticmethod
    def import_entity(
        conn_str: str,
        resource_url: str,
        token: str,
        entity_set_name: str,
        staging_table: str,
        merge_sql: str,
        *,
        import_id: Optional[str] = None,
        select: Optional[list[str]] = None,
        filter: Optional[str] = None,
        orderby: Optional[str] = None,
        page_size: int = 5000,
        annotations: Optional[list[str]] = DynamicsCrmClient.CHOICE_AND_LOOKUP_ANNOTATIONS,
        merge_params: Optional[list[Any]] = None,
        timeout: float = 30.0,
        session: Optional[requests.Session] = None,
    ) -> dict[str, Any]:
        """Read an entity set page by page, staging and merging each page before the next.

        Each non-empty page is written to ``staging_table`` with
        ``SqlServerTransfer.records_to_json_staging`` and then merged with
        ``SqlServerTransfer.sql_sp_execute(conn_str, merge_sql,
        params=[import_id, *merge_params], return_results=True)``. Empty pages are
        skipped. The same ``import_id`` (a new UUID when empty) is used for every page.

        The merge must return a result set whose first row holds counts (e.g.
        ``RowsRead, RowsInserted, RowsUpdated``). Every numeric column of that row
        (int, float or Decimal, not bool) is added up over all pages; other columns
        are ignored. A merge that returns no rows raises ``RuntimeError``.

        Errors are not caught: a failure on page N raises, and pages 1 to N-1 stay
        merged.

        Args:
            conn_str: SQL Server connection string for the staging table and merge.
            resource_url: Dataverse base URL, e.g. ``https://org.crm.dynamics.com``.
            token: Azure AD access token for the Dataverse resource.
            entity_set_name: Dataverse entity set name, e.g. ``accounts``.
            staging_table: JSON staging table, e.g.
                ``[Data.Staging].[DS.CRM].[Imports.Account]``.
            merge_sql: SQL run after each page; ``import_id`` fills its first ``%s``.
            import_id: Import ID for every page; a new UUID when empty.
            select: Optional list of columns to read (``$select``).
            filter: Optional OData filter (``$filter``).
            orderby: Optional OData sort (``$orderby``).
            page_size: Records per page.
            annotations: Annotations to request; choice labels and lookup tables by default.
            merge_params: Extra parameters for ``merge_sql``, after ``import_id``.
            timeout: HTTP timeout in seconds.
            session: Optional shared ``requests`` session.

        Returns:
            Dict with ``entity_set_name``, ``import_id``, ``pages``, ``row_count``
            (records read from Dataverse), ``merge_totals`` (column → summed value)
            and ``timestamp`` (UTC ISO 8601).
        """
        resolved_merge_sql = Validation.require_non_empty(merge_sql, "merge_sql")
        resolved_staging_table = SqlServer.validate_table_name(staging_table, "staging_table")
        if merge_params is not None and not isinstance(merge_params, list):
            raise ValueError("merge_params must be a list or None")

        logger = ToolkitLogger.get_logger("DataverseImport")
        resolved_import_id = (import_id or "").strip() or str(uuid.uuid4())
        merge_values = [resolved_import_id, *(merge_params or [])]

        pages = DynamicsCrmClient.iter_entity_pages(
            resource_url,
            entity_set_name,
            token,
            select=select,
            filter=filter,
            orderby=orderby,
            page_size=page_size,
            annotations=annotations,
            timeout=timeout,
            session=session,
        )

        page_count = 0
        row_count = 0
        merge_totals: dict[str, Any] = {}
        resolved_entity_set_name = entity_set_name
        for page in pages:
            page_count = page["page_number"]
            resolved_entity_set_name = page["entity_set_name"]
            records = page["records"]
            if not records:
                logger.info(
                    "Page %d of %s: no records, skipped", page_count, resolved_entity_set_name
                )
                continue

            SqlServerTransfer.records_to_json_staging(
                conn_str, records, resolved_staging_table, import_id=resolved_import_id
            )
            merge = SqlServerTransfer.sql_sp_execute(
                conn_str, resolved_merge_sql, params=merge_values, return_results=True
            )
            merge_rows = merge.get("records") or []
            if not merge_rows:
                raise RuntimeError(
                    f"merge_sql returned no rows for page {page_count} of "
                    f"{resolved_entity_set_name} (import {resolved_import_id}); "
                    "it must return one row of counts"
                )

            merge_row = merge_rows[0]
            for column, value in merge_row.items():
                if _is_number(value):
                    merge_totals[column] = merge_totals.get(column, 0) + value
            row_count += len(records)

            logger.info(
                "Page %d of %s: %d record(s) staged and merged: %s",
                page_count,
                resolved_entity_set_name,
                len(records),
                merge_row,
            )

        logger.info(
            "Imported %s: %d record(s) in %d page(s) (import %s); totals: %s",
            resolved_entity_set_name,
            row_count,
            page_count,
            resolved_import_id,
            merge_totals,
        )

        return {
            "entity_set_name": resolved_entity_set_name,
            "import_id": resolved_import_id,
            "pages": page_count,
            "row_count": row_count,
            "merge_totals": merge_totals,
            "timestamp": Dates.utc_now().isoformat(),
        }

    @staticmethod
    def check_entity_deletes(
        conn_str: str,
        resource_url: str,
        token: str,
        entity_set_name: str,
        key_column: str,
        staging_table: str,
        delete_sql: str,
        *,
        import_id: Optional[str] = None,
        page_size: int = 5000,
        timeout: float = 30.0,
        session: Optional[requests.Session] = None,
    ) -> dict[str, Any]:
        """Stage every ID of an entity set, then run a delete check once.

        Reads only ``key_column`` (no annotations) page by page and stages each
        non-empty page with ``SqlServerTransfer.records_to_json_staging``; each row's
        JSON is ``{"<key_column>": "<id>"}``. Once every page has been read,
        ``delete_sql`` runs once with ``import_id`` as its parameter, even when no IDs
        came back (the procedure does its own safety check). If a page read fails the
        error is raised before ``delete_sql`` runs, so a partial ID list never reaches it.

        Args:
            conn_str: SQL Server connection string for the staging table and check.
            resource_url: Dataverse base URL, e.g. ``https://org.crm.dynamics.com``.
            token: Azure AD access token for the Dataverse resource.
            entity_set_name: Dataverse entity set name, e.g. ``accounts``.
            key_column: Primary key column, e.g. ``accountid`` (letters, digits, ``_``).
            staging_table: JSON staging table for the IDs.
            delete_sql: SQL run once after all pages; ``import_id`` fills its ``%s``.
            import_id: Import ID for the staged IDs; a new UUID when empty.
            page_size: Records per page.
            timeout: HTTP timeout in seconds.
            session: Optional shared ``requests`` session.

        Returns:
            Dict with ``entity_set_name``, ``import_id``, ``pages``, ``keys_read``,
            ``result`` (first row of the ``delete_sql`` result, or ``{}``) and
            ``timestamp`` (UTC ISO 8601).
        """
        resolved_key_column = Validation.require_non_empty(key_column, "key_column")
        if not _KEY_COLUMN_RE.fullmatch(resolved_key_column):
            raise ValueError("key_column must contain only letters, numbers and underscores")
        resolved_delete_sql = Validation.require_non_empty(delete_sql, "delete_sql")
        resolved_staging_table = SqlServer.validate_table_name(staging_table, "staging_table")

        logger = ToolkitLogger.get_logger("DataverseImport")
        resolved_import_id = (import_id or "").strip() or str(uuid.uuid4())

        pages = DynamicsCrmClient.iter_entity_pages(
            resource_url,
            entity_set_name,
            token,
            select=[resolved_key_column],
            page_size=page_size,
            timeout=timeout,
            session=session,
        )

        page_count = 0
        keys_read = 0
        resolved_entity_set_name = entity_set_name
        for page in pages:
            page_count = page["page_number"]
            resolved_entity_set_name = page["entity_set_name"]
            records = page["records"]
            if not records:
                continue
            SqlServerTransfer.records_to_json_staging(
                conn_str,
                [{resolved_key_column: record.get(resolved_key_column)} for record in records],
                resolved_staging_table,
                import_id=resolved_import_id,
            )
            keys_read += len(records)
            logger.info(
                "Page %d of %s: %d key(s) staged", page_count, resolved_entity_set_name, len(records)
            )

        delete_check = SqlServerTransfer.sql_sp_execute(
            conn_str, resolved_delete_sql, params=resolved_import_id, return_results=True
        )
        delete_rows = delete_check.get("records") or []
        result = delete_rows[0] if delete_rows else {}

        logger.info(
            "Delete check for %s: %d key(s) in %d page(s) (import %s); result: %s",
            resolved_entity_set_name,
            keys_read,
            page_count,
            resolved_import_id,
            result,
        )

        return {
            "entity_set_name": resolved_entity_set_name,
            "import_id": resolved_import_id,
            "pages": page_count,
            "keys_read": keys_read,
            "result": result,
            "timestamp": Dates.utc_now().isoformat(),
        }
