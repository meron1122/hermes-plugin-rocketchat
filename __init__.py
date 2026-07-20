"""Rocket.Chat gateway adapter plugin for Hermes Agent.

Connects to a self-hosted Rocket.Chat instance via its REST API (v1) for
outbound traffic and the Realtime (DDP) WebSocket for inbound messages.
No external Rocket.Chat library required — uses aiohttp, which is already
a Hermes dependency.

Design notes:
    Rocket.Chat's docs recommend REST for writes (chat.postMessage,
    chat.update, rooms.media) and DDP for reads (stream-room-messages).
    The bot subscribes to the ``__my_messages__`` virtual room id, which
    covers every channel/DM/group the bot is a member of — no per-room
    enumeration required.

    Personal Access Tokens double as DDP resume tokens, so a single
    ``ROCKETCHAT_TOKEN`` + ``ROCKETCHAT_USER_ID`` pair authenticates both
    surfaces. Generate the PAT with "Ignore Two Factor" checked to keep
    unattended REST calls working on 2FA-enabled workspaces.

Environment variables:
    ROCKETCHAT_URL              Server URL (e.g. https://rc.example.com)
    ROCKETCHAT_TOKEN            Personal Access Token (used as auth token)
    ROCKETCHAT_USER_ID          Bot user's _id (shown alongside the PAT)
    ROCKETCHAT_ALLOWED_USERS    Comma-separated user IDs
    ROCKETCHAT_ALLOW_ALL_USERS  Allow all users (dev only)
    ROCKETCHAT_HOME_CHANNEL     Room ID for cron/notification delivery
    ROCKETCHAT_SUPPRESS_HOME_CHANNEL_NOTICE  Hide missing-home-channel notice
    ROCKETCHAT_REQUIRE_MENTION  Require @mention in channels (default: true)
    ROCKETCHAT_FREE_RESPONSE_CHANNELS  Rooms exempt from mention requirement
    ROCKETCHAT_REPLY_MODE       Channel/group replies: 'thread' or 'off' (default: off)
    ROCKETCHAT_REACTIONS        Add 👀/✅/❌ reactions to messages (default: true)
    ROCKETCHAT_AGENT_FILE_MAX_BYTES  Agent-tool upload guard (default: 100 MiB; 0 disables)
"""

from .adapter import RocketchatAdapter
from .helpers import (
    MAX_MESSAGE_LENGTH,
    _env_enablement,
    _standalone_send,
    check_requirements,
    is_connected,
    validate_config,
)
from .setup_wizard import interactive_setup
from .tools import TOOLS

__all__ = ["register", "RocketchatAdapter"]


def register(ctx):
    """Plugin entry point: called by the Hermes plugin system."""
    # Agent tools — land in the auto-generated ``hermes-rocketchat``
    # toolset because their toolset name matches the platform name.
    for tool_name, schema, handler, emoji in TOOLS:
        ctx.register_tool(
            name=tool_name,
            toolset="rocketchat",
            schema=schema,
            handler=handler,
            check_fn=check_requirements,
            is_async=True,
            emoji=emoji,
        )

    ctx.register_platform(
        name="rocketchat",
        label="Rocket.Chat",
        adapter_factory=lambda cfg: RocketchatAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["ROCKETCHAT_URL", "ROCKETCHAT_TOKEN", "ROCKETCHAT_USER_ID"],
        install_hint="Uses aiohttp (already a Hermes dependency) — no extra packages needed",
        setup_fn=interactive_setup,
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="ROCKETCHAT_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        allowed_users_env="ROCKETCHAT_ALLOWED_USERS",
        allow_all_env="ROCKETCHAT_ALLOW_ALL_USERS",
        max_message_length=MAX_MESSAGE_LENGTH,
        emoji="🚀",
        pii_safe=False,
        allow_update_command=True,
        platform_hint=(
            "You are chatting via Rocket.Chat. Rocket.Chat renders Markdown natively. "
            "In channels, users must @mention you for the bot to respond (unless the room "
            "is in the free-response list). Channel/group replies can be threaded "
            "(ROCKETCHAT_REPLY_MODE); DM replies always stay flat. "
            "Keep responses clear and concise."
        ),
    )
