"""Plugin-level helpers: requirement checks, config validation, env
enablement, shared constants, and the standalone cron sender (REST-only,
no live adapter needed)."""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Rocket.Chat's default Message_MaxAllowedSize is 5000; admins can raise it
# but the safe default for multi-line messages is 5000.
MAX_MESSAGE_LENGTH = 5000

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


def check_requirements() -> bool:
    """Return True if the Rocket.Chat adapter can be used."""
    token = os.getenv("ROCKETCHAT_TOKEN", "")
    url = os.getenv("ROCKETCHAT_URL", "")
    user_id = os.getenv("ROCKETCHAT_USER_ID", "")
    if not token:
        return False
    if not url:
        return False
    if not user_id:
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
    return bool(url and token and user_id)


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
    url = os.getenv("ROCKETCHAT_URL", "").strip()
    token = os.getenv("ROCKETCHAT_TOKEN", "").strip()
    user_id = os.getenv("ROCKETCHAT_USER_ID", "").strip()
    if not (url and token and user_id):
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
    extra = getattr(pconfig, "extra", {}) or {}
    url = os.getenv("ROCKETCHAT_URL") or extra.get("url", "")
    token = os.getenv("ROCKETCHAT_TOKEN") or getattr(pconfig, "token", "") or extra.get("token", "")
    user_id = os.getenv("ROCKETCHAT_USER_ID") or extra.get("user_id", "")
    if not url or not token or not user_id:
        return {"error": "Rocket.Chat standalone send: ROCKETCHAT_URL, TOKEN, and USER_ID must be configured"}

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
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            async with session.post(
                f"{url.rstrip('/')}/api/v1/chat.postMessage",
                headers=headers,
                json=payload,
            ) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    return {"error": f"Rocket.Chat standalone send HTTP {resp.status}: {body[:200]}"}
                data = await resp.json()
                msg = data.get("message") or {}
                return {"success": True, "message_id": msg.get("_id", "")}
    except Exception as exc:
        logger.debug("Rocket.Chat standalone send raised", exc_info=True)
        return {"error": f"Rocket.Chat standalone send failed: {exc}"}
