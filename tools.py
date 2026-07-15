"""Agent tools: channel discovery/creation and direct messages.

Registered into the ``rocketchat`` toolset (see ``register()`` in
``__init__.py``), which the gateway auto-includes for Rocket.Chat
sessions. Handlers are REST-only one-shots using env credentials, so
they also work outside the gateway process (e.g. in cron job sessions).
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from tools.registry import tool_error, tool_result


async def _api(
    method: str,
    path: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """One-shot authenticated ``/api/v1`` call. Errors come back under ``_error``."""
    import aiohttp

    url = os.getenv("ROCKETCHAT_URL", "").rstrip("/")
    headers = {
        "X-Auth-Token": os.getenv("ROCKETCHAT_TOKEN", ""),
        "X-User-Id": os.getenv("ROCKETCHAT_USER_ID", ""),
        "Content-Type": "application/json",
    }
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30)
        ) as session:
            async with session.request(
                method,
                f"{url}/api/v1/{path}",
                headers=headers,
                params=params,
                json=payload,
            ) as resp:
                data = await resp.json(content_type=None) or {}
                if resp.status >= 400 or not data.get("success", True):
                    err = data.get("error") or f"HTTP {resp.status}"
                    return {"_error": str(err)}
                return data
    except Exception as exc:
        return {"_error": str(exc)}


async def handle_list_channels(args: dict, **kw) -> str:
    """List public channels and private groups visible to the bot."""
    rooms: list = []
    errors: list = []
    for path, key, rtype in (
        ("channels.list", "channels", "channel"),
        ("groups.list", "groups", "group"),
    ):
        data = await _api("GET", path, params={"count": 100})
        if "_error" in data:
            errors.append(f"{path}: {data['_error']}")
            continue
        for room in data.get(key) or []:
            rooms.append(
                {
                    "room_id": room.get("_id"),
                    "name": room.get("name"),
                    "type": rtype,
                    "topic": room.get("topic") or "",
                    "members": room.get("usersCount"),
                }
            )
    name_filter = str(args.get("filter") or "").strip().lower()
    if name_filter:
        rooms = [r for r in rooms if name_filter in (r["name"] or "").lower()]
    if not rooms and errors:
        return tool_error("; ".join(errors))
    result: Dict[str, Any] = {"channels": rooms, "count": len(rooms)}
    if errors:
        # channels.list needs the view-c-room permission; groups.list only
        # returns rooms the bot is a member of — partial results are normal.
        result["warnings"] = errors
    return tool_result(result)


async def handle_create_channel(args: dict, **kw) -> str:
    """Create a public channel or private group, optionally inviting members."""
    name = str(args.get("name") or "").strip()
    if not name:
        return tool_error("name is required")
    private = bool(args.get("private"))
    payload: Dict[str, Any] = {"name": name}
    members = args.get("members") or []
    if members:
        payload["members"] = [str(m).strip().lstrip("@") for m in members if str(m).strip()]
    path = "groups.create" if private else "channels.create"
    data = await _api("POST", path, payload=payload)
    if "_error" in data:
        return tool_error(f"Failed to create {'group' if private else 'channel'}: {data['_error']}")
    room = data.get("group" if private else "channel") or {}
    return tool_result(
        room_id=room.get("_id"),
        name=room.get("name"),
        private=private,
        members=payload.get("members", []),
    )


async def handle_post(args: dict, **kw) -> str:
    """Post a message to a channel/group by name or room id."""
    message = str(args.get("message") or "").strip()
    if not message:
        return tool_error("message is required")
    room_id = str(args.get("room_id") or "").strip()
    channel = str(args.get("channel") or "").strip().lstrip("#")
    if not room_id and not channel:
        return tool_error("channel (name) or room_id is required")

    payload: Dict[str, Any] = {"text": message}
    if room_id:
        payload["roomId"] = room_id
        target = room_id
    else:
        payload["channel"] = f"#{channel}"
        target = f"#{channel}"
    data = await _api("POST", "chat.postMessage", payload=payload)
    if "_error" in data:
        return tool_error(
            f"Failed to post to {target}: {data['_error']} "
            "(is the bot a member of the room?)"
        )
    msg = data.get("message") or {}
    return tool_result(
        sent=True,
        target=target,
        room_id=msg.get("rid") or room_id,
        message_id=msg.get("_id"),
    )


async def handle_dm(args: dict, **kw) -> str:
    """Open (or reuse) a DM room with a user; optionally send a message."""
    username = str(args.get("username") or "").strip().lstrip("@")
    if not username:
        return tool_error("username is required")
    data = await _api("POST", "im.create", payload={"username": username})
    if "_error" in data:
        return tool_error(f"Could not open DM with @{username}: {data['_error']}")
    room_id = (data.get("room") or {}).get("_id")
    if not room_id:
        return tool_error(f"im.create returned no room id for @{username}")

    message = str(args.get("message") or "").strip()
    if not message:
        return tool_result(
            room_id=room_id,
            username=username,
            sent=False,
            hint=(
                f"DM room is open. Send now by calling this tool with a message, "
                f"or schedule delivery with cronjob deliver='rocketchat:{room_id}'."
            ),
        )
    sent = await _api(
        "POST", "chat.postMessage", payload={"roomId": room_id, "text": message}
    )
    if "_error" in sent:
        return tool_error(f"DM room open but send failed: {sent['_error']}")
    return tool_result(
        room_id=room_id,
        username=username,
        sent=True,
        message_id=(sent.get("message") or {}).get("_id"),
    )


LIST_CHANNELS_SCHEMA = {
    "name": "rocketchat_list_channels",
    "description": (
        "List channels and private groups on the Rocket.Chat server with their "
        "room_id, name, topic, and member count. Use the room_id as a target for "
        "send_message or cronjob delivery (deliver='rocketchat:<room_id>'). "
        "Public channels require the bot to have the view-c-room permission; "
        "private groups are listed only if the bot is a member."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "filter": {
                "type": "string",
                "description": "Optional case-insensitive substring to filter channel names",
            },
        },
        "required": [],
    },
}

CREATE_CHANNEL_SCHEMA = {
    "name": "rocketchat_create_channel",
    "description": (
        "Create a new Rocket.Chat channel (public) or private group, optionally "
        "inviting members by username. Requires the bot to have the "
        "create-c / create-p permission — expect an error otherwise."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Channel name (no spaces; use-dashes-or-underscores)",
            },
            "private": {
                "type": "boolean",
                "description": "Create a private group instead of a public channel (default false)",
            },
            "members": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Usernames to invite (with or without leading @)",
            },
        },
        "required": ["name"],
    },
}

POST_SCHEMA = {
    "name": "rocketchat_post",
    "description": (
        "Post a message to a Rocket.Chat channel or private group — use this "
        "to deliver results to a different room than the current conversation "
        "(e.g. 'research this thread and post the summary to #reports'). "
        "Target by channel name (leading # optional) or by room_id (from "
        "rocketchat_list_channels). The bot must be a member of the room. "
        "For scheduled posts use cronjob deliver='rocketchat:<room_id>' instead."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "channel": {
                "type": "string",
                "description": "Channel/group name, e.g. '#reports' or 'reports'",
            },
            "room_id": {
                "type": "string",
                "description": "Exact room id (takes precedence over channel)",
            },
            "message": {
                "type": "string",
                "description": "Message text to post (Rocket.Chat renders Markdown)",
            },
        },
        "required": ["message"],
    },
}

DM_SCHEMA = {
    "name": "rocketchat_dm",
    "description": (
        "Open a direct-message room with a Rocket.Chat user by username and "
        "optionally send them a message right away. Always returns the DM "
        "room_id — for scheduled/future delivery (e.g. 'remind @user about X "
        "tomorrow') call this without a message to get the room_id, then "
        "create a cronjob with deliver='rocketchat:<room_id>'."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "username": {
                "type": "string",
                "description": "Rocket.Chat username (with or without leading @)",
            },
            "message": {
                "type": "string",
                "description": "Message to send immediately (omit to just open the room and get its id)",
            },
        },
        "required": ["username"],
    },
}

TOOLS = (
    ("rocketchat_list_channels", LIST_CHANNELS_SCHEMA, handle_list_channels, "📋"),
    ("rocketchat_create_channel", CREATE_CHANNEL_SCHEMA, handle_create_channel, "➕"),
    ("rocketchat_post", POST_SCHEMA, handle_post, "📣"),
    ("rocketchat_dm", DM_SCHEMA, handle_dm, "✉️"),
)
