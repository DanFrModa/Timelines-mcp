#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "mcp>=1.2.0,<3",
#   "httpx>=0.27.0",
#   "pydantic>=2.0.0",
#   "uvicorn>=0.30.0",
#   "starlette>=0.37.0",
# ]
# ///
"""MCP server for the TimelinesAI Public API (https://app.timelines.ai/integrations/api).

TimelinesAI is a WhatsApp inbox for teams: several connected WhatsApp numbers,
their chats, the people responsible for each chat, and labels on top. This server
authenticates with a TimelinesAI API token (prefix ``tla_``) and exposes typed
tools for reading that inbox, plus a generic request tool for anything else.

The important difference from a normal API: writing here talks to real people.
A sent WhatsApp message cannot be unsent, so sending is gated separately from
every other write and is OFF unless explicitly enabled.

Environment variables:
    TIMELINES_API_TOKEN     Required. API token, e.g. ``tla_abc123...``
    TIMELINES_MCP_TRANSPORT Optional. ``stdio`` (default) or ``http`` for a remote server
    MCP_AUTH_TOKEN          Required when transport is ``http``. Min 32 chars
    TIMELINES_READ_ONLY     Optional. ``1`` blocks every write. See defaults below
    TIMELINES_ALLOW_SEND    Optional. ``1`` to permit sending WhatsApp messages
    TIMELINES_API_BASE      Optional. Defaults to the URL above
    TIMELINES_MAX_CHARS     Optional. Response truncation limit, default 20000
    TIMELINES_TIMEOUT       Optional. Request timeout in seconds, default 45
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union
from urllib.parse import urlencode

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

try:  # mcp SDK 1.x
    from mcp.server.fastmcp import FastMCP as _Server
except ImportError:  # mcp SDK 2.x renamed FastMCP to MCPServer
    from mcp.server.mcpserver import MCPServer as _Server

mcp = _Server("timelines_mcp")

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

DEFAULT_API_BASE = "https://app.timelines.ai/integrations/api"
API_BASE_URL: str = os.environ.get("TIMELINES_API_BASE", DEFAULT_API_BASE).rstrip("/")
MAX_CHARS: int = int(os.environ.get("TIMELINES_MAX_CHARS", "20000"))
REQUEST_TIMEOUT: float = float(os.environ.get("TIMELINES_TIMEOUT", "45"))

TRANSPORT: str = os.environ.get("TIMELINES_MCP_TRANSPORT", "stdio").strip().lower()
IS_REMOTE: bool = TRANSPORT in {"http", "streamable-http", "streamable_http"}

# Read-only defaults differ by transport, deliberately.
#   stdio  -> writes ALLOWED by default (single trusted user on their own machine)
#   http   -> writes BLOCKED by default (network-reachable; must opt in explicitly)
_READ_ONLY_RAW: str = os.environ.get("TIMELINES_READ_ONLY", "").strip().lower()
if IS_REMOTE:
    READ_ONLY: bool = _READ_ONLY_RAW not in {"0", "false", "no"}
else:
    READ_ONLY = _READ_ONLY_RAW in {"1", "true", "yes"}

# Sending is its own gate, and it is off by default on BOTH transports.
#
# Every other write here is internal and reversible: a label can be removed, a
# chat can be reopened, an assignment can be changed. Sending a WhatsApp message
# is neither — it reaches a real person's phone immediately and there is no
# unsend. That asymmetry deserves its own switch rather than riding along with
# TIMELINES_READ_ONLY.
ALLOW_SEND: bool = os.environ.get("TIMELINES_ALLOW_SEND", "").strip().lower() in {
    "1",
    "true",
    "yes",
}

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# TimelinesAI documents 50 requests/minute per workspace, plus a monthly cap of
# 200,000 calls. The per-minute figure is what a paginating scan runs into, so
# anything that loops over pages has to space itself out by at least this much.
# Going faster does not fail loudly: it returns 429 partway through, after the
# work is already half done.
READ_RATE_LIMIT_PER_MIN = 50
MIN_REQUEST_INTERVAL = 60.0 / READ_RATE_LIMIT_PER_MIN  # 1.2 seconds

# Fallback wait when a 429 arrives without a Retry-After header.
DEFAULT_RETRY_AFTER = 5.0

# Paths that put a message on someone's phone. Matched as prefixes against the
# normalized path; any POST landing here needs ALLOW_SEND *and* confirm=True.
SEND_PATH_MARKERS: Tuple[str, ...] = (
    "/messages",           # POST /messages  (send by phone number)
    "/messages/send",
)


def _is_send_path(path: str, method: str) -> bool:
    """True when this call would deliver a WhatsApp message.

    Sends look like ``POST /messages`` or ``POST /chats/{id}/messages``. Reading
    history is ``GET /chats/{id}/messages`` — same path, harmless verb — so the
    method has to be part of the test.
    """
    if method != "POST":
        return False
    clean = path.split("?")[0].rstrip("/")
    if clean.endswith("/messages"):
        return True
    return any(clean.startswith(m) for m in SEND_PATH_MARKERS)


# Destructive beyond sending: removing things other people depend on.
CONFIRM_REQUIRED_PREFIXES: Tuple[str, ...] = (
    "/files",                  # DELETE /files/{uid}
    "/webhooks",               # PUT/DELETE reconfigures live event delivery
    "/workspace/invitations",  # revoking a teammate's access
)

# Endpoints from the published API reference, used by the discover tool. The
# docs are thin on pagination and on which of these a given token may touch,
# which is exactly why probing them is worth a tool.
KNOWN_ENDPOINTS: Tuple[str, ...] = (
    "/workspace",
    "/workspace/teammates",
    "/whatsapp_accounts",
    "/chats",
    "/files",
    "/webhooks",
)


class AuthConfigError(RuntimeError):
    """Raised when the server is missing a usable TimelinesAI token."""


def _get_token() -> str:
    token = os.environ.get("TIMELINES_API_TOKEN", "").strip()
    if not token:
        raise AuthConfigError(
            "TIMELINES_API_TOKEN is not set. Add it to the server's env block in "
            "claude_desktop_config.json, e.g. \"env\": {\"TIMELINES_API_TOKEN\": \"tla_...\"}. "
            "Create a token in the TimelinesAI dashboard under the API/integrations settings."
        )
    return token


def _auth_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {_get_token()}",
        "Accept": "application/json",
        "User-Agent": "timelines-mcp/1.0",
    }


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------


class ResponseFormat(str, Enum):
    """Output format for tool responses."""

    JSON = "json"
    MARKDOWN = "markdown"


class HttpMethod(str, Enum):
    """HTTP verbs supported by the generic request tool."""

    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    PATCH = "PATCH"
    DELETE = "DELETE"


QueryValue = Union[str, int, float, bool, None, List[Union[str, int, float, bool]]]


def _stringify(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _build_query(query: Optional[Dict[str, QueryValue]]) -> str:
    """Serialize query parameters the way TimelinesAI expects them.

    Multi-value filters are comma-joined in a single parameter — ``label=vip,enterprise``
    — not repeated pairs and not bracket notation. Passing a Python list here
    produces that form.
    """
    if not query:
        return ""
    pairs: List[Tuple[str, str]] = []
    for key, value in query.items():
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            items = [_stringify(v) for v in value if v is not None]
            if items:
                pairs.append((key, ",".join(items)))
        else:
            pairs.append((key, _stringify(value)))
    return urlencode(pairs, doseq=False) if pairs else ""


def _normalize_path(path: str) -> str:
    """Turn a user-supplied path into an absolute URL against the API base."""
    path = path.strip()
    if path.startswith("http://") or path.startswith("https://"):
        return path
    if not path.startswith("/"):
        path = "/" + path
    # Tolerate callers who repeat the prefix already present in the base.
    for prefix in ("/integrations/api", "/api"):
        if API_BASE_URL.endswith(prefix) and path.startswith(prefix + "/"):
            path = path[len(prefix):]
            break
    return f"{API_BASE_URL}{path}"


def _api_path(url_or_path: str) -> str:
    """The path portion of a normalized URL, for gate checks."""
    if url_or_path.startswith(API_BASE_URL):
        return url_or_path[len(API_BASE_URL):]
    return url_or_path


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return (
        text[:limit]
        + f"\n\n... [truncated: {len(text) - limit} more characters. "
        "Narrow the request with more filters or a `fields` selection, or raise "
        "TIMELINES_MAX_CHARS. Page size is fixed at 50 and cannot be lowered.]"
    )


def _select_fields(data: Any, fields: Optional[Sequence[str]]) -> Any:
    """Keep only ``fields`` on each record, to reduce response size.

    TimelinesAI wraps payloads as {"status":"ok","data":{...}}, and list payloads
    carry a "has_more_pages" flag beside the records, so envelope keys have to be
    walked through rather than pruned or the pagination metadata disappears.

    Identifying the envelope by key name alone does not survive contact with this
    API: a message record has its own key called ``data`` (a dict of metadata),
    so a name-based test reads every message as an envelope and prunes nothing.
    The reliable signal is position — **anything inside a list is a record** — and
    that is what decides here. Key names are only consulted for dicts, where
    there is no list to go by.

    Since page size is fixed at 50, `fields` is the only lever left for keeping a
    response readable, so it failing silently is worse than it not existing.
    """
    if not fields:
        return data
    keep = set(fields)
    containers = ("data", "chats", "messages", "items", "results", "records")

    def prune_record(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: v for k, v in node.items() if k in keep}
        return node

    def walk(node: Any) -> Any:
        if isinstance(node, list):
            # List members are records, whatever their keys happen to be called.
            return [prune_record(item) for item in node]
        if isinstance(node, dict):
            if any(k in containers for k in node):
                return {k: (walk(v) if k in containers else v) for k, v in node.items()}
            return prune_record(node)
        return node

    return walk(data)


def _format_error(exc: Exception, url: str) -> str:
    """Turn an exception into an actionable message for the agent.

    TimelinesAI returns a structured error body —
    {"status":"error","message":...,"error_code":...,"errors":[{"fields":[],"msg":""}]}
    — and the per-field entries are the fastest way to see what was rejected.
    """
    if isinstance(exc, AuthConfigError):
        return f"Error: {exc}"
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        body = exc.response.text[:2000]
        detail = ""
        try:
            payload = exc.response.json()
            bits = []
            if payload.get("error_code"):
                bits.append(f"error_code={payload['error_code']}")
            if payload.get("message"):
                bits.append(payload["message"])
            for entry in payload.get("errors") or []:
                fields = ", ".join(entry.get("fields") or [])
                bits.append(f"[{fields}] {entry.get('msg', '')}".strip())
            detail = "\n".join(bits)
        except Exception:  # noqa: BLE001 - body was not the expected JSON shape
            pass
        hints = {
            400: "Bad request. Check the field names against the error entries below. "
                 "Phone numbers must be international format: +14840000000.",
            401: "Unauthorized. The token is missing, expired or revoked. Verify "
                 "TIMELINES_API_TOKEN in the TimelinesAI dashboard.",
            403: "Forbidden. The token's workspace does not include this resource, or "
                 "the plan does not cover the Public API.",
            404: "Not found. The chat, message or file id does not exist in this workspace.",
            409: "Conflict. The resource is in a state that blocks this change.",
            422: "Validation failed. Inspect the per-field errors below.",
            429: f"Rate limited. TimelinesAI allows {READ_RATE_LIMIT_PER_MIN} requests "
                 "per minute per workspace (plus 200,000 calls a month), and this applies "
                 "to READS, not just sends. Respect the Retry-After header on the response, "
                 "and prefer filters over scanning many pages.",
        }
        hint = hints.get(status, "")
        if status >= 500:
            hint = "TimelinesAI server error. Retry shortly."
        return f"Error {status} on {url}\n{hint}\n{detail}\n\nResponse body:\n{body}".strip()
    if isinstance(exc, httpx.TimeoutException):
        return (
            f"Error: request to {url} timed out after {REQUEST_TIMEOUT}s. Retry with a "
            "smaller page, or raise TIMELINES_TIMEOUT. If this was a send, check the chat "
            "before retrying so the message does not go out twice."
        )
    if isinstance(exc, httpx.RequestError):
        return f"Error: could not reach {url} ({type(exc).__name__}: {exc})."
    return f"Error: unexpected {type(exc).__name__}: {exc}"


def _retry_after_from_response(response: "httpx.Response") -> float:
    """Seconds to wait per the server's Retry-After header, with a sane fallback.

    Honouring the header beats guessing: too short and the retry is refused
    again, too long and everything crawls. The value is capped so a malformed or
    hostile header cannot stall the server for hours.
    """
    raw = response.headers.get("retry-after", "").strip()
    if not raw:
        return DEFAULT_RETRY_AFTER
    try:
        return max(0.0, min(float(raw), 120.0))
    except ValueError:
        # The header may be an HTTP date rather than a count of seconds.
        return DEFAULT_RETRY_AFTER


def _retry_after_seconds(exc: Exception) -> Optional[float]:
    """Seconds to wait after a 429, or None when the error is not a 429."""
    if not isinstance(exc, httpx.HTTPStatusError):
        return None
    if exc.response.status_code != 429:
        return None
    return _retry_after_from_response(exc.response)


def _check_write_allowed(method: str, path: str, confirmed: bool) -> None:
    """Run every write gate in order, raising PermissionError on the first refusal.

    Three independent gates:
      1. READ_ONLY  - the deployment is a reader, full stop.
      2. send       - the call would message a real person and sending is off.
      3. confirm    - the call is destructive and did not say so explicitly.
    """
    if method not in WRITE_METHODS:
        return

    if READ_ONLY:
        raise PermissionError(
            f"{method} blocked: this server runs with TIMELINES_READ_ONLY enabled. "
            "Set TIMELINES_READ_ONLY=0 to allow writes."
        )

    clean = _api_path(path).split("?")[0]

    if _is_send_path(clean, method):
        if not ALLOW_SEND:
            raise PermissionError(
                "Sending blocked: TIMELINES_ALLOW_SEND is not set. This call would "
                "deliver a WhatsApp message to a real person's phone, and there is no "
                "unsend. Set TIMELINES_ALLOW_SEND=1 only if that is genuinely intended."
            )
        if not confirmed:
            raise PermissionError(
                "Sending blocked: pass confirm=true. Before doing so, show the user the "
                "exact recipient and the exact message text and get their agreement — "
                "a sent WhatsApp message cannot be taken back."
            )
        return

    if not confirmed and any(clean.startswith(p) for p in CONFIRM_REQUIRED_PREFIXES):
        raise PermissionError(
            f"{method} {clean} blocked: this endpoint removes or reconfigures something "
            "others depend on. Re-issue the call with confirm=true after checking with "
            "the user."
        )


class _Throttle:
    """Process-wide pacer that keeps outbound calls under the documented rate.

    The 50/minute limit is per workspace, and every tool in this server shares
    one workspace, so the pacing has to be shared too. Spacing requests at the
    source is better than reacting to 429s: a rejected request still costs a
    round trip, and the failure lands mid-task rather than up front.

    An isolated call waits for nothing — the delay only appears when a previous
    call happened less than MIN_REQUEST_INTERVAL ago, which is exactly the burst
    case that trips the limit.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._last: float = 0.0

    async def wait(self) -> None:
        async with self._lock:
            now = asyncio.get_event_loop().time()
            gap = now - self._last
            if gap < MIN_REQUEST_INTERVAL:
                await asyncio.sleep(MIN_REQUEST_INTERVAL - gap)
            self._last = asyncio.get_event_loop().time()


