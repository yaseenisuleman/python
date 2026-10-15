"""OpenObserve log ingestion.

Kestra usage::

    from python_toolkit.api.openobserve import OpenObserve

    OpenObserve.log_execution(
        {"event_type": "afterExecution", "status": "{{ execution.state }}",
         "started_at": "{{ execution.startDate }}"},
        "{{ envs.openobserve_url }}",
        "{{ envs.openobserve_user }}",
        "{{ envs.openobserve_password }}",
        execution_id="{{ execution.id }}",
        flow_id="{{ flow.id }}",
        namespace="{{ flow.namespace }}",
    )

Sends one record to the OpenObserve JSON ingestion API
(``{base_url}/api/{organization}/{stream}/_json``).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

import requests

from python_toolkit.common.json import JsonTools
from python_toolkit.common.logging import ToolkitLogger
from python_toolkit.common.validation import Validation

__all__ = ["OpenObserve"]

_SEVERITY_BY_STATUS = {
    "SUCCESS": "success",
    "FAILED": "error",
    "WARNING": "warning",
}


class OpenObserve:
    """Send execution events to OpenObserve."""

    @staticmethod
    def log_execution(
        event: str | dict[str, Any],
        base_url: str,
        username: str,
        password: str,
        *,
        execution_id: str = "",
        flow_id: str = "",
        namespace: str = "",
        instance_name: str = "production",
        event_name: str = "kestra_execution",
        organization: str = "default",
        stream: str = "kestra_executions",
        verify_ssl: bool = False,
        timeout: float = 2.0,
        session: Optional[requests.Session] = None,
    ) -> dict[str, Any]:
        """Post an execution event to an OpenObserve stream.

        Args:
            event: JSON string or dict with ``started_at`` (ISO 8601) and optionally
                ``event_type``, ``status`` and ``metadata`` (dict or JSON string).
            base_url: OpenObserve URL, e.g. ``http://10.20.3.81:5080``.
            username: OpenObserve user.
            password: OpenObserve password.
            execution_id: ID of the execution being logged.
            flow_id: ID of the flow being logged.
            namespace: Namespace of the flow being logged.
            instance_name: Instance label stored on the record.
            event_name: Value stored in the record's ``event`` field.
            organization: OpenObserve organization.
            stream: OpenObserve stream name.
            verify_ssl: Verify the server's TLS certificate.
            timeout: HTTP timeout in seconds.
            session: Optional shared ``requests`` session.

        Returns:
            Dict with ``stream``, ``status_code`` and the ``payload`` that was sent.
        """
        logger = ToolkitLogger.get_logger("OpenObserve")
        if isinstance(event, dict):
            event = dict(event)
        else:
            event = JsonTools.validate_object(event, name="event", required_keys=["started_at"])
        started_at_text = Validation.require_non_empty(str(event.get("started_at") or ""), "event.started_at")
        resolved_base_url = Validation.require_non_empty(base_url, "base_url").rstrip("/")
        resolved_username = Validation.require_non_empty(username, "username")
        password = Validation.require_not_none(password, "password")

        # Local wall-clock times, so started_at and ended_at are comparable.
        started_at = datetime.fromisoformat(
            started_at_text.replace("Z", "+00:00")
        ).astimezone().replace(tzinfo=None)
        ended_at = datetime.now()

        status = event.get("status")
        payload: dict[str, Any] = {
            "instance_name": instance_name,
            "event": event_name,
            "execution_id": execution_id,
            "flow_id": flow_id,
            "namespace": namespace,
            "event_type": event.get("event_type"),
            "status": status,
            "started_at": started_at.isoformat(),
            "ended_at": ended_at.isoformat(),
            "duration_seconds": (ended_at - started_at).total_seconds(),
            "severity": _SEVERITY_BY_STATUS.get(status, "info"),
        }

        metadata = event.get("metadata")
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except json.JSONDecodeError:
                pass
        if metadata is not None:
            payload["metadata"] = json.dumps(metadata, indent=2)

        if not verify_ssl:
            import urllib3

            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        url = f"{resolved_base_url}/api/{organization}/{stream}/_json"
        http = session or requests
        try:
            response = http.post(
                url,
                json=[payload],
                auth=(resolved_username, password),
                verify=verify_ssl,
                timeout=timeout,
            )
            response.raise_for_status()
        except Exception as exc:
            logger.error("OpenObserve logging failed: %s: %s", type(exc).__name__, exc)
            error_response = getattr(exc, "response", None)
            if error_response is not None:
                logger.error("OpenObserve error response: %s", error_response.text)
            raise

        logger.info("Logged %s event for %s to OpenObserve stream %s", status, flow_id, stream)

        return {
            "stream": stream,
            "status_code": response.status_code,
            "payload": payload,
        }
