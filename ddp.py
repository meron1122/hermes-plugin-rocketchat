"""DDP / WebSocket transport: realtime inbound stream with reconnect."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, Dict, List

from .helpers import (
    _DDP_PROTOCOL_VERSION,
    _RECONNECT_BASE_DELAY,
    _RECONNECT_JITTER,
    _RECONNECT_MAX_DELAY,
    websocket_endpoint_matches,
    websocket_url,
)

logger = logging.getLogger(__name__)


class DdpTransportMixin:
    """DDP WebSocket layer of :class:`~.adapter.RocketchatAdapter`."""

    async def _ddp_send(self, payload: Dict[str, Any]) -> None:
        """Send a DDP frame if the socket is open."""
        if not self._ws or self._ws.closed:
            return
        await self._ws.send_json(payload)

    async def _ddp_method(self, method: str, params: List[Any]) -> str:
        """Invoke a DDP method (fire-and-forget). Returns the method id."""
        call_id = str(self._ddp_next_id)
        self._ddp_next_id += 1
        await self._ddp_send({
            "msg": "method",
            "method": method,
            "id": call_id,
            "params": params,
        })
        return call_id

    async def _ddp_sub(self, name: str, params: List[Any]) -> str:
        """Subscribe to a DDP publication. Returns the sub id."""
        sub_id = str(uuid.uuid4())
        self._ddp_subs[sub_id] = False
        await self._ddp_send({
            "msg": "sub",
            "id": sub_id,
            "name": name,
            "params": params,
        })
        return sub_id

    async def _ws_loop(self) -> None:
        """Connect to the DDP socket and listen for events, reconnecting on failure."""
        delay = _RECONNECT_BASE_DELAY
        while not self._closing:
            try:
                await self._ws_connect_and_listen()
                delay = _RECONNECT_BASE_DELAY
            except asyncio.CancelledError:
                return
            except Exception as exc:
                if self._closing:
                    return
                import aiohttp
                if isinstance(exc, aiohttp.WSServerHandshakeError) and exc.status in (401, 403):
                    logger.error("Rocket.Chat WS auth failed (HTTP %d) — stopping reconnect", exc.status)
                    return
                err_str = str(exc).lower()
                if "401" in err_str or "403" in err_str or "unauthorized" in err_str:
                    logger.error(
                        "Rocket.Chat WS permanent error (%s) — stopping reconnect",
                        type(exc).__name__,
                    )
                    return
                logger.warning(
                    "Rocket.Chat WS error (%s) — reconnecting in %.0fs",
                    type(exc).__name__,
                    delay,
                )

            if self._closing:
                return

            import random
            jitter = delay * _RECONNECT_JITTER * random.random()
            await asyncio.sleep(delay + jitter)
            delay = min(delay * 2, _RECONNECT_MAX_DELAY)

    async def _ws_connect_and_listen(self) -> None:
        """Single DDP WebSocket session: connect, login, subscribe, listen."""
        ws_url = websocket_url(self._base_url)
        logger.info("Rocket.Chat: DDP connecting to %s", ws_url)

        self._ws = await self._session.ws_connect(ws_url, heartbeat=None)
        response = getattr(self._ws, "_response", None)
        final_url = getattr(response, "url", None)
        if not websocket_endpoint_matches(ws_url, final_url):
            await self._ws.close()
            self._ws = None
            raise RuntimeError("Rocket.Chat WebSocket endpoint changed")
        self._ddp_subs.clear()

        await self._ddp_send({
            "msg": "connect",
            "version": _DDP_PROTOCOL_VERSION,
            "support": [_DDP_PROTOCOL_VERSION],
        })

        await self._ddp_method("login", [{"resume": self._token}])

        await self._ddp_sub("stream-room-messages", ["__my_messages__", False])
        logger.info("Rocket.Chat: DDP logged in and subscribed")

        async for raw_msg in self._ws:
            if self._closing:
                return

            if raw_msg.type in (raw_msg.type.TEXT, raw_msg.type.BINARY):
                try:
                    event = json.loads(raw_msg.data)
                except (json.JSONDecodeError, TypeError):
                    continue
                await self._handle_ddp_frame(event)
            elif raw_msg.type in (
                raw_msg.type.ERROR, raw_msg.type.CLOSE,
                raw_msg.type.CLOSING, raw_msg.type.CLOSED,
            ):
                logger.info("Rocket.Chat: DDP WebSocket closed (%s)", raw_msg.type)
                break

    async def _handle_ddp_frame(self, event: Dict[str, Any]) -> None:
        """Dispatch a single DDP frame."""
        kind = event.get("msg")
        if kind == "ping":
            pong: Dict[str, Any] = {"msg": "pong"}
            if "id" in event:
                pong["id"] = event["id"]
            await self._ddp_send(pong)
            return

        if kind == "ready":
            for sub_id in event.get("subs", []):
                self._ddp_subs[sub_id] = True
            return

        if kind == "nosub":
            sub_id = event.get("id", "")
            err = event.get("error") or {}
            self._ddp_subs.pop(sub_id, None)
            if err:
                logger.warning("Rocket.Chat: DDP subscription was rejected")
            return

        if kind == "changed":
            collection = event.get("collection")
            if collection != "stream-room-messages":
                return
            fields = event.get("fields") or {}
            args = fields.get("args") or []
            if not args:
                return
            await self._handle_message(args[0])
            return