_throttle = _Throttle()


async def _api_request(
    method: str,
    path: str,
    *,
    query: Optional[Dict[str, QueryValue]] = None,
    body: Optional[Any] = None,
    confirmed: bool = False,
) -> Tuple[int, Any, str]:
    """Perform an authenticated request. Returns (status, parsed_body, final_url).

    Paces itself against the workspace rate limit and, for reads, retries once
    when the server answers 429. Writes are never retried automatically: a send
    that may or may not have gone out is not something to repeat on a guess.
    """
    method = method.upper()
    url = _normalize_path(path)
    # Gate on the normalized URL so that "/messages" and "/integrations/api/messages"
    # are recognised as the same endpoint by the send and confirm checks.
    _check_write_allowed(method, url, confirmed)

    qs = _build_query(query)
    if qs:
        url = f"{url}{'&' if '?' in url else '?'}{qs}"

    headers = _auth_headers()
    if body is not None:
        headers["Content-Type"] = "application/json"

    # Reads are safe to repeat; a write is not, so it gets one shot.
    attempts = 2 if method == "GET" else 1
    response = None
    for attempt in range(attempts):
        await _throttle.wait()
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, follow_redirects=True) as client:
            response = await client.request(method, url, headers=headers, json=body)
        if response.status_code != 429 or attempt == attempts - 1:
            break
        # Wait exactly as long as the server asked before the single retry.
        await asyncio.sleep(_retry_after_from_response(response))

    assert response is not None
    raw = response.text
    try:
        parsed: Any = response.json()
    except ValueError:
        parsed = None

    response.raise_for_status()

    # TimelinesAI answers 200 with {"status":"error", ...} in some failure modes,
    # so a 2xx alone does not mean the call worked. Surface that as an error.
    if isinstance(parsed, dict) and parsed.get("status") == "error":
        message = parsed.get("message") or "the API reported status=error"
        raise RuntimeError(f"TimelinesAI returned status=error on {url}: {message}")

    return response.status_code, parsed if parsed is not None else raw, url


