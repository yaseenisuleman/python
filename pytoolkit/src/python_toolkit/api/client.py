"""HTTP API client wrapper.

Kestra usage::

    from python_toolkit.api.client import ApiClient
"""

from __future__ import annotations

from types import TracebackType
from typing import Any, Mapping, MutableMapping, Optional

import requests

from python_toolkit.common.logging import ToolkitLogger
from python_toolkit.common.validation import Validation


class ApiClient:
    """Simple requests-based HTTP client."""

    def __init__(
        self,
        base_url: str,
        *,
        headers: Optional[Mapping[str, str]] = None,
        timeout: float = 30.0,
        session: Optional[requests.Session] = None,
    ) -> None:
        self._logger = ToolkitLogger.get_logger(self.__class__.__name__)
        self.base_url = Validation.require_non_empty(base_url.rstrip("/"), "base_url")
        self.timeout = timeout
        self._owns_session = session is None
        self.session = session or requests.Session()
        self._headers: MutableMapping[str, str] = dict(headers or {})

    def close(self) -> None:
        """Close the session when this client created it."""
        if self._owns_session:
            self.session.close()

    def __enter__(self) -> ApiClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        json: Optional[Any] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> requests.Response:
        """Send an HTTP request and raise on error status."""
        url = f"{self.base_url}/{path.lstrip('/')}"
        merged_headers = {**self._headers, **(headers or {})}

        self._logger.info("HTTP %s %s", method.upper(), url)
        response = self.session.request(
            method=method.upper(),
            url=url,
            params=params,
            json=json,
            headers=merged_headers,
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response

    def get(self, path: str, **kwargs: Any) -> requests.Response:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> requests.Response:
        return self.request("POST", path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> requests.Response:
        return self.request("PUT", path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> requests.Response:
        return self.request("DELETE", path, **kwargs)
