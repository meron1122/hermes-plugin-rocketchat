"""Agent tools: channel discovery/creation, direct messages, and file uploads.

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


async def handle_send_file(args: dict, **kw) -> str:
    """Upload a local file to a Rocket.Chat channel, group, or DM via rooms.media (two-step).

    Target resolution priority:
      1. room_id  — exact room ID (preferred; from rocketchat_dm or rocketchat_list_channels)
      2. username — a REAL Rocket.Chat login (not a display name); resolved via im.create
      3. channel  — channel/group name (resolved via channels.info)
    Never construct or guess a room_id from a name. Pass a literal ID you already hold.
    """
    import mimetypes
    from pathlib import Path

    file_path = str(args.get("file_path") or "").strip()
    if not file_path:
        return tool_error("file_path is required")

    p = Path(file_path)
    if not p.exists():
        return tool_error(f"File not found: {file_path}")

    room_id = str(args.get("room_id") or "").strip()
    username = str(args.get("username") or "").strip().lstrip("@")
    channel = str(args.get("channel") or "").strip().lstrip("#")

    if not room_id and not username and not channel:
        return tool_error(
            "One of room_id, username, or channel is required. "
            "Use a literal room_id (from rocketchat_dm) or a real username — do not guess."
        )

    # Resolve room_id from a real username via im.create (idempotent: reuses existing DM)
    if not room_id and username:
        data = await _api("POST", "im.create", payload={"username": username})
        if "_error" in data:
            return tool_error(f"Could not open DM with @{username}: {data['_error']}")
        room = data.get("room") or {}
        room_id = room.get("_id") or ""
        # Ghost-room guard: a valid DM must contain the target user + the bot (>= 2 members)
        members = room.get("usernames") or []
        if len(members) < 2 or username not in members:
            return tool_error(
                f"DM room for @{username} has no real recipient (members: {members}). "
                f"The username is incorrect or the user does not exist — file not sent."
            )
        if not room_id:
            return tool_error(f"im.create returned no room id for @{username}")

    # Resolve room_id from channel name if needed
    if not room_id:
        data = await _api("GET", "channels.info", params={"roomName": channel})
        if "_error" in data:
            return tool_error(f"Could not find channel #{channel}: {data['_error']}")
        room_id = (data.get("channel") or {}).get("_id")
        if not room_id:
            return tool_error(f"Channel #{channel} returned no room id")

    # Prepare file data
    fname = str(args.get("file_name") or "").strip() or p.name
    ct = mimetypes.guess_type(fname)[0] or "application/octet-stream"
    file_data = p.read_bytes()
    caption = str(args.get("caption") or "").strip() or None
    tmid = str(args.get("tmid") or "").strip() or None

    # Two-step rooms.media upload
    import aiohttp

    url = os.getenv("ROCKETCHAT_URL", "").rstrip("/")
    headers = {
        "X-Auth-Token": os.getenv("ROCKETCHAT_TOKEN", ""),
        "X-User-Id": os.getenv("ROCKETCHAT_USER_ID", ""),
    }

    # Step 1: upload bytes
    step1_url = f"{url}/api/v1/rooms.media/{room_id}"
    form = aiohttp.FormData()
    form.add_field("file", file_data, filename=fname, content_type=ct)
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=120)
        ) as session:
            async with session.post(
                step1_url, headers=headers, data=form
            ) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    return tool_error(f"Upload step 1 failed ({resp.status}): {body[:200]}")
                step1 = await resp.json()
    except Exception as exc:
        return tool_error(f"Upload step 1 error: {exc}")

    file_id = (step1.get("file") or {}).get("_id")
    if not file_id:
        return tool_error(f"Upload step 1 returned no file id: {step1}")

    # Step 2: confirm + create message
    step2_payload: Dict[str, Any] = {}
    if caption:
        step2_payload["msg"] = caption
    if tmid:
        step2_payload["tmid"] = tmid

    step2_data = await _api(
        "POST",
        f"rooms.mediaConfirm/{room_id}/{file_id}",
        payload=step2_payload,
    )
    if "_error" in step2_data:
        return tool_error(f"Upload step 2 failed: {step2_data['_error']}")

    msg = step2_data.get("message") or {}
    target = f"@{username}" if username else (f"#{channel}" if channel else room_id)
    return tool_result(
        sent=True,
        target=target,
        room_id=room_id,
        message_id=msg.get("_id"),
        file=fname,
        size=len(file_data),
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

SEND_FILE_SCHEMA = {
    "name": "rocketchat_send_file",
    "description": (
        "Upload a local file to a Rocket.Chat channel, group, or DM. "
        "Uses the two-step rooms.media flow — no size limit beyond server config. "
        "Returns the message_id of the created file message. "
        "TARGET (pick ONE, in priority order): "
        "1) room_id — the exact room ID you already hold (from rocketchat_dm or "
        "rocketchat_list_channels). PREFERRED. "
        "2) username — a REAL Rocket.Chat login (e.g. 'younesamalou'), NOT a display name. "
        "3) channel — a channel/group name. "
        "NEVER guess or construct a room_id from a name; pass a literal ID you received."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Absolute path to the local file to upload",
            },
            "room_id": {
                "type": "string",
                "description": (
                    "Exact room ID (takes precedence over username/channel). "
                    "Use the literal ID returned by rocketchat_dm or rocketchat_list_channels — "
                    "do not invent or derive it from a username."
                ),
            },
            "username": {
                "type": "string",
                "description": (
                    "Target user's REAL Rocket.Chat login (no leading @), e.g. 'younesamalou'. "
                    "Must be an actual username, not a display name. Resolved via im.create; "
                    "the send is rejected if the user does not exist."
                ),
            },
            "channel": {
                "type": "string",
                "description": "Channel/group name, e.g. '#reports' or 'reports'",
            },
            "caption": {
                "type": "string",
                "description": "Optional message text to attach to the file",
            },
            "file_name": {
                "type": "string",
                "description": "Override the displayed filename (default: basename of file_path)",
            },
            "tmid": {
                "type": "string",
                "description": "Optional thread root message id — file will be posted inside that thread",
            },
        },
        "required": ["file_path"],
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
    ("rocketchat_send_file", SEND_FILE_SCHEMA, handle_send_file, "📎"),
    ("rocketchat_dm", DM_SCHEMA, handle_dm, "✉️"),
)