async def _run(
    method: str,
    path: str,
    *,
    query: Optional[Dict[str, QueryValue]] = None,
    body: Optional[Any] = None,
    fields: Optional[Sequence[str]] = None,
    response_format: ResponseFormat = ResponseFormat.JSON,
    max_chars: Optional[int] = None,
    confirmed: bool = False,
) -> str:
    """Execute a request and render the result, converting errors to text."""
    url = _normalize_path(path)
    try:
        status, data, url = await _api_request(
            method, path, query=query, body=body, confirmed=confirmed
        )
    except PermissionError as exc:
        return f"Error: {exc}"
    except RuntimeError as exc:
        return f"Error: {exc}"
    except Exception as exc:  # noqa: BLE001 - surfaced to the agent as text
        return _format_error(exc, url)

    data = _select_fields(data, fields)
    limit = max_chars or MAX_CHARS

    if response_format is ResponseFormat.MARKDOWN:
        return _truncate(_to_markdown(data, method, url, status), limit)

    payload: Dict[str, Any] = {"status": status, "url": url, "data": data}
    if _has_more_pages(data):
        payload["next_page_hint"] = (
            "More results exist. Increment `page` to continue."
        )
    return _truncate(json.dumps(payload, indent=2, ensure_ascii=False, default=str), limit)


def _has_more_pages(data: Any) -> bool:
    """Whether a list payload says another page is waiting.

    The flag can sit at the top level or inside the "data" envelope depending on
    the endpoint, so both are checked.
    """
    if not isinstance(data, dict):
        return False
    if data.get("has_more_pages"):
        return True
    inner = data.get("data")
    return isinstance(inner, dict) and bool(inner.get("has_more_pages"))


def _extract_records(data: Any) -> Optional[List[Any]]:
    """Find the list of records inside a TimelinesAI payload.

    Shapes seen in the wild: a bare list, {"data": [...]}, and
    {"data": {"chats": [...], "has_more_pages": false}}.
    """
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return None
    for key in ("chats", "messages", "items", "results", "records", "data"):
        value = data.get(key)
        if isinstance(value, list):
            return value
    inner = data.get("data")
    if isinstance(inner, dict):
        return _extract_records(inner)
    return None


def _to_markdown(data: Any, method: str, url: str, status: int) -> str:
    lines = [f"**{method} {url}** -> {status}", ""]
    records = _extract_records(data)
    if records is None:
        lines.append("```json")
        lines.append(json.dumps(data, indent=2, ensure_ascii=False, default=str))
        lines.append("```")
        return "\n".join(lines)

    lines.append(f"{len(records)} record(s)" + (" (more pages available)" if _has_more_pages(data) else ""))
    lines.append("")
    for index, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            lines.append(f"{index}. {record}")
            continue
        title = (
            record.get("name")
            or record.get("chat_name")
            or record.get("phone")
            or record.get("id")
            or f"Record {index}"
        )
        lines.append(f"### {title}")
        for key, value in record.items():
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False, default=str)
            lines.append(f"- **{key}**: {value}")
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Input models
# --------------------------------------------------------------------------


class _Base(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True, extra="forbid")


class ListInput(_Base):
    """Shared pagination and shaping parameters for TimelinesAI list endpoints.

    Page size is NOT adjustable. Verified against the live API on 2026-08-25:
    ``limit``, ``per_page``, ``page_size``, ``size``, ``count``, ``take`` and
    ``rows`` are all ignored and every page comes back with 50 records. Only
    ``page`` does anything, and ``has_more_pages`` says whether to ask for the
    next one.
    """

    page: Optional[int] = Field(default=1, description="1-based page number", ge=1)
    extra_query: Optional[Dict[str, QueryValue]] = Field(
        default=None,
        description=(
            "Additional query parameters merged into the request. List values are "
            "comma-joined, which is how this API takes multi-value filters: "
            "{'label': ['vip', 'enterprise']} -> label=vip,enterprise"
        ),
    )
    fields: Optional[List[str]] = Field(
        default=None,
        description="Keep only these keys on each returned record, to shrink the response",
        max_length=50,
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.JSON, description="'json' (default) or 'markdown'"
    )

    def to_query(self) -> Dict[str, QueryValue]:
        query: Dict[str, QueryValue] = {}
        if self.page is not None:
            query["page"] = self.page
        if self.extra_query:
            query.update(self.extra_query)
        return query


class ChatsInput(ListInput):
    """Input model for listing chats with the documented filters."""

    label: Optional[List[str]] = Field(
        default=None,
        description="Keep chats carrying any of these labels, e.g. ['vip', 'enterprise']",
        max_length=50,
    )
    responsible: Optional[str] = Field(
        default=None,
        description="Email of the teammate responsible for the chat, e.g. 'ana@empresa.com'",
    )
    read: Optional[bool] = Field(
        default=None, description="False returns only chats with unread messages"
    )
    group: Optional[bool] = Field(
        default=None, description="True returns only group chats, False only 1:1"
    )
    name: Optional[str] = Field(
        default=None, description="Substring match on the chat or contact name"
    )
    created_after: Optional[str] = Field(
        default=None, description="Only chats created after this date, ISO 8601"
    )

    def to_query(self) -> Dict[str, QueryValue]:
        query = super().to_query()
        if self.label:
            query["label"] = self.label
        if self.responsible:
            query["responsible"] = self.responsible
        if self.read is not None:
            query["read"] = self.read
        if self.group is not None:
            query["group"] = self.group
        if self.name:
            query["name"] = self.name
        if self.created_after:
            query["created_after"] = self.created_after
        return query


