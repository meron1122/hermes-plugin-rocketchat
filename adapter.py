"""Rocket.Chat gateway adapter: REST client, connection lifecycle,
message sending, and session-title→room-topic sync.

Transport, inbound handling, and media live in sibling modules
(ddp.py, inbound.py, media.py); plugin-level helpers in helpers.py.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any, Dict, Optional

from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.helpers import MessageDeduplicator

from .ddp import DdpTransportMixin
from .helpers import MAX_MESSAGE_LENGTH, _ROOM_TYPE_MAP
from .inbound import InboundMixin
from .media import MediaMixin

logger = logging.getLogger(__name__)


class RocketchatAdapter(
    InboundMixin, MediaMixin, DdpTransportMixin, BasePlatformAdapter
):
    """Gateway adapter for Rocket.Chat (self-hosted).

    Mixins come first so platform-specific hooks (reactions, media,
    DDP) override the BasePlatformAdapter defaults.
    """

    def __init__(self, config, **kwargs):
        platform = Platform("rocketchat")
        super().__init__(config=config, platform=platform)

        extra = getattr(config, "extra", {}) or {}

        self._base_url: str = (
            extra.get("url", "")
            or os.getenv("ROCKETCHAT_URL", "")
        ).rstrip("/")
        self._token: str = getattr(config, "token", None) or extra.get("token", "") or os.getenv("ROCKETCHAT_TOKEN", "")
        self._bot_user_id: str = (
            extra.get("user_id", "")
            or os.getenv("ROCKETCHAT_USER_ID", "")
        )

        # Filled in by connect() once we look up the bot's username.
        self._bot_username: str = ""

        # aiohttp session + websocket handle
        self._session: Any = None  # aiohttp.ClientSession
        self._ws: Any = None       # aiohttp.ClientWebSocketResponse
        self._ws_task: Optional[asyncio.Task] = None
        self._reconnect_task: Optional[asyncio.Task] = None
        self._closing = False

        # DDP bookkeeping
        self._ddp_next_id = 1
        self._ddp_subs: Dict[str, bool] = {}  # sub-id -> ready

        # Room type cache (roomId -> "dm"/"group"/"channel").
        self._room_type_cache: Dict[str, str] = {}

        # Reply mode: "thread" to nest replies, "off" for flat messages.
        self._reply_mode: str = (
            extra.get("reply_mode", "")
            or os.getenv("ROCKETCHAT_REPLY_MODE", "off")
        ).lower()

        # Dedup cache.
        self._dedup = MessageDeduplicator()

        # Title→topic sync state: rate-limit and last-known topic per room.
        self._last_topic_sync: Dict[str, float] = {}  # room_id → timestamp
        self._last_topic: Dict[str, str] = {}  # room_id → last known topic

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        return {
            "X-Auth-Token": self._token,
            "X-User-Id": self._bot_user_id,
            "Content-Type": "application/json",
        }

    async def _api_get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """GET /api/v1/{path}."""
        import aiohttp
        url = f"{self._base_url}/api/v1/{path.lstrip('/')}"
        try:
            async with self._session.get(
                url, headers=self._headers(), params=params,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    logger.error("RC API GET %s → %s: %s", path, resp.status, body[:200])
                    return {}
                return await resp.json()
        except aiohttp.ClientError as exc:
            logger.error("RC API GET %s network error: %s", path, exc)
            return {}

    async def _api_post(
        self, path: str, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        """POST /api/v1/{path} with JSON body."""
        import aiohttp
        url = f"{self._base_url}/api/v1/{path.lstrip('/')}"
        try:
            async with self._session.post(
                url, headers=self._headers(), json=payload,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    logger.error("RC API POST %s → %s: %s", path, resp.status, body[:200])
                    return {}
                return await resp.json()
        except aiohttp.ClientError as exc:
            logger.error("RC API POST %s network error: %s", path, exc)
            return {}

    # ------------------------------------------------------------------
    # Required overrides
    # ------------------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Connect to Rocket.Chat and start the DDP listener.

        Rocket.Chat's DDP stream has no server-side update queue whose startup
        policy depends on ``is_reconnect``.  The argument is accepted to match
        the current Hermes platform-adapter contract.
        """
        import aiohttp

        if not self._base_url or not self._token or not self._bot_user_id:
            logger.error("Rocket.Chat: URL, token, or user id not configured")
            return False

        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30)
        )
        self._closing = False

        # Verify credentials and fetch bot identity.
        me = await self._api_get("me")
        if not me or not me.get("success"):
            logger.error(
                "Rocket.Chat: failed to authenticate — check "
                "ROCKETCHAT_TOKEN, ROCKETCHAT_USER_ID, ROCKETCHAT_URL"
            )
            await self._session.close()
            return False

        if me.get("_id") and me["_id"] != self._bot_user_id:
            logger.warning(
                "Rocket.Chat: ROCKETCHAT_USER_ID (%s) doesn't match /me (%s) — using /me",
                self._bot_user_id, me["_id"],
            )
            self._bot_user_id = me["_id"]
        self._bot_username = me.get("username", "")
        logger.info(
            "Rocket.Chat: authenticated as @%s (%s) on %s",
            self._bot_username,
            self._bot_user_id,
            self._base_url,
        )

        # Start DDP WebSocket in background.
        self._ws_task = asyncio.create_task(self._ws_loop())
        self._mark_connected()
        return True

    async def disconnect(self) -> None:
        """Disconnect from Rocket.Chat."""
        self._closing = True

        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()
            try:
                await self._ws_task
            except (asyncio.CancelledError, Exception):
                pass

        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()

        if self._ws:
            await self._ws.close()
            self._ws = None

        if self._session and not self._session.closed:
            await self._session.close()

        self._mark_disconnected()
        logger.info("Rocket.Chat: disconnected")

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a message (or multiple chunks) to a room."""
        if not content:
            return SendResult(success=True)

        formatted = self.format_message(content)
        chunks = self.truncate_message(formatted, MAX_MESSAGE_LENGTH)

        last_id = None
        for chunk in chunks:
            payload: Dict[str, Any] = {
                "roomId": chat_id,
                "text": chunk,
            }
            if reply_to and self._reply_mode == "thread":
                payload["tmid"] = reply_to

            data = await self._api_post("chat.postMessage", payload)
            if not data or not data.get("success"):
                return SendResult(success=False, error="Failed to post message")
            msg = data.get("message") or {}
            last_id = msg.get("_id") or last_id
            if msg:
                logger.info("Rocket.Chat: send() POST chat.postMessage → rid=%s tmid=%s msg_id=%s",
                            msg.get("rid"), msg.get("tmid"), msg.get("_id"))

        # After sending, sync session title → RC topic for DMs.
        # This fires on every outgoing message but is rate-limited and
        # short-circuits when the title hasn't changed.
        try:
            await self._sync_title_to_rc_topic(chat_id)
        except Exception:
            logger.debug("Title sync failed in send()", exc_info=True)

        return SendResult(success=True, message_id=last_id)

    @staticmethod
    def _set_topic_endpoint(chat_type: str) -> str:
        """Return the RC endpoint key for setting a room topic based on room type."""
        return {
            "dm": "dm.setTopic",
            "channel": "channels.setTopic",
            "group": "groups.setTopic",
        }.get(chat_type, "channels.setTopic")

    async def _sync_title_to_rc_topic(self, chat_id: str) -> None:
        """Sync Hermes session title to RC room topic for DMs/groups/channels.

        Called after every outgoing send().  Checks the current session title
        and updates the RC topic if they differ.  This covers:
          - Auto-title (first-reply title generated by Hermes)
          - /title command (already handled in _handle_message, but also
            catches manual session_db / CLI rename changes that happened
            between messages)
        Rate-limited to at most once every 5 seconds per room.
        """
        import time
        now = time.time()
        if chat_id in self._last_topic_sync and now - self._last_topic_sync[chat_id] < 5:
            return
        self._last_topic_sync[chat_id] = now

        # Only for DM/group/channel rooms where topic setting makes sense
        chat_type = self._room_type_cache.get(chat_id)
        if not chat_type:
            try:
                chat_type = await self._resolve_room_type(chat_id)
            except Exception:
                return
        if chat_type not in ("dm", "group", "channel"):
            return

        # Build a SessionSource and look up the session
        from gateway.config import Platform
        from gateway.session import SessionSource

        session_store = getattr(self, "_session_store", None)
        if not session_store:
            return

        try:
            source = SessionSource(
                platform=Platform("rocketchat"),
                chat_id=chat_id,
                chat_type="dm",
            )
            entry = session_store.get_or_create_session(source)
        except Exception as exc:
            logger.debug("Title sync: session lookup failed: %s", exc)
            return

        # Get the session title from the SQLite DB
        db = getattr(session_store, "_db", None)
        if not db:
            return
        try:
            title = db.get_session_title(entry.session_id)
        except Exception as exc:
            logger.debug("Title sync: get_title failed: %s", exc)
            return
        if not title:
            return

        # Get the current RC topic
        data = await self._api_get("rooms.info", params={"roomId": chat_id})
        room = (data or {}).get("room") or {}
        current_topic = (room.get("topic") or "").strip()

        # Only call the API if topics differ
        if title != current_topic:
            endpoint = self._set_topic_endpoint(chat_type)
            try:
                resp = await self._api_post(endpoint, {
                    "roomId": chat_id,
                    "topic": title,
                })
                if resp and resp.get("success"):
                    self._last_topic[chat_id] = title
                    logger.info(
                        "Rocket.Chat: synced session title '%s' to %s topic (room=%s)",
                        title, chat_type, chat_id,
                    )
            except Exception as exc:
                logger.debug("Title sync: %s failed: %s", endpoint, exc)
        else:
            # Already in sync — just update the cache
            self._last_topic[chat_id] = current_topic

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Return room name and type.

        Rocket.Chat exposes one unified ``rooms.info`` endpoint that works
        for channels, private groups, and DMs.
        """
        data = await self._api_get("rooms.info", params={"roomId": chat_id})
        room = (data or {}).get("room") or {}
        if not room:
            return {"name": chat_id, "type": "channel"}

        raw_type = room.get("t", "c")
        chat_type = _ROOM_TYPE_MAP.get(raw_type, "channel")
        self._room_type_cache[chat_id] = chat_type

        if chat_type == "dm":
            others = [
                u for u in room.get("usernames", [])
                if u and u != self._bot_username
            ]
            name = others[0] if others else chat_id
        else:
            name = room.get("fname") or room.get("name") or chat_id

        return {"name": name, "type": chat_type, "chat_id": chat_id}

    # ------------------------------------------------------------------
    # Optional overrides
    # ------------------------------------------------------------------

    async def send_typing(
        self, chat_id: str, metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """Notify that the bot is typing.

        Rocket.Chat 6.x+ replaced the legacy ``/typing`` stream with
        ``/user-activity``, and 8.x expects the activity string ``"user-typing"``.
        """
        if not self._ws or self._ws.closed:
            return
        if not self._bot_username:
            return
        await self._ddp_method(
            "stream-notify-room",
            [f"{chat_id}/user-activity", self._bot_username, ["user-typing"], {}],
        )

    async def stop_typing(self, chat_id: str) -> None:
        """Clear the typing indicator (empty user-activity list)."""
        if not self._ws or self._ws.closed:
            return
        if not self._bot_username:
            return
        await self._ddp_method(
            "stream-notify-room",
            [f"{chat_id}/user-activity", self._bot_username, [], {}],
        )

    async def edit_message(
        self, chat_id: str, message_id: str, content: str, *, finalize: bool = False
    ) -> SendResult:
        """Edit an existing message via chat.update."""
        formatted = self.format_message(content)
        data = await self._api_post(
            "chat.update",
            {"roomId": chat_id, "msgId": message_id, "text": formatted},
        )
        if not data or not data.get("success"):
            return SendResult(success=False, error="Failed to edit message")
        msg = data.get("message") or {}
        return SendResult(success=True, message_id=msg.get("_id", message_id))

    def format_message(self, content: str) -> str:
        """Rocket.Chat renders Markdown natively and previews plain image
        URLs — strip image markdown to match Mattermost's behavior.

        Also strip Hermes-internal delivery directives (MEDIA:,
        [[audio_as_voice]], [[image]], [[file]]) — the gateway already
        delivers media via send_voice/send_image/send_document methods,
        and these tokens must not reach the Rocket.Chat API as text.
        """
        # Strip image markdown: ![alt](url) → url
        content = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r"\2", content)
        # Strip entire lines and trailing content that start with media tags
        content = re.sub(
            r"(?m)^\s*(?:\[\[audio_as_voice\]\]|\[\[image\]\]|\[\[file\]\]|MEDIA)\s*:?.*(?:\n|$)",
            "",
            content,
        )
        # Also strip orphan MEDIA: references not at line start
        content = re.sub(r"\s*MEDIA:\S+\s*", " ", content)
        return content.strip()
