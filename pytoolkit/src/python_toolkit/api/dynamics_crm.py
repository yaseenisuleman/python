"""Dynamics 365 CRM (Dataverse Web API) client.

Kestra usage::

    from python_toolkit.api.dynamics_crm import DynamicsCrmClient

    token = DynamicsCrmClient.get_access_token(
        "{{ inputs.tenant_id }}",
        "{{ inputs.client_id }}",
        "{{ inputs.client_secret }}",
        "{{ inputs.resource }}",
    )

Reads (``query_entities``, or page by page with ``iter_entity_pages``) and
updates (``update_entity``, ``bulk_update_entities``) Dataverse records.

Uses the Azure AD v1 OAuth 2.0 client credentials grant
(``https://login.microsoftonline.com/{tenant_id}/oauth2/token``) against a
Dynamics ``resource`` URL, e.g. ``https://org.crm.dynamics.com/``.
"""

from __future__ import annotations
import time
from typing import Any, Iterator, Optional
from urllib.parse import quote, urlencode
import requests

from python_toolkit.common.dates import Dates
from python_toolkit.common.logging import ToolkitLogger
from python_toolkit.common.validation import Validation

__all__ = ["DynamicsCrmClient"]

_TOKEN_PATH = "oauth2/token"

# Statuses Dataverse uses for service protection limits (throttling); the request is retried.
_THROTTLE_STATUSES = (429, 503)
_DEFAULT_RETRY_AFTER_SECONDS = 5.0


