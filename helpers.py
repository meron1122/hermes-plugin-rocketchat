"""Plugin-level helpers: requirement checks, config validation, env
enablement, shared constants, and the standalone cron sender (REST-only,
no live adapter needed)."""

from __future__ import annotations

import logging
import json
import inspect
import os
import re
import unicodedata
from typing import Any, Dict, List, Optional
from urllib.parse import unquote, urlsplit, urlunsplit

logger = logging.getLogger(__name__)

# Rocket.Chat's default Message_MaxAllowedSize is 5000; admins can raise it
# but the safe default for multi-line messages is 5000.
MAX_MESSAGE_LENGTH = 5000

# Inbound attachments and outbound URL-backed media are buffered before Hermes
# caches or uploads them.  Keep a hard process-local ceiling even if an operator
# misconfigures the deployment value; unlike the agent-upload guard, zero never
# means unlimited for network-controlled response bodies.
DEFAULT_MEDIA_DOWNLOAD_MAX_BYTES = 100 * 1024 * 1024
HARD_MEDIA_DOWNLOAD_MAX_BYTES = 1024 * 1024 * 1024

# Every authenticated Rocket.Chat JSON response is network-controlled.  Bound
# it before decoding so a malicious/compromised server (or an unexpectedly huge
# thread) cannot make the gateway buffer and parse an unbounded body.
DEFAULT_API_RESPONSE_MAX_BYTES = 2 * 1024 * 1024
MIN_API_RESPONSE_MAX_BYTES = 64 * 1024
HARD_API_RESPONSE_MAX_BYTES = 16 * 1024 * 1024

# Room type codes returned by the Rocket.Chat API.
#   d = direct message (1:1)
#   c = public channel
#   p = private group (private channel)
#   l = livechat / omnichannel
_ROOM_TYPE_MAP = {
    "d": "dm",
    "c": "channel",
    "p": "group",
    "l": "group",
}

# Reconnect parameters (exponential backoff).
_RECONNECT_BASE_DELAY = 2.0
_RECONNECT_MAX_DELAY = 60.0
_RECONNECT_JITTER = 0.2

# DDP protocol version. Rocket.Chat supports "1" across 7.x/8.x.
_DDP_PROTOCOL_VERSION = "1"

_TRUE_VALUES = {"1", "true", "yes", "on"}
_PERCENT_ESCAPE_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")


def _env_flag(name: str, *, default: bool = False) -> bool:
    """Read a conventional boolean environment flag."""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUE_VALUES


def is_valid_server_identifier(value: Any) -> bool:
    """Return whether *value* is a compact, unambiguous Rocket.Chat id/name."""
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and len(value) <= 255
        and not any(
            unicodedata.category(char) in {"Cc", "Cf", "Cs"}
            for char in value
        )
    )


def is_valid_url_path_identifier(value: Any) -> bool:
    """Return whether an opaque id is safe as one URL path component."""
    return bool(
        is_valid_server_identifier(value)
        and value not in {".", ".."}
        and "/" not in value
        and "\\" not in value
    )


def media_download_max_bytes() -> int:
    """Return the bounded network-media response limit.

    Values below one byte or otherwise invalid retain the secure default.  A
    deployment may lower the limit freely, while the hard 1 GiB ceiling cannot
    be disabled through configuration.
    """
    raw = os.getenv(
        "ROCKETCHAT_MEDIA_DOWNLOAD_MAX_BYTES",
        str(DEFAULT_MEDIA_DOWNLOAD_MAX_BYTES),
    )
    try:
        value = int(raw.strip())
    except (AttributeError, TypeError, ValueError):
        return DEFAULT_MEDIA_DOWNLOAD_MAX_BYTES
    if value < 1:
        return DEFAULT_MEDIA_DOWNLOAD_MAX_BYTES
    return min(value, HARD_MEDIA_DOWNLOAD_MAX_BYTES)


class MediaDownloadTooLarge(ValueError):
    """Raised before a network-controlled body can exceed its memory budget."""


class ApiResponseTooLarge(ValueError):
    """Raised before a Rocket.Chat JSON body can exceed its memory budget."""


def api_response_max_bytes() -> int:
    """Return the process-wide limit for Rocket.Chat JSON REST responses."""
    raw = os.getenv(
        "ROCKETCHAT_AGENT_RESPONSE_MAX_BYTES",
        str(DEFAULT_API_RESPONSE_MAX_BYTES),
    )
    try:
        value = int(raw.strip())
    except (AttributeError, TypeError, ValueError):
        return DEFAULT_API_RESPONSE_MAX_BYTES
    return min(
        HARD_API_RESPONSE_MAX_BYTES,
        max(MIN_API_RESPONSE_MAX_BYTES, value),
    )


