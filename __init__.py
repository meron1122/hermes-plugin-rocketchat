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
    ROCKETCHAT_AGENT_FILE_UPLOADS   Enable agent-triggered local file uploads
    ROCKETCHAT_AGENT_FILE_ALLOWED_ROOTS  Absolute upload roots (POSIX path separator)
    ROCKETCHAT_AGENT_FILE_MAX_CONCURRENCY  Concurrent file operations (default: 1)
    ROCKETCHAT_AGENT_WRITE_TOOLS     Enable mutating agent tools (default: false)
    ROCKETCHAT_AGENT_WRITE_ALLOWED_ROOMS  Exact cross-room write allowlist
    ROCKETCHAT_AGENT_WRITE_TRUSTED_USERS  Trusted privileged/cross-room writers
    ROCKETCHAT_AGENT_TOOLS_ALLOW_EXTERNAL  Allow writes outside Rocket.Chat sessions
    ROCKETCHAT_RETRIEVAL_ALLOWED_ROOMS  Exact cross-room/contextless read allowlist
    ROCKETCHAT_RETRIEVAL_TRUSTED_USERS  Users allowed to use the cross-room allowlist
    ROCKETCHAT_RETRIEVAL_ALLOW_CONTEXTLESS  Enable allowlisted contextless reads
    ROCKETCHAT_THREAD_CONTEXT_MAX_CHARS  Inbound thread-context character budget
    ROCKETCHAT_MEDIA_DOWNLOAD_MAX_BYTES  Network-media byte budget
    ROCKETCHAT_FORWARDED_SLASH_COMMANDS  Exact RC-native command allowlist
    ROCKETCHAT_TOPIC_SYNC             Enable room-topic writes (default: false)
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
from .tools import (
    TOOLS,
    file_uploads_enabled,
    validate_tool_configuration,
    write_tools_enabled,
)

__all__ = ["register", "RocketchatAdapter"]


def _read_tool_requirements() -> bool:
    """Read tools require valid credentials and a hardened server URL."""
    return bool(check_requirements() and validate_tool_configuration())


def _write_tool_requirements() -> bool:
    """Mutating tools are absent unless the operator explicitly enables them."""
    return bool(_read_tool_requirements() and write_tools_enabled())


def _file_upload_requirements() -> bool:
    """Uploads require the write grant plus their own scoped capability."""
    return bool(_read_tool_requirements() and file_uploads_enabled())


def register(ctx):
    """Plugin entry point: called by the Hermes plugin system."""
    # Keep read and write capabilities independently selectable.  Runtime
    # guards in the handlers remain authoritative even if a caller obtains a
    # handler reference or a stale tool definition.
    for tool_name, schema, handler, emoji, toolset in TOOLS:
        ctx.register_tool(
            name=tool_name,
            toolset=toolset,
            schema=schema,
            handler=handler,
            check_fn=(
                _file_upload_requirements
                if tool_name == "rocketchat_send_file"
                else (
                    _write_tool_requirements
                    if toolset == "rocketchat_write"
                    else _read_tool_requirements
                )
            ),
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
