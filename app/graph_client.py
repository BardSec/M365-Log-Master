"""Microsoft Graph API client for sign-in log ingestion."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Iterator

import msal
import requests

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# Fields we select from Graph to keep payloads small
SIGNIN_SELECT_FIELDS = ",".join(
    [
        "id",
        "createdDateTime",
        "userId",
        "userDisplayName",
        "userPrincipalName",
        "appId",
        "appDisplayName",
        "ipAddress",
        "clientAppUsed",
        "status",
        "location",
        "deviceDetail",
        "conditionalAccessStatus",
        "riskDetail",
        "riskLevelAggregated",
        "isInteractive",
    ]
)

# Maximum retries on 429 / transient errors
MAX_RETRIES = 5


class GraphAuthError(Exception):
    pass


class GraphClient:
    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        scope: str = "https://graph.microsoft.com/.default",
    ):
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.scope = scope
        self._app: msal.ConfidentialClientApplication | None = None
        self._token_cache: dict[str, Any] = {}

    def _get_msal_app(self) -> msal.ConfidentialClientApplication:
        if self._app is None:
            authority = f"https://login.microsoftonline.com/{self.tenant_id}"
            self._app = msal.ConfidentialClientApplication(
                self.client_id,
                authority=authority,
                client_credential=self.client_secret,
            )
        return self._app

    def get_token(self) -> str:
        """Acquire (or reuse) an access token via client credentials."""
        app = self._get_msal_app()
        result = app.acquire_token_silent([self.scope], account=None)
        if not result:
            result = app.acquire_token_for_client(scopes=[self.scope])

        if "access_token" not in result:
            error = result.get("error_description", result.get("error", "unknown"))
            raise GraphAuthError(f"Failed to acquire token: {error}")

        return result["access_token"]

    def _get(self, url: str, params: dict | None = None) -> dict:
        """Perform a GET request with retry logic for 429 and 5xx errors."""
        token = self.get_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "ConsistencyLevel": "eventual",
        }

        delay = 2
        for attempt in range(MAX_RETRIES):
            try:
                resp = requests.get(url, headers=headers, params=params, timeout=30)
            except requests.RequestException as exc:
                logger.warning("Network error on attempt %d: %s", attempt + 1, exc)
                if attempt < MAX_RETRIES - 1:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise

            if resp.status_code == 200:
                return resp.json()

            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", delay))
                logger.warning(
                    "Rate-limited (429). Waiting %ds before retry %d/%d.",
                    retry_after,
                    attempt + 1,
                    MAX_RETRIES,
                )
                time.sleep(retry_after)
                # Refresh token after long wait
                token = self.get_token()
                headers["Authorization"] = f"Bearer {token}"
                delay = max(delay * 2, retry_after)
                continue

            if resp.status_code in (500, 502, 503, 504):
                logger.warning(
                    "Server error %d on attempt %d/%d. Retrying in %ds.",
                    resp.status_code,
                    attempt + 1,
                    MAX_RETRIES,
                    delay,
                )
                time.sleep(delay)
                delay *= 2
                continue

            # 401 – token refresh
            if resp.status_code == 401 and attempt == 0:
                self._app = None  # force re-auth
                token = self.get_token()
                headers["Authorization"] = f"Bearer {token}"
                continue

            # Include Graph's error body in the exception so it shows in sync logs
            try:
                detail = resp.json().get("error", {})
                msg = f"{detail.get('code', '')}: {detail.get('message', resp.text)}"
            except Exception:
                msg = resp.text
            raise requests.HTTPError(
                f"{resp.status_code} {resp.reason} – {msg}", response=resp
            )

        raise RuntimeError(f"Exceeded max retries for URL: {url}")

    def fetch_sign_in_logs(
        self,
        from_dt: datetime,
        to_dt: datetime,
        page_size: int = 500,
    ) -> Iterator[dict]:
        """
        Yield individual sign-in event dicts between from_dt and to_dt.

        Uses OData $filter on createdDateTime (server-side) and follows
        @odata.nextLink for pagination.
        """
        from_str = _fmt_dt(from_dt)
        to_str = _fmt_dt(to_dt)

        url = f"{GRAPH_BASE}/auditLogs/signIns"
        params = {
            "$filter": (
                f"createdDateTime ge {from_str} and createdDateTime lt {to_str}"
            ),
            "$select": SIGNIN_SELECT_FIELDS,
            "$top": str(page_size),
            "$orderby": "createdDateTime asc",
        }

        page = 1
        while url:
            logger.debug(
                "Fetching Graph sign-in page %d (from=%s to=%s)", page, from_str, to_str
            )
            data = self._get(url, params if page == 1 else None)
            events: list[dict] = data.get("value", [])
            logger.info(
                "Graph page %d returned %d events.", page, len(events)
            )
            yield from events

            url = data.get("@odata.nextLink")
            page += 1

    def test_connection(self) -> bool:
        """Verify credentials work by fetching one sign-in event."""
        try:
            url = f"{GRAPH_BASE}/auditLogs/signIns"
            self._get(url, {"$top": "1"})
            return True
        except Exception as exc:
            logger.error("Graph connection test failed: %s", exc)
            return False


def _fmt_dt(dt: datetime) -> str:
    """Format datetime as OData-compatible UTC ISO-8601 string."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