async def read_bounded_json_response(response: Any) -> Dict[str, Any]:
    """Read and decode one bounded Rocket.Chat JSON object response.

    The byte count applies to the actual (possibly decompressed) chunks yielded
    by aiohttp, rather than trusting only ``Content-Length``.  The final JSON
    value must be an object because every plugin call site expects a mapping.
    """
    maximum = api_response_max_bytes()
    content_length = getattr(response, "content_length", None)
    if (
        isinstance(content_length, int)
        and not isinstance(content_length, bool)
        and content_length > maximum
    ):
        raise ApiResponseTooLarge(
            "Rocket.Chat API response exceeded the configured limit"
        )

    content = getattr(response, "content", None)
    body: Optional[bytes] = None
    if content is not None and hasattr(content, "iter_chunked"):
        chunks = bytearray()
        async for chunk in content.iter_chunked(64 * 1024):
            if not isinstance(chunk, (bytes, bytearray, memoryview)):
                raise ValueError("Rocket.Chat API response is invalid")
            chunks.extend(chunk)
            if len(chunks) > maximum:
                raise ApiResponseTooLarge(
                    "Rocket.Chat API response exceeded the configured limit"
                )
        body = bytes(chunks)
    else:
        read = getattr(response, "read", None)
        if callable(read) and inspect.iscoroutinefunction(read):
            raw_body = await read()
            if not isinstance(raw_body, (bytes, bytearray, memoryview)):
                raise ValueError("Rocket.Chat API response is invalid")
            if len(raw_body) > maximum:
                raise ApiResponseTooLarge(
                    "Rocket.Chat API response exceeded the configured limit"
                )
            body = bytes(raw_body)

    if body is not None:
        data = json.loads(body.decode("utf-8")) if body else {}
    else:
        # Compatibility for minimal unit-test doubles.  Real aiohttp responses
        # always expose ``content``/``read`` and therefore take a bounded path.
        data = await response.json(content_type=None)
    if not isinstance(data, dict):
        raise ValueError("Rocket.Chat API response is invalid")
    return data


async def read_bounded_response_bytes(
    response: Any, *, maximum: Optional[int] = None
) -> bytes:
    """Stream one HTTP response into memory without exceeding the media limit."""
    configured_maximum = media_download_max_bytes()
    if maximum is None:
        maximum = configured_maximum
    elif not isinstance(maximum, int) or isinstance(maximum, bool) or maximum < 1:
        raise MediaDownloadTooLarge("Rocket.Chat media response is too large")
    else:
        maximum = min(maximum, configured_maximum)
    content_length = getattr(response, "content_length", None)
    if (
        isinstance(content_length, int)
        and not isinstance(content_length, bool)
        and content_length > maximum
    ):
        raise MediaDownloadTooLarge("Rocket.Chat media response is too large")

    content = getattr(response, "content", None)
    if content is not None and hasattr(content, "iter_chunked"):
        body = bytearray()
        async for chunk in content.iter_chunked(64 * 1024):
            if not isinstance(chunk, (bytes, bytearray, memoryview)):
                raise ValueError("Rocket.Chat media response is invalid")
            body.extend(chunk)
            if len(body) > maximum:
                raise MediaDownloadTooLarge(
                    "Rocket.Chat media response is too large"
                )
        return bytes(body)

    body = await response.read()
    if not isinstance(body, (bytes, bytearray, memoryview)):
        raise ValueError("Rocket.Chat media response is invalid")
    if len(body) > maximum:
        raise MediaDownloadTooLarge("Rocket.Chat media response is too large")
    return bytes(body)


def _has_forbidden_url_chars(value: str, *, decoded: bool = False) -> bool:
    """Reject characters that can hide or split an authenticated URL."""
    for char in value:
        category = unicodedata.category(char)
        if category in {"Cc", "Cf", "Cs"}:
            return True
        if not decoded and char.isspace():
            return True
    return False