class RequestInput(_Base):
    """Input model for the generic TimelinesAI API request tool."""

    method: HttpMethod = Field(default=HttpMethod.GET, description="HTTP method")
    path: str = Field(
        ...,
        description=(
            "API path relative to https://app.timelines.ai/integrations/api, e.g. "
            "'/chats' or '/chats/123/messages'. Absolute URLs are allowed."
        ),
        min_length=1,
        max_length=500,
    )
    query: Optional[Dict[str, QueryValue]] = Field(
        default=None,
        description="Query parameters. Lists are comma-joined: {'label': ['a','b']} -> label=a,b",
    )
    body: Optional[Any] = Field(
        default=None, description="JSON request body for POST/PUT/PATCH/DELETE"
    )
    confirm: bool = Field(
        default=False,
        description=(
            "Required for sending messages and for destructive endpoints. Set it only "
            "after the user has agreed to that specific action."
        ),
    )
    fields: Optional[List[str]] = Field(
        default=None, description="Keep only these keys on each returned record", max_length=50
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.JSON, description="'json' (default) or 'markdown'"
    )
    max_chars: Optional[int] = Field(
        default=None, description="Override the response truncation limit", ge=500, le=200000
    )

    @field_validator("path")
    @classmethod
    def _no_blank_path(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("path cannot be empty")
        return v.strip()


class SendMessageInput(_Base):
    """Input model for sending a WhatsApp message.

    Addressing is either by phone number (POST /messages) or by chat id
    (POST /chats/{id}/messages). Exactly one must be given.
    """

    phone: Optional[str] = Field(
        default=None,
        description=(
            "Recipient in international format, e.g. '+5215512345678'. Use this to "
            "reach someone who may not have a chat yet."
        ),
        max_length=32,
    )
    chat_id: Optional[Union[int, str]] = Field(
        default=None,
        description="Existing chat id, to reply inside a conversation that already exists",
    )
    text: Optional[str] = Field(
        default=None, description="Message body, plain text, max 2000 characters", max_length=2000
    )
    whatsapp_account_phone: Optional[str] = Field(
        default=None,
        description=(
            "Which connected WhatsApp number sends it, in international format. Omitted, "
            "TimelinesAI uses the most recently connected account in the workspace — "
            "which may not be the one the user expects."
        ),
        max_length=32,
    )
    file_uid: Optional[str] = Field(
        default=None, description="Attachment uid from the files endpoints"
    )
    label: Optional[str] = Field(
        default=None, description="Label to apply to the chat, max 64 characters", max_length=64
    )
    chat_name: Optional[str] = Field(
        default=None,
        description="Exact chat or group name, to target a named group",
        max_length=256,
    )
    confirm: bool = Field(
        default=False,
        description=(
            "REQUIRED. Set true only after showing the user the exact recipient and the "
            "exact text and getting their agreement. A sent message cannot be unsent."
        ),
    )
    extra_body: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Any further documented fields, e.g. {'attachment_template_id': 12}",
    )

    @field_validator("phone", "whatsapp_account_phone")
    @classmethod
    def _international_format(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        clean = v.strip().replace(" ", "").replace("-", "")
        if not clean.startswith("+"):
            raise ValueError(
                f"'{v}' must be in international format starting with '+', "
                "e.g. '+5215512345678'"
            )
        return clean


class ChatIdInput(_Base):
    """Input model for endpoints addressed by chat id."""

    chat_id: Union[int, str] = Field(..., description="The chat id")
    fields: Optional[List[str]] = Field(
        default=None, description="Keep only these keys on the returned object", max_length=50
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.JSON, description="'json' (default) or 'markdown'"
    )


class MessagesInput(ListInput):
    """Input model for reading a chat's message history."""

    chat_id: Union[int, str] = Field(..., description="The chat whose messages to read")


class UpdateChatInput(_Base):
    """Input model for updating a chat: assignment, read state, open/closed."""

    chat_id: Union[int, str] = Field(..., description="The chat to update")
    responsible: Optional[str] = Field(
        default=None, description="Email of the teammate to assign the chat to"
    )
    closed: Optional[bool] = Field(
        default=None, description="True closes the chat, False reopens it"
    )
    read: Optional[bool] = Field(default=None, description="Mark the chat read or unread")
    extra_body: Optional[Dict[str, Any]] = Field(
        default=None, description="Any further documented update fields"
    )


class LabelsInput(_Base):
    """Input model for reading and changing the labels on a chat."""

    chat_id: Union[int, str] = Field(..., description="The chat whose labels to act on")
    action: str = Field(
        default="list",
        description=(
            "'list' reads them, 'add' appends without touching existing ones, "
            "'replace' overwrites the whole set — replace removes any label not listed."
        ),
        pattern="^(list|add|replace)$",
    )
    labels: Optional[List[str]] = Field(
        default=None,
        description="Label names, required for 'add' and 'replace'",
        max_length=50,
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.JSON, description="'json' (default) or 'markdown'"
    )


class DiscoverInput(_Base):
    """Input model for probing which endpoints exist and are permitted."""

    paths: Optional[List[str]] = Field(
        default=None,
        description="Paths to probe with GET. Defaults to the documented endpoint list.",
        max_length=60,
    )
    include_sample: bool = Field(
        default=False, description="Include a short sample of each successful response body"
    )


class ActivitySummaryInput(_Base):
    """Input model for the aggregated inbox report."""

    max_pages: int = Field(
        default=10,
        description=(
            "Safety cap on pagination. The API returns a fixed 50 chats per page, so "
            "10 pages covers 500. Raise for big workspaces, but expect a slower call: "
            "each page is one round trip."
        ),
        ge=1,
        le=100,
    )
    filters: Optional[Dict[str, QueryValue]] = Field(
        default=None,
        description="Optional filters passed straight to /chats, e.g. {'group': False}",
    )


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------


@mcp.tool(
    name="timelines_whoami",
    annotations={
        "title": "Check TimelinesAI Token, Workspace and Gates",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def timelines_whoami() -> str:
    """Verify the token works and report the workspace, quota and write gates.

    Run this first in any session. It confirms authentication, names the
    workspace the token belongs to, and — most importantly before any write —
    states plainly whether this deployment is allowed to send WhatsApp messages.

    Returns:
        str: JSON of the shape
            {
              "api_base": str,
              "token_preview": str,        # first 10 characters only
              "authenticated": bool,
              "workspace": <the /workspace payload> | null,
              "gates": {"read_only": bool, "allow_send": bool,
                        "sending_effectively_enabled": bool},
              "probes": [{"path": str, "status": int | str}]
            }

    Examples:
        - Use when: starting a session, or after a 401/403 from another tool
        - Use when: the user asks "can you actually send from here?"
        - Don't use when: you need chats or messages (use the list tools)

    Error Handling:
        - authenticated=false with 401 everywhere means the token is invalid or
          revoked; check TIMELINES_API_TOKEN in the TimelinesAI dashboard
        - 403 on every probe usually means the plan does not include the Public API
    """
    try:
        token = _get_token()
        headers = _auth_headers()
    except AuthConfigError as exc:
        return f"Error: {exc}"

    probes = ["/workspace", "/whatsapp_accounts", "/chats", "/workspace/teammates"]
    results: List[Dict[str, Any]] = []
    workspace: Any = None
    authenticated = False

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, follow_redirects=True) as client:
            for path in probes:
                url = _normalize_path(path)
                params: Dict[str, str] = {}
                try:
                    response = await client.get(url, headers=headers, params=params)
                except httpx.RequestError as exc:
                    results.append({"path": path, "status": type(exc).__name__})
                    continue
                results.append({"path": path, "status": response.status_code})
                if 200 <= response.status_code < 300:
                    authenticated = True
                    if path == "/workspace" and workspace is None:
                        try:
                            workspace = response.json()
                        except ValueError:
                            workspace = response.text[:500]
    except Exception as exc:  # noqa: BLE001 - surfaced to the agent as text
        return _format_error(exc, API_BASE_URL)

    result = {
        "api_base": API_BASE_URL,
        "token_preview": token[:10] + "...",
        "authenticated": authenticated,
        "workspace": workspace,
        "gates": {
            "read_only": READ_ONLY,
            "allow_send": ALLOW_SEND,
            "sending_effectively_enabled": (not READ_ONLY) and ALLOW_SEND,
        },
        "probes": results,
    }
    return _truncate(json.dumps(result, indent=2, ensure_ascii=False, default=str), MAX_CHARS)


@mcp.tool(
    name="timelines_request",
    annotations={
        "title": "TimelinesAI API Request (any endpoint)",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def timelines_request(params: RequestInput) -> str:
    """Call any endpoint on the TimelinesAI Public API with the configured token.

    The general-purpose escape hatch, for anything the typed tools do not cover:
    files, webhooks, reactions, teammate invitations. Prefer the typed tools when
    they fit, since they carry the correct parameter names and the right gates.

    Args:
        params (RequestInput): Validated parameters containing:
            - method (HttpMethod): GET, POST, PUT, PATCH or DELETE (default GET)
            - path (str): Path relative to the API base, e.g. '/webhooks'
            - query (Optional[Dict]): Query parameters; lists are comma-joined
            - body (Optional[Any]): JSON body for write methods
            - confirm (bool): Required for sends and destructive endpoints
            - fields (Optional[List[str]]): Keys to keep on each record
            - response_format (ResponseFormat): 'json' or 'markdown'
            - max_chars (Optional[int]): Truncation limit override

    Returns:
        str: On success, JSON of the shape
            {"status": int, "url": str, "data": <parsed response body>}
            On failure, "Error <status> on <url>" plus a hint and the API's own
            error object (error_code, message, per-field errors).

    Examples:
        - "List the webhooks" -> method=GET, path='/webhooks'
        - "Upload a file by URL" -> method=POST, path='/files', body={'url': '...'}
        - "React to a message" -> method=PUT, path='/messages/<uid>/reactions',
          body={...}
        - Don't use when: sending a message (use timelines_send_message, which
          validates the phone format and enforces the send gate)

    Error Handling:
        - Any POST to a /messages path goes through the send gate: it needs
          TIMELINES_ALLOW_SEND and confirm=true, exactly like the typed tool
        - Writes are refused entirely when TIMELINES_READ_ONLY is on
        - A 200 carrying {"status":"error"} is surfaced as an error, not success
    """
    return await _run(
        params.method.value,
        params.path,
        query=params.query,
        body=params.body,
        fields=params.fields,
        response_format=params.response_format,
        max_chars=params.max_chars,
        confirmed=params.confirm,
    )


@mcp.tool(
    name="timelines_discover",
    annotations={
        "title": "Discover TimelinesAI Endpoints",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def timelines_discover(params: DiscoverInput) -> str:
    """Probe TimelinesAI paths with GET and report which exist and are permitted.

    The published reference is thin on pagination and says little about which
    endpoints a given plan includes, so this reports the status per path: 200
    'works', 403 'exists but your plan or token cannot', 404 'no such route'.

    Args:
        params (DiscoverInput): Validated parameters containing:
            - paths (Optional[List[str]]): Paths to probe; defaults to the documented set
            - include_sample (bool): Include a 400-character sample of each 200 response

    Returns:
        str: JSON of the shape
            {"api_base": str, "probed": int,
             "reachable": [{"path": str, "status": int, "sample": str}],
             "forbidden": [{"path": str, "status": int}],
             "missing": [str], "errors": [{"path": str, "error": str}]}

    Examples:
        - Use when: "What can this token actually reach?" -> run with defaults
        - Use when: confirming the pagination shape -> include_sample=True and read
          whether the payload carries has_more_pages
        - Don't use when: you already know the path (use timelines_request)

    Error Handling:
        - Everything 401 means the token is wrong; check timelines_whoami first
    """
    paths = params.paths or list(KNOWN_ENDPOINTS)
    reachable: List[Dict[str, Any]] = []
    forbidden: List[Dict[str, Any]] = []
    missing: List[str] = []
    errors: List[Dict[str, str]] = []

    try:
        headers = _auth_headers()
    except AuthConfigError as exc:
        return f"Error: {exc}"

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, follow_redirects=True) as client:
            for path in paths:
                url = _normalize_path(path)
                try:
                    response = await client.get(url, headers=headers)
                except httpx.RequestError as exc:
                    errors.append({"path": path, "error": f"{type(exc).__name__}: {exc}"})
                    continue
                status = response.status_code
                if 200 <= status < 300:
                    entry: Dict[str, Any] = {"path": path, "status": status}
                    if params.include_sample:
                        entry["sample"] = response.text[:400]
                    reachable.append(entry)
                elif status in (401, 403):
                    forbidden.append({"path": path, "status": status})
                elif status == 404:
                    missing.append(path)
                else:
                    errors.append({"path": path, "error": f"HTTP {status}: {response.text[:200]}"})
    except Exception as exc:  # noqa: BLE001 - surfaced to the agent as text
        return _format_error(exc, API_BASE_URL)

    result = {
        "api_base": API_BASE_URL,
        "probed": len(paths),
        "reachable": reachable,
        "forbidden": forbidden,
        "missing": missing,
        "errors": errors,
    }
    return _truncate(json.dumps(result, indent=2, ensure_ascii=False), MAX_CHARS)


@mcp.tool(
    name="timelines_list_chats",
    annotations={
        "title": "List TimelinesAI Chats",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def timelines_list_chats(params: ChatsInput) -> str:
    """List WhatsApp conversations with their owner, labels and unread state.

    Wraps GET /chats, the main read endpoint. Read-only. Filters combine, so
    "unread chats assigned to Ana that are labelled vip" is one call.

    Args:
        params (ChatsInput): Validated parameters containing:
            - label (Optional[List[str]]): Any of these labels, comma-joined for the API
            - responsible (Optional[str]): Teammate email the chat is assigned to
            - read (Optional[bool]): False returns only chats with unread messages
            - group (Optional[bool]): True only groups, False only 1:1
            - name (Optional[str]): Substring match on the chat or contact name
            - created_after (Optional[str]): ISO 8601 date lower bound
            - page (Optional[int]): 1-based page. Page size is fixed at 50 by the API
            - extra_query (Optional[Dict]): Any further filters
            - fields (Optional[List[str]]): Keys to keep on each chat
            - response_format (ResponseFormat): 'json' or 'markdown'

    Returns:
        str: JSON of the shape {"status": int, "url": str, "data": {...}}. Verified
            chat fields: id, name, phone, jid, photo, is_group, whatsapp_account_id
            (a JID string, not a number), closed, read, unattended, labels,
            responsible_email, responsible_name, chat_url, created_timestamp,
            last_message_timestamp, last_message_uid, is_allowed_to_message.
            Note the assignee is responsible_email on the RECORD, while the
            query FILTER is called responsible. When another page exists the
            payload says has_more_pages and a "next_page_hint" is added.

    Examples:
        - Use when: "What's unread?" -> read=False
        - Use when: "Ana's VIP conversations" -> responsible='ana@empresa.com',
          label=['vip']
        - Use when: "Find the chat with Acme" -> name='acme'
        - Don't use when: you want per-chat totals across the whole inbox
          (use timelines_activity_summary)

    Error Handling:
        - An empty result with filters set usually means the label or email does
          not match exactly; list without filters first to see the real values
        - Big workspaces truncate; page size cannot be lowered, so pass `fields`
          to keep only the keys you need
    """
    return await _run(
        "GET",
        "/chats",
        query=params.to_query(),
        fields=params.fields,
        response_format=params.response_format,
    )


@mcp.tool(
    name="timelines_get_chat",
    annotations={
        "title": "Get One TimelinesAI Chat",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def timelines_get_chat(params: ChatIdInput) -> str:
    """Retrieve a single chat by id, with its full metadata.

    Wraps GET /chats/{id}. Read-only. Use it after timelines_list_chats has
    given you an id, or when the user pastes one.

    Args:
        params (ChatIdInput): Validated parameters containing:
            - chat_id (int | str): The chat id
            - fields (Optional[List[str]]): Keys to keep on the returned object
            - response_format (ResponseFormat): 'json' or 'markdown'

    Returns:
        str: JSON of the shape {"status": int, "url": str, "data": <chat object>}.

    Examples:
        - Use when: "Who is handling chat 4821?" -> chat_id=4821
        - Don't use when: you have a phone number instead of an id
          (use timelines_list_chats with name= or the phone as the filter)

    Error Handling:
        - 404 means the chat does not exist in this workspace
    """
    return await _run(
        "GET",
        f"/chats/{params.chat_id}",
        fields=params.fields,
        response_format=params.response_format,
    )


@mcp.tool(
    name="timelines_list_messages",
    annotations={
        "title": "Read a TimelinesAI Chat's Messages",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def timelines_list_messages(params: MessagesInput) -> str:
    """Read the message history of one chat.

    Wraps GET /chats/{id}/messages. Read-only — same path as sending, but GET,
    so nothing leaves the building.

    Args:
        params (MessagesInput): Validated parameters containing:
            - chat_id (int | str): The chat to read
            - page (Optional[int]): 1-based page. Page size is fixed at 50 by the API
            - extra_query (Optional[Dict]): Any further filters the API accepts
            - fields (Optional[List[str]]): Keys to keep on each message
            - response_format (ResponseFormat): 'json' or 'markdown'

    Returns:
        str: JSON of the shape {"status": int, "url": str, "data": {...}} holding
            the messages plus has_more_pages.

    Examples:
        - Use when: "What did the customer say in chat 4821?" -> chat_id=4821
        - Use when: summarizing a conversation -> read a page, then decide whether
          the older pages matter before pulling them
        - Don't use when: replying (use timelines_send_message)

    Error Handling:
        - Long histories truncate; page through with `page` and pass `fields`
          rather than raising the character limit, so the useful part is not buried
    """
    query = params.to_query()
    return await _run(
        "GET",
        f"/chats/{params.chat_id}/messages",
        query=query,
        fields=params.fields,
        response_format=params.response_format,
    )


@mcp.tool(
    name="timelines_send_message",
    annotations={
        "title": "Send a WhatsApp Message (irreversible)",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def timelines_send_message(params: SendMessageInput) -> str:
    """Send a WhatsApp message to a phone number or into an existing chat.

    This reaches a real person's phone within seconds and cannot be undone. Before
    calling it, show the user the exact recipient and the exact text and get their
    agreement — then set confirm=true. Never infer consent from an earlier, more
    general instruction.

    Wraps POST /messages when addressing by phone, or POST /chats/{id}/messages
    when replying inside an existing conversation. Give exactly one of `phone`
    or `chat_id`.

    Args:
        params (SendMessageInput): Validated parameters containing:
            - phone (Optional[str]): Recipient in international format, '+5215512345678'
            - chat_id (Optional[int|str]): Existing chat to reply into instead
            - text (Optional[str]): Message body, max 2000 characters
            - whatsapp_account_phone (Optional[str]): Which connected number sends it.
              Omitted, TimelinesAI picks the most recently connected account — which
              may not be the one the user means, so prefer to be explicit
            - file_uid (Optional[str]): Attachment uid from the files endpoints
            - label (Optional[str]): Label to apply to the chat, max 64 chars
            - chat_name (Optional[str]): Exact group name, to target a named group
            - confirm (bool): REQUIRED, see above
            - extra_body (Optional[Dict]): Further documented fields

    Returns:
        str: JSON of the shape {"status": int, "url": str, "data": {"status": "ok",
            "data": {"message_uid": str}}}. The message_uid identifies the message
            for later reaction or status lookups.

    Examples:
        - Use when: the user said "reply to Ana that we ship Monday", you showed
          them the draft, they approved -> phone='+52...', text='...', confirm=True
        - Use when: replying in a known thread -> chat_id=4821, text='...', confirm=True
        - Don't use when: the user has not seen the exact text yet — draft it in
          the conversation first and ask
        - Don't use when: sending to many people; that is a campaign, and each
          recipient deserves its own explicit approval

    Error Handling:
        - Refused when TIMELINES_READ_ONLY is on, when TIMELINES_ALLOW_SEND is not
          set, or when confirm is not true. Each refusal names the gate that stopped it
        - 400 with a per-field error usually means the phone is not in international
          format, or text exceeds 2000 characters
        - On a timeout, check the chat before retrying so it does not go out twice
    """
    if bool(params.phone) == bool(params.chat_id):
        return (
            "Error: give exactly one of `phone` or `chat_id`. `phone` starts a message to "
            "a number; `chat_id` replies inside an existing conversation."
        )
    if not params.text and not params.file_uid:
        return "Error: nothing to send. Provide `text`, or a `file_uid` for an attachment."

    body: Dict[str, Any] = {}
    for key in ("text", "whatsapp_account_phone", "file_uid", "label", "chat_name"):
        value = getattr(params, key)
        if value is not None:
            body[key] = value
    if params.extra_body:
        body.update(params.extra_body)

    if params.phone:
        body["phone"] = params.phone
        path = "/messages"
    else:
        path = f"/chats/{params.chat_id}/messages"

    return await _run("POST", path, body=body, confirmed=params.confirm)


@mcp.tool(
    name="timelines_update_chat",
    annotations={
        "title": "Update a TimelinesAI Chat",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def timelines_update_chat(params: UpdateChatInput) -> str:
    """Reassign a chat, close or reopen it, or change its read state.

    Wraps PATCH /chats/{id}. These changes are internal to the workspace and
    reversible — nothing leaves for the customer — so they do not need the send
    gate, only that writes are enabled at all.

    Args:
        params (UpdateChatInput): Validated parameters containing:
            - chat_id (int | str): The chat to update
            - responsible (Optional[str]): Teammate email to assign it to
            - closed (Optional[bool]): True closes, False reopens
            - read (Optional[bool]): Mark read or unread
            - extra_body (Optional[Dict]): Any further documented update fields

    Returns:
        str: JSON of the shape {"status": int, "url": str, "data": <updated chat>}.

    Examples:
        - Use when: "Assign chat 4821 to Ana" -> chat_id=4821,
          responsible='ana@empresa.com'
        - Use when: "Close everything already answered" -> after listing them,
          one call per chat
        - Don't use when: adding labels (use timelines_manage_labels)

    Error Handling:
        - Refused when TIMELINES_READ_ONLY is on
        - 400 on `responsible` usually means that email is not a teammate in this
          workspace; check timelines_list_teammates
    """
    body: Dict[str, Any] = {}
    for key in ("responsible", "closed", "read"):
        value = getattr(params, key)
        if value is not None:
            body[key] = value
    if params.extra_body:
        body.update(params.extra_body)
    if not body:
        return "Error: nothing to update. Set responsible, closed, read or extra_body."

    return await _run("PATCH", f"/chats/{params.chat_id}", body=body, confirmed=True)


@mcp.tool(
    name="timelines_manage_labels",
    annotations={
        "title": "List, Add or Replace Chat Labels",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def timelines_manage_labels(params: LabelsInput) -> str:
    """Read a chat's labels, append new ones, or overwrite the whole set.

    Wraps GET/POST/PUT /chats/{id}/labels. Note the difference between the two
    write modes: 'add' appends and leaves existing labels alone, while 'replace'
    overwrites — any label not in your list is removed. When in doubt, 'add'.

    Args:
        params (LabelsInput): Validated parameters containing:
            - chat_id (int | str): The chat whose labels to act on
            - action (str): 'list' (default), 'add' or 'replace'
            - labels (Optional[List[str]]): Label names; required for add and replace
            - response_format (ResponseFormat): 'json' or 'markdown'

    Returns:
        str: JSON of the shape {"status": int, "url": str, "data": <labels payload>}.

    Examples:
        - Use when: "What is chat 4821 tagged as?" -> action='list'
        - Use when: "Tag it as vip" -> action='add', labels=['vip']
        - Use when: "Its only tag should be closed-won" -> action='replace',
          labels=['closed-won']
        - Don't use when: filtering by label (that is timelines_list_chats)

    Error Handling:
        - 'add' and 'replace' without `labels` return an error before any request
        - Writes are refused when TIMELINES_READ_ONLY is on
    """
    if params.action == "list":
        return await _run(
            "GET",
            f"/chats/{params.chat_id}/labels",
            response_format=params.response_format,
        )

    if not params.labels:
        return f"Error: action='{params.action}' needs a non-empty `labels` list."

    method = "POST" if params.action == "add" else "PUT"
    return await _run(
        method,
        f"/chats/{params.chat_id}/labels",
        body={"labels": params.labels},
        response_format=params.response_format,
        confirmed=True,
    )


@mcp.tool(
    name="timelines_list_whatsapp_accounts",
    annotations={
        "title": "List Connected WhatsApp Numbers",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def timelines_list_whatsapp_accounts(params: ListInput) -> str:
    """List the WhatsApp numbers connected to the workspace.

    Wraps GET /whatsapp_accounts. Read-only. Worth calling before any send: if
    the workspace has more than one number, leaving whatsapp_account_phone unset
    lets TimelinesAI pick the most recently connected one, which is rarely what
    the user intends.

    Args:
        params (ListInput): page, extra_query, fields, response_format.

    Returns:
        str: JSON of the shape {"status": int, "url": str, "data": <accounts payload>}.

    Examples:
        - Use when: "Which numbers do we have connected?"
        - Use when: preparing a send, to pick the right sending number explicitly
        - Don't use when: you need the conversations (use timelines_list_chats)

    Error Handling:
        - An empty list means no WhatsApp number is linked yet; sends will fail
          until one is connected in the dashboard
    """
    return await _run(
        "GET",
        "/whatsapp_accounts",
        query=params.to_query(),
        fields=params.fields,
        response_format=params.response_format,
    )


@mcp.tool(
    name="timelines_list_teammates",
    annotations={
        "title": "List Workspace Teammates",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def timelines_list_teammates(params: ListInput) -> str:
    """List the people in the workspace and their emails.

    Wraps GET /workspace/teammates. Read-only. The emails here are exactly the
    values the `responsible` filter and the assignment field expect, so this is
    the lookup that makes those work.

    Args:
        params (ListInput): page, extra_query, fields, response_format.

    Returns:
        str: JSON of the shape {"status": int, "url": str, "data": <teammates payload>}.

    Examples:
        - Use when: "Who's on the team?"
        - Use when: a `responsible` filter returned nothing and you need the exact
          spelling of someone's email
        - Don't use when: inviting someone (that is timelines_request with
          POST /workspace/invitations and confirm=true)

    Error Handling:
        - 403 means the token's role cannot see workspace membership
    """
    return await _run(
        "GET",
        "/workspace/teammates",
        query=params.to_query(),
        fields=params.fields,
        response_format=params.response_format,
    )


@mcp.tool(
    name="timelines_activity_summary",
    annotations={
        "title": "Summarize the TimelinesAI Inbox",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def timelines_activity_summary(params: ActivitySummaryInput) -> str:
    """Count the whole inbox: unread, unattended, open, by owner, label and number.

    TimelinesAI has no aggregation endpoint, so this paginates /chats and counts
    here — the tally you would otherwise ask an agent to do by hand across
    hundreds of records, done exactly and without spending context on the rows.

    Note on assignment: the chat record carries the owner in `responsible_email`,
    while the query filter for the same thing is called `responsible`. Filtering by
    a teammate's email is the reliable way to count one person's chats without
    scanning the whole inbox.

    Args:
        params (ActivitySummaryInput): Validated parameters containing:
            - max_pages (int): Pagination cap; the API fixes 50 chats per page, so the
              default of 10 covers 500 chats. Each page is paced 1.2s apart to stay
              within the rate limit, so a 100-page scan takes about two minutes
            - filters (Optional[Dict]): Filters passed straight to /chats

    Returns:
        str: JSON of the shape
            {
              "chats_counted": int,
              "complete": bool,        # false when the scan did not reach the end
              "stopped_early": str,    # present only when the API cut the scan short
              "totals": {"unread": int, "unattended": int, "open": int,
                         "closed": int, "groups": int, "direct": int,
                         "unassigned": int},
              "by_responsible": {"ana@empresa.com": {"total": int, "unread": int}},
              "by_label": {"vip": int},
              "by_whatsapp_account": {"<id>": int}
            }

    Examples:
        - Use when: "How's the inbox looking?" -> run with defaults
        - Use when: "What still needs a reply?" -> read totals.unattended
        - Use when: "Who has the most unread?" -> read by_responsible
        - Use when: "How many VIP chats are open?" -> filters={'label': 'vip'}
        - Don't use when: you need the chats themselves (use timelines_list_chats)

    Error Handling:
        - TimelinesAI allows 50 requests/minute per workspace, and reads count.
          This tool paces itself at that rate and retries once honouring the
          Retry-After header; if it is cut short anyway it returns what it counted
          plus a "stopped_early" note rather than discarding the work. A full scan
          is therefore slow by design: roughly 1.2 seconds per 50 chats
        - complete=false means the inbox held more than max_pages*50 chats and the
          counts cover only what was scanned. This is worse than merely incomplete:
          the pages come back in the API's order, not shuffled, and the early ones
          skew toward unassigned and unlabelled chats. A partial run can therefore
          say "100 unassigned, 0 per person" about an inbox where hundreds of chats
          do have owners. Raise max_pages until complete is true, or filter, before
          reporting any ratio
    """
    totals = {
        "unread": 0,
        "unattended": 0,
        "open": 0,
        "closed": 0,
        "groups": 0,
        "direct": 0,
        "unassigned": 0,
    }
    by_responsible: Dict[str, Dict[str, int]] = {}
    by_label: Dict[str, int] = {}
    by_account: Dict[str, int] = {}
    counted = 0
    more_remaining = False
    url = _normalize_path("/chats")

    stopped_early: Optional[str] = None

    for page in range(1, params.max_pages + 1):
        query: Dict[str, QueryValue] = {"page": page}
        if params.filters:
            query.update(params.filters)

        # Pacing and 429 retries live in _api_request now, so every tool gets
        # them, not just this one.
        try:
            _, data, url = await _api_request("GET", "/chats", query=query)
        except Exception as exc:  # noqa: BLE001 - handled below
            # Whatever was counted so far is still worth returning. Discarding 19
            # pages of work because the 20th was rate-limited leaves the caller
            # with nothing, which is strictly worse than partial counts that are
            # labelled as partial.
            if counted == 0:
                return _format_error(exc, url)
            stopped_early = _format_error(exc, url).split("\n")[0]
            more_remaining = True
            break

        records = _extract_records(data) or []
        for record in records:
            if not isinstance(record, dict):
                continue
            counted += 1

            if record.get("read") is False:
                totals["unread"] += 1
            if record.get("closed"):
                totals["closed"] += 1
            else:
                totals["open"] += 1
            if record.get("is_group"):
                totals["groups"] += 1
            else:
                totals["direct"] += 1

            if record.get("unattended"):
                totals["unattended"] += 1

            # The chat record names the assignee "responsible_email" /
            # "responsible_name" — there is no plain "responsible" key, which is
            # only the name of the query FILTER. Reading the filter's name here
            # silently counted every chat as unassigned.
            owner = record.get("responsible_email") or record.get("responsible_name")
            if owner is None:
                legacy = record.get("responsible")
                owner = legacy.get("email") or legacy.get("name") if isinstance(legacy, dict) else legacy
            if owner:
                bucket = by_responsible.setdefault(str(owner), {"total": 0, "unread": 0})
                bucket["total"] += 1
                if record.get("read") is False:
                    bucket["unread"] += 1
            else:
                totals["unassigned"] += 1

            for label in record.get("labels") or []:
                name = label.get("name") if isinstance(label, dict) else label
                if name:
                    by_label[str(name)] = by_label.get(str(name), 0) + 1

            account = record.get("whatsapp_account_id")
            if account is not None:
                by_account[str(account)] = by_account.get(str(account), 0) + 1

        if not _has_more_pages(data):
            more_remaining = False
            break
        # The API says another page exists, so stopping now leaves counts partial.
        more_remaining = True
        if not records:
            break

    result: Dict[str, Any] = {
        "chats_counted": counted,
        "complete": not more_remaining,
        "totals": totals,
        "by_responsible": dict(
            sorted(by_responsible.items(), key=lambda kv: kv[1]["total"], reverse=True)
        ),
        "by_label": dict(sorted(by_label.items(), key=lambda kv: kv[1], reverse=True)),
        "by_whatsapp_account": by_account,
    }
    if stopped_early:
        result["stopped_early"] = stopped_early
    if more_remaining:
        how = f"before stopping early ({stopped_early})" if stopped_early \
            else f"across {params.max_pages} pages"
        tail = (
            " The scan was cut short by the API, not by max_pages, so raising "
            "max_pages alone will not help: wait a few seconds and use filters."
            if stopped_early else
            " Raise max_pages until complete is true, or narrow with filters so the "
            "scan finishes."
        )
        result["warning"] = (
            f"Scanned {counted} chats {how} and more remain. "
            "These counts are PARTIAL AND NOT A REPRESENTATIVE SAMPLE: the API returns "
            "pages in its own order, and the early pages of this workspace skew heavily "
            "toward unassigned, unlabelled chats. Reporting these ratios as if they "
            "described the whole inbox would be wrong." + tail
        )
    return _truncate(json.dumps(result, indent=2, ensure_ascii=False, default=str), MAX_CHARS)


def _build_http_app():
    """Build the Streamable-HTTP ASGI app, gated behind a shared bearer secret.

    The MCP protocol itself carries no authentication, so anything reachable on
    a public URL must be protected at the transport layer. Every request except
    the health check must present ``Authorization: Bearer <MCP_AUTH_TOKEN>`` or
    the equivalent path-embedded secret.
    """
    import secrets

    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse, PlainTextResponse

    expected = os.environ.get("MCP_AUTH_TOKEN", "").strip()
    if not expected:
        raise RuntimeError(
            "MCP_AUTH_TOKEN is required when TIMELINES_MCP_TRANSPORT=http. Without it "
            "the server would be an unauthenticated public gateway to your WhatsApp "
            "inbox. Generate one with: "
            "python3 -c \"import secrets;print(secrets.token_urlsafe(48))\""
        )
    if len(expected) < 32:
        raise RuntimeError(
            f"MCP_AUTH_TOKEN is only {len(expected)} characters. Use at least 32 "
            "(48+ recommended) so it cannot be guessed or brute-forced."
        )

    app = mcp.streamable_http_app(stateless_http=True, json_response=True, host="0.0.0.0")

    class BearerAuthMiddleware(BaseHTTPMiddleware):
        """Gate every request on the shared secret.

        Two ways to present it:

        1. ``/s/<secret>/mcp`` - the secret embedded in the URL path. Required by
           Claude's custom connectors, which cannot attach a static header.
        2. ``Authorization: Bearer <secret>`` - for curl, scripts and the
           MCP Inspector.

        THIS MIDDLEWARE NEVER RETURNS 401, BY DESIGN.

        An MCP client treats 401 (or a WWW-Authenticate header) as "this server
        speaks OAuth", and responds by fetching /.well-known/oauth-* and calling
        POST /register. This server implements none of that, so the handshake
        fails with "Couldn't register with the sign-in service" and the connector
        never comes up.

        Returning 404 instead means unauthenticated callers see a server with no
        auth scheme and nothing at that path, so no OAuth flow is ever started.

        NOTE: a connector URL that is merely MISTYPED produces the same 404 and
        therefore the same misleading OAuth error. If a connector will not
        register, check that the URL ends in exactly "/mcp" before suspecting
        anything else.
        """

        async def dispatch(self, request, call_next):
            path = request.url.path

            if path in ("/healthz", "/"):
                return PlainTextResponse("ok")

            # Path-embedded secret: /s/<secret>/mcp -> forwarded as /mcp
            if path.startswith("/s/"):
                presented, _, tail = path[3:].partition("/")
                if secrets.compare_digest(presented, expected):
                    new_path = "/" + tail
                    request.scope["path"] = new_path
                    request.scope["raw_path"] = new_path.encode()
                    return await call_next(request)
                return JSONResponse({"detail": "Not Found"}, status_code=404)

            header = request.headers.get("authorization", "")
            scheme, _, presented = header.partition(" ")
            if scheme.lower() == "bearer" and presented:
                if secrets.compare_digest(presented.strip(), expected):
                    return await call_next(request)

            # Everything else, including /.well-known/* and /register.
            return JSONResponse({"detail": "Not Found"}, status_code=404)

    app.add_middleware(BearerAuthMiddleware)
    return app


def _startup_banner() -> str:
    """One line describing exactly which gates are open, for the logs.

    Printed on every boot so the deployment's posture is visible in Railway's log
    tail without opening a shell — in particular whether it can send WhatsApp
    messages to real people.
    """
    try:
        _get_token()
        token_state = "set"
    except AuthConfigError:
        token_state = "NO TOKEN SET"
    sending = (not READ_ONLY) and ALLOW_SEND
    return (
        f"token={token_state}  read_only={READ_ONLY}  allow_send={ALLOW_SEND}  "
        f"sending_enabled={sending}"
    )


if __name__ == "__main__":
    if "--help" in sys.argv:
        print(__doc__)
        sys.exit(0)

    banner = _startup_banner()

    if IS_REMOTE:
        import uvicorn

        port = int(os.environ.get("PORT", "8000"))
        print(
            f"[timelines-mcp] streamable-http on 0.0.0.0:{port}  {banner}  "
            f"api_base={API_BASE_URL}",
            file=sys.stderr,
        )
        if "sending_enabled=True" in banner:
            print(
                "[timelines-mcp] WARNING: this deployment can send WhatsApp messages "
                "to real people.",
                file=sys.stderr,
            )
        uvicorn.run(_build_http_app(), host="0.0.0.0", port=port, log_level="info")
    else:
        print(f"[timelines-mcp] stdio  {banner}", file=sys.stderr)
        mcp.run()