class DynamicsCrmClient:
    """Client for the Dynamics 365 / Dataverse Web API."""

    # Choice labels and lookup target table names, for ``annotations=``.
    CHOICE_AND_LOOKUP_ANNOTATIONS = [
        "OData.Community.Display.V1.FormattedValue",
        "Microsoft.Dynamics.CRM.lookuplogicalname",
    ]

    @staticmethod
    def __build_auth_headers(token: str) -> dict[str, str]:
        resolved_token = Validation.require_non_empty(token, "token")
        return {
            "Authorization": f"Bearer {resolved_token}",
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
        }

    @staticmethod
    def __raise_update_error(action: str, response: requests.Response) -> None:
        try:
            error_payload = response.json()
        except ValueError:
            error_payload = {"raw": response.text}

        message = (
            f"{action} failed with status code {response.status_code}: "
            f"{error_payload}"
        )
        raise RuntimeError(message)

    @staticmethod
    def __short_update_error(response: requests.Response) -> str:
        try:
            error_payload = response.json()
            message = error_payload.get("error", {}).get("message")
        except (AttributeError, ValueError):
            message = None

        if not message:
            message = response.text.strip() or "Dataverse rejected the update"

        short_message = str(message).split("---->", 1)[0].strip()
        return " ".join(short_message.split())[:500]

    @staticmethod
    def get_access_token(
        tenant_id: str,
        client_id: str,
        client_secret: str,
        resource: str,
        *,
        timeout: float = 30.0,
        session: Optional[requests.Session] = None,
    ) -> dict[str, Any]:
        """Acquire an Azure AD access token for the Dataverse Web API.

        Args:
            tenant_id: Azure AD tenant ID (GUID).
            client_id: App registration (application) client ID.
            client_secret: App registration client secret.
            resource: Dynamics CRM resource URL, e.g.
                ``https://org.crm.dynamics.com/``.
            timeout: HTTP timeout in seconds.
            session: Optional shared ``requests`` session.

        Returns:
            Dict with ``access_token``, ``token_type``, ``expires_in``,
            ``ext_expires_in``, ``expires_on``, ``not_before``, and
            ``resource``.
        """
        logger = ToolkitLogger.get_logger("DynamicsCrmClient")
        resolved_tenant_id = Validation.require_non_empty(tenant_id, "tenant_id")
        resolved_client_id = Validation.require_non_empty(client_id, "client_id")
        resolved_client_secret = Validation.require_non_empty(
            client_secret, "client_secret"
        )
        resolved_resource = Validation.require_non_empty(resource, "resource")

        token_url = (
            f"https://login.microsoftonline.com/{resolved_tenant_id}/{_TOKEN_PATH}"
        )
        payload = {
            "grant_type": "client_credentials",
            "client_id": resolved_client_id,
            "client_secret": resolved_client_secret,
            "resource": resolved_resource,
        }

        http = session or requests
        logger.info("Requesting Dynamics access token for resource %s", resolved_resource)
        response = http.post(token_url, data=payload, timeout=timeout)
        response.raise_for_status()

        data = response.json()
        access_token = data.get("access_token")
        if not access_token:
            raise ValueError("Token response did not include access_token")

        return {
            "access_token": access_token,
            "token_type": data.get("token_type", "Bearer"),
            "expires_in": data.get("expires_in"),
            "ext_expires_in": data.get("ext_expires_in"),
            "expires_on": data.get("expires_on"),
            "not_before": data.get("not_before"),
            "resource": data.get("resource", resolved_resource),
        }

    @staticmethod
    def update_entity(
        resource_url: str,
        entity_set_name: str,
        entity_id: str,
        field_values: dict[str, Any],
        token: str,
        *,
        timeout: float = 30.0,
        session: Optional[requests.Session] = None,
    ) -> dict[str, Any]:
        """Update a single Dataverse entity by ID using a dynamic field dictionary.

        Args:
            resource_url: Dataverse base URL, e.g. ``https://org.crm.dynamics.com``.
            entity_set_name: Dataverse logical entity set name, e.g. ``accounts``.
            entity_id: Entity GUID to update.
            field_values: Mapping of CRM column names to values to update.
            token: Azure AD access token for the Dataverse resource.
            timeout: HTTP timeout in seconds.
            session: Optional shared ``requests`` session.

        Returns:
            A dictionary with the update outcome details.
        """
        resolved_resource_url = Validation.require_non_empty(resource_url, "resource_url")
        resolved_entity_set_name = Validation.require_non_empty(
            entity_set_name, "entity_set_name"
        )
        resolved_entity_id = Validation.require_non_empty(entity_id, "entity_id")
        if not isinstance(field_values, dict) or not field_values:
            raise ValueError("field_values must be a non-empty dictionary")

        http = session or requests
        url = (
            f"{resolved_resource_url.rstrip('/')}/api/data/v9.2/"
            f"{resolved_entity_set_name}({resolved_entity_id})"
        )
        payload = dict(field_values)
        headers = DynamicsCrmClient.__build_auth_headers(token)

        response = http.patch(url, headers=headers, json=payload, timeout=timeout)
        if response.status_code not in (200, 204):
            DynamicsCrmClient.__raise_update_error(
                f"Update entity {resolved_entity_set_name}({resolved_entity_id})",
                response,
            )

        return {
            "entity_set_name": resolved_entity_set_name,
            "entity_id": resolved_entity_id,
            "updated_fields": list(payload.keys()),
            "status_code": response.status_code,
        }

    @staticmethod
    def __retry_after_seconds(response: requests.Response) -> float:
        """Seconds to wait from a ``Retry-After`` header, or the default when absent or not a number."""
        try:
            seconds = float(response.headers.get("Retry-After"))
        except (TypeError, ValueError):
            return _DEFAULT_RETRY_AFTER_SECONDS
        return seconds if seconds >= 0 else _DEFAULT_RETRY_AFTER_SECONDS

    @staticmethod
    def iter_entity_pages(
        resource_url: str,
        entity_set_name: str,
        token: str,
        *,
        select: Optional[list[str]] = None,
        filter: Optional[str] = None,
        orderby: Optional[str] = None,
        page_size: int = 5000,
        annotations: Optional[list[str]] = None,
        max_retries: int = 3,
        timeout: float = 30.0,
        session: Optional[requests.Session] = None,
    ) -> Iterator[dict[str, Any]]:
        """Read Dataverse records from an entity set one page at a time.

        A generator: each page is requested only when the previous one has been
        consumed, so callers can save a page before the next is read. Follows
        ``@odata.nextLink`` until Dataverse returns no further link. An empty result
        still yields one page (with no records).

        Inputs are validated when this method is called, before the first page is read.

        Args:
            resource_url: Dataverse base URL, e.g. ``https://org.crm.dynamics.com``.
            entity_set_name: Dataverse logical entity set name, e.g. ``accounts``.
            token: Azure AD access token for the Dataverse resource.
            select: Optional list of columns to return (``$select``).
            filter: Optional OData filter expression (``$filter``).
            orderby: Optional OData sort expression (``$orderby``), e.g.
                ``"modifiedon asc"``.
            page_size: Records per page, sent as ``Prefer: odata.maxpagesize``.
            annotations: Optional annotation names to request with
                ``Prefer: odata.include-annotations``, e.g.
                ``DynamicsCrmClient.CHOICE_AND_LOOKUP_ANNOTATIONS``.
            max_retries: Retries per page when Dataverse throttles (429 or 503),
                each after waiting for ``Retry-After`` (5 s if absent).
            timeout: HTTP timeout in seconds.
            session: Optional shared ``requests`` session.

        Yields:
            Dict per page with ``entity_set_name``, ``page_number`` (1-based),
            ``records`` (with ``@odata.*`` keys removed; annotation keys such as
            ``name@OData.Community.Display.V1.FormattedValue`` are kept),
            ``row_count``, and ``timestamp`` (UTC ISO 8601).
        """
        resolved_resource_url = Validation.require_non_empty(resource_url, "resource_url")
        resolved_entity_set_name = Validation.require_non_empty(
            entity_set_name, "entity_set_name"
        )
        Validation.require_non_empty(token, "token")
        if (
            not isinstance(page_size, int)
            or isinstance(page_size, bool)
            or page_size <= 0
        ):
            raise ValueError("page_size must be a positive integer")
        if (
            not isinstance(max_retries, int)
            or isinstance(max_retries, bool)
            or max_retries < 0
        ):
            raise ValueError("max_retries must be an integer of 0 or more")
        if annotations is not None and (
            not isinstance(annotations, (list, tuple))
            or not all(isinstance(name, str) and name.strip() for name in annotations)
        ):
            raise ValueError("annotations must be a list of non-empty strings")

        url = (
            f"{resolved_resource_url.rstrip('/')}/api/data/v9.2/"
            f"{resolved_entity_set_name}"
        )
        query_options: dict[str, str] = {}
        if select:
            query_options["$select"] = ",".join(select)
        if filter:
            query_options["$filter"] = filter
        if orderby:
            query_options["$orderby"] = orderby
        if query_options:
            # Encode spaces as %20 (not +) and keep $ and , readable for OData.
            url += "?" + urlencode(query_options, quote_via=quote, safe="$,")

        prefer = f"odata.maxpagesize={page_size}"
        if annotations:
            prefer += f',odata.include-annotations="{",".join(annotations)}"'

        headers = DynamicsCrmClient.__build_auth_headers(token)
        headers.update(
            {
                "Prefer": prefer,
                "OData-MaxVersion": "4.0",
                "OData-Version": "4.0",
            }
        )

        return DynamicsCrmClient.__read_pages(
            session or requests,
            url,
            headers,
            resolved_entity_set_name,
            max_retries,
            timeout,
        )

    @staticmethod
    def __read_pages(
        http: Any,
        url: str,
        headers: dict[str, str],
        entity_set_name: str,
        max_retries: int,
        timeout: float,
    ) -> Iterator[dict[str, Any]]:
        """Generator behind ``iter_entity_pages`` (kept separate so inputs are validated eagerly)."""
        logger = ToolkitLogger.get_logger("DynamicsCrmClient")
        next_url: Optional[str] = url
        page_number = 0

        while next_url:
            page_number += 1
            retries = 0
            while True:
                response = http.get(next_url, headers=headers, timeout=timeout)
                if response.status_code not in _THROTTLE_STATUSES or retries >= max_retries:
                    break
                retries += 1
                wait_seconds = DynamicsCrmClient.__retry_after_seconds(response)
                logger.warning(
                    "Dataverse returned %d for %s page %d; retry %d of %d in %.1f s",
                    response.status_code,
                    entity_set_name,
                    page_number,
                    retries,
                    max_retries,
                    wait_seconds,
                )
                time.sleep(wait_seconds)

            if response.status_code != 200:
                DynamicsCrmClient.__raise_update_error(
                    f"Query entities {entity_set_name}", response
                )

            data = response.json()
            records = [
                {
                    key: value
                    for key, value in record.items()
                    if not key.startswith("@odata.")
                }
                for record in data.get("value", [])
            ]
            # nextLink is a full URL that already carries the query options.
            next_url = data.get("@odata.nextLink")

            logger.info(
                "Read page %d of %s: %d record(s)", page_number, entity_set_name, len(records)
            )
            yield {
                "entity_set_name": entity_set_name,
                "page_number": page_number,
                "records": records,
                "row_count": len(records),
                "timestamp": Dates.utc_now().isoformat(),
            }

    @staticmethod
    def query_entities(
        resource_url: str,
        entity_set_name: str,
        token: str,
        *,
        select: Optional[list[str]] = None,
        filter: Optional[str] = None,
        orderby: Optional[str] = None,
        page_size: int = 5000,
        annotations: Optional[list[str]] = None,
        max_retries: int = 3,
        timeout: float = 30.0,
        session: Optional[requests.Session] = None,
    ) -> dict[str, Any]:
        """Read Dataverse records from an entity set, following every result page.

        Paging is automatic: each response's ``@odata.nextLink`` is followed
        until Dataverse returns no further link, and all pages are combined.
        To process large results page by page, use ``iter_entity_pages``.

        Args:
            resource_url: Dataverse base URL, e.g. ``https://org.crm.dynamics.com``.
            entity_set_name: Dataverse logical entity set name, e.g. ``accounts``.
            token: Azure AD access token for the Dataverse resource.
            select: Optional list of columns to return (``$select``).
            filter: Optional OData filter expression (``$filter``), e.g.
                ``"evt_loaddate ne null or evt_loadtype ne null"``.
            orderby: Optional OData sort expression (``$orderby``).
            page_size: Records per page, sent as ``Prefer: odata.maxpagesize``.
            annotations: Optional annotation names to request with
                ``Prefer: odata.include-annotations``.
            max_retries: Retries per page when Dataverse throttles (429 or 503).
            timeout: HTTP timeout in seconds.
            session: Optional shared ``requests`` session.

        Returns:
            Dict with ``entity_set_name``, ``records`` (all pages, with
            ``@odata.*`` annotation keys removed), ``row_count``,
            ``page_count``, and ``timestamp`` (UTC ISO 8601).
        """
        logger = ToolkitLogger.get_logger("DynamicsCrmClient")
        pages = DynamicsCrmClient.iter_entity_pages(
            resource_url,
            entity_set_name,
            token,
            select=select,
            filter=filter,
            orderby=orderby,
            page_size=page_size,
            annotations=annotations,
            max_retries=max_retries,
            timeout=timeout,
            session=session,
        )

        records: list[dict[str, Any]] = []
        page_count = 0
        resolved_entity_set_name = entity_set_name
        for page in pages:
            page_count = page["page_number"]
            resolved_entity_set_name = page["entity_set_name"]
            records.extend(page["records"])

        logger.info(
            "Read %d record(s) from %s in %d page(s)",
            len(records),
            resolved_entity_set_name,
            page_count,
        )

        return {
            "entity_set_name": resolved_entity_set_name,
            "records": records,
            "row_count": len(records),
            "page_count": page_count,
            "timestamp": Dates.utc_now().isoformat(),
        }

    @staticmethod
    def bulk_update_entities(
        resource_url: str,
        entity_set_name: str,
        updates: list[dict[str, Any]],
        token: str,
        *,
        entity_type_name: Optional[str] = None,
        timeout: float = 30.0,
        session: Optional[requests.Session] = None,
        max_batch_size: int = 1000,
    ) -> dict[str, Any]:
        """Update multiple Dataverse entities using the Dataverse UpdateMultiple action.

        Each entry in ``updates`` may be either:
            {"entity_id": "<guid>", "values": {"column": "value"}}
        or
            {"entity_id": "<guid>", "field_values": {"column": "value"}}

        ``entity_type_name`` is the singular Dataverse logical type name used by
        ``@odata.type``. If omitted, it is derived by removing a trailing ``s``
        from ``entity_set_name``.

        This method posts a single batch request to the Dataverse custom action shape:
        ``{entity_set_name}/Microsoft.Dynamics.CRM.UpdateMultiple``
        and enforces the Dataverse batch limit of 1000 records per request. If a
        batch fails, it is recursively split in half until failed records are
        isolated without sending every record as an individual request.
        """
        resolved_resource_url = Validation.require_non_empty(resource_url, "resource_url")
        resolved_entity_set_name = Validation.require_non_empty(
            entity_set_name, "entity_set_name"
        )
        derived_entity_type_name = (
            resolved_entity_set_name[:-1]
            if resolved_entity_set_name.endswith("s")
            else resolved_entity_set_name
        )
        resolved_entity_type_name = Validation.require_non_empty(
            entity_type_name or derived_entity_type_name,
            "entity_type_name",
        )
        if not isinstance(updates, list) or not updates:
            raise ValueError("updates must be a non-empty list of entity update objects")
        if not isinstance(max_batch_size, int) or max_batch_size <= 0:
            raise ValueError("max_batch_size must be a positive integer")
        if len(updates) > max_batch_size:
            raise ValueError(
                f"updates exceeds the Dataverse batch limit of {max_batch_size} records"
            )

        normalized_updates: list[dict[str, Any]] = []
        for index, update in enumerate(updates):
            if not isinstance(update, dict):
                raise ValueError(f"Update at index {index} must be a dictionary")

            entity_id = update.get("entity_id")
            field_values = update.get("values")
            if field_values is None:
                field_values = update.get("field_values")
            if not entity_id:
                raise ValueError(f"Update at index {index} is missing entity_id")
            if not isinstance(field_values, dict) or not field_values:
                raise ValueError(
                    f"Update at index {index} is missing a non-empty field_values dict"
                )

            normalized_updates.append(
                {
                    "entity_id": str(entity_id),
                    "field_values": dict(field_values),
                }
            )

        http = session or requests
        url = (
            f"{resolved_resource_url.rstrip('/')}/api/data/v9.2/"
            f"{resolved_entity_set_name}/Microsoft.Dynamics.CRM.UpdateMultiple"
        )
        headers = DynamicsCrmClient.__build_auth_headers(token)

        successful_records: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []

        def submit_batch(batch: list[dict[str, Any]]) -> None:
            payload = {
                "Targets": [
                    {
                        "@odata.type": (
                            f"Microsoft.Dynamics.CRM.{resolved_entity_type_name}"
                        ),
                        f"{resolved_entity_type_name}id": item["entity_id"],
                        **item["field_values"],
                    }
                    for item in batch
                ]
            }
            response = http.post(url, headers=headers, json=payload, timeout=timeout)
            if response.status_code in (200, 204):
                successful_records.extend(batch)
                return

            if len(batch) > 1:
                midpoint = len(batch) // 2
                submit_batch(batch[:midpoint])
                submit_batch(batch[midpoint:])
                return

            record = batch[0]
            errors.append(
                {
                    "entity_id": record["entity_id"],
                    "field_values": record["field_values"],
                    "status_code": response.status_code,
                    "message": DynamicsCrmClient.__short_update_error(response),
                }
            )

        submit_batch(normalized_updates)

        return {
            "entity_set_name": resolved_entity_set_name,
            "total_records": len(normalized_updates),
            "success": len(errors) == 0,
            "success_count": len(successful_records),
            "error_count": len(errors),
            "updated_count": len(successful_records),
            "batch_size": len(normalized_updates),
            "status_code": 200 if not errors else 207,
            "errors": errors,
        }