def validate_server_url(raw_url: Any) -> str:
    """Return a normalized Rocket.Chat HTTP base URL or raise ``ValueError``.

    The URL is an authentication boundary because PAT-bearing requests and the
    DDP resume token are sent to this origin.  HTTPS is mandatory unless the
    operator explicitly opts in to HTTP for a trusted local deployment via
    ``ROCKETCHAT_ALLOW_INSECURE_HTTP=true``.
    """
    if not isinstance(raw_url, str) or not raw_url:
        raise ValueError("Rocket.Chat server URL is invalid")
    if (
        _has_forbidden_url_chars(raw_url)
        or "\\" in raw_url
        or _PERCENT_ESCAPE_RE.search(raw_url)
    ):
        raise ValueError("Rocket.Chat server URL is invalid")
    decoded = unquote(raw_url)
    if _has_forbidden_url_chars(decoded, decoded=True):
        raise ValueError("Rocket.Chat server URL is invalid")

    try:
        parsed = urlsplit(raw_url)
        scheme = parsed.scheme.lower()
        if scheme not in {"https", "http"}:
            raise ValueError("Rocket.Chat server URL is invalid")
        if scheme == "http" and not _env_flag(
            "ROCKETCHAT_ALLOW_INSECURE_HTTP"
        ):
            raise ValueError("Rocket.Chat server URL must use HTTPS")
        if (
            not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or "@" in parsed.netloc
            or parsed.query
            or parsed.fragment
            or parsed.netloc.endswith(":")
            or "%" in parsed.hostname
        ):
            raise ValueError("Rocket.Chat server URL is invalid")
        parsed.port  # Validate malformed and out-of-range ports eagerly.
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith("Rocket.Chat"):
            raise
        raise ValueError("Rocket.Chat server URL is invalid") from None

    path = parsed.path.rstrip("/")
    return urlunsplit((scheme, parsed.netloc, path, "", ""))


def _normalize_auth_value(raw_value: Any, *, maximum: int) -> str:
    """Normalize a header credential without ever including it in errors."""
    if not isinstance(raw_value, str):
        raise ValueError("Rocket.Chat credentials are invalid")
    value = raw_value.strip()
    if (
        not value
        or len(value) > maximum
        or any(
            char.isspace()
            or unicodedata.category(char) in {"Cc", "Cf", "Cs"}
            for char in value
        )
    ):
        raise ValueError("Rocket.Chat credentials are invalid")
    return value


def validate_auth_config(
    raw_url: Any, raw_token: Any, raw_user_id: Any
) -> tuple[str, str, str]:
    """Validate and normalize one PAT-authenticated Rocket.Chat config."""
    return (
        validate_server_url(raw_url),
        _normalize_auth_value(raw_token, maximum=4096),
        _normalize_auth_value(raw_user_id, maximum=255),
    )


def websocket_url(raw_base_url: Any) -> str:
    """Build the DDP endpoint from a validated HTTP(S) server URL."""
    base_url = validate_server_url(raw_base_url)
    parsed = urlsplit(base_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    path = f"{parsed.path}/websocket"
    return urlunsplit((scheme, parsed.netloc, path, "", ""))


def websocket_endpoint_matches(expected_url: Any, actual_url: Any) -> bool:
    """Return whether a completed WS handshake stayed on its exact endpoint."""

    def endpoint_key(raw_url: Any) -> tuple[str, str, int, str]:
        if not isinstance(raw_url, str) or not raw_url:
            raise ValueError
        if (
            _has_forbidden_url_chars(raw_url)
            or "\\" in raw_url
            or _PERCENT_ESCAPE_RE.search(raw_url)
        ):
            raise ValueError
        parsed = urlsplit(raw_url)
        scheme = parsed.scheme.lower()
        if (
            scheme not in {"ws", "wss"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or "@" in parsed.netloc
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError
        port = parsed.port
        if port is None:
            port = 443 if scheme == "wss" else 80
        return scheme, parsed.hostname.lower(), port, parsed.path

    try:
        return endpoint_key(expected_url) == endpoint_key(str(actual_url))
    except (TypeError, ValueError):
        return False


def check_requirements() -> bool:
    """Return True if the Rocket.Chat adapter can be used."""
    token = os.getenv("ROCKETCHAT_TOKEN", "")
    url = os.getenv("ROCKETCHAT_URL", "")
    user_id = os.getenv("ROCKETCHAT_USER_ID", "")
    try:
        validate_auth_config(url, token, user_id)
    except ValueError:
        return False
    try:
        import aiohttp  # noqa: F401
        return True
    except ImportError:
        return False


def validate_config(config) -> bool:
    """Validate that the platform config has enough info to connect."""
    extra = getattr(config, "extra", {}) or {}
    url = os.getenv("ROCKETCHAT_URL") or extra.get("url", "")
    token = os.getenv("ROCKETCHAT_TOKEN") or getattr(config, "token", "") or extra.get("token", "")
    user_id = os.getenv("ROCKETCHAT_USER_ID") or extra.get("user_id", "")
    try:
        validate_auth_config(url, token, user_id)
        return True
    except ValueError:
        return False


def is_connected(config) -> bool:
    """Check whether Rocket.Chat is configured (env or config.yaml)."""
    return validate_config(config)


def _env_enablement() -> dict | None:
    """Seed ``PlatformConfig.extra`` from env vars during gateway config load.

    Called by the platform registry's env-enablement hook BEFORE adapter
    construction, so ``gateway status`` reflects env-only configuration
    without instantiating the Rocket.Chat client.

    Returns ``None`` when Rocket.Chat isn't minimally configured.
    """
    raw_url = os.getenv("ROCKETCHAT_URL", "")
    token = os.getenv("ROCKETCHAT_TOKEN", "").strip()
    user_id = os.getenv("ROCKETCHAT_USER_ID", "").strip()
    try:
        url, token, user_id = validate_auth_config(raw_url, token, user_id)
    except ValueError:
        return None

    seed: dict = {
        "url": url,
        "token": token,
        "user_id": user_id,
    }

    reply_mode = os.getenv("ROCKETCHAT_REPLY_MODE", "").strip()
    if reply_mode:
        seed["reply_mode"] = reply_mode

    suppress_home_notice = os.getenv(
        "ROCKETCHAT_SUPPRESS_HOME_CHANNEL_NOTICE", ""
    ).strip()
    if suppress_home_notice:
        seed["suppress_home_channel_notice"] = suppress_home_notice

    home = os.getenv("ROCKETCHAT_HOME_CHANNEL", "").strip()
    if home:
        seed["home_channel"] = {
            "chat_id": home,
            "name": os.getenv("ROCKETCHAT_HOME_CHANNEL_NAME", home),
        }

    return seed


async def _standalone_send(
    pconfig,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Optional[List[str]] = None,
    force_document: bool = False,
) -> Dict[str, Any]:
    """Open an ephemeral REST-only connection to send a message for cron delivery.

    Uses ``chat.postMessage`` via aiohttp — no DDP WebSocket (too heavy for
    one-shot sends).

    ``thread_id`` and ``media_files`` are accepted for signature parity but
    ``media_files`` is not implemented yet for the standalone path.
    """
    if (
        not is_valid_server_identifier(chat_id)
        or not isinstance(message, str)
        or (thread_id is not None and not is_valid_server_identifier(thread_id))
    ):
        return {"error": "Rocket.Chat standalone send target is invalid"}
    extra = getattr(pconfig, "extra", {}) or {}
    raw_url = os.getenv("ROCKETCHAT_URL") or extra.get("url", "")
    raw_token = os.getenv("ROCKETCHAT_TOKEN") or getattr(pconfig, "token", "") or extra.get("token", "")
    raw_user_id = os.getenv("ROCKETCHAT_USER_ID") or extra.get("user_id", "")
    try:
        url, token, user_id = validate_auth_config(
            raw_url, raw_token, raw_user_id
        )
    except ValueError:
        return {"error": "Rocket.Chat standalone send configuration is invalid"}

    headers = {
        "X-Auth-Token": token,
        "X-User-Id": user_id,
        "Content-Type": "application/json",
    }
    payload: Dict[str, Any] = {
        "roomId": chat_id,
        "text": message,
    }
    if thread_id:
        payload["tmid"] = thread_id

    import aiohttp

    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30), trust_env=False
        ) as session:
            async with session.post(
                f"{url}/api/v1/chat.postMessage",
                headers=headers,
                json=payload,
                allow_redirects=False,
            ) as resp:
                if resp.status < 200 or resp.status >= 300:
                    logger.warning(
                        "Rocket.Chat standalone send rejected with HTTP %s",
                        resp.status,
                    )
                    return {"error": "Rocket.Chat standalone send was rejected"}
                data = await read_bounded_json_response(resp)
                if not isinstance(data, dict) or data.get("success") is not True:
                    return {"error": "Rocket.Chat standalone send returned an invalid response"}
                msg = data.get("message")
                if (
                    not isinstance(msg, dict)
                    or not is_valid_server_identifier(msg.get("_id"))
                    or msg.get("rid") != chat_id
                    or (thread_id and msg.get("tmid") != thread_id)
                ):
                    return {"error": "Rocket.Chat standalone send returned an invalid target"}
                return {"success": True, "message_id": msg["_id"]}
    except Exception as exc:
        logger.warning(
            "Rocket.Chat standalone send failed (%s)", type(exc).__name__
        )
        return {"error": "Rocket.Chat standalone send failed"}
