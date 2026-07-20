"""Tests for the Rocket.Chat platform adapter plugin.

Adapted from the test suite of hermes-agent PR #4637 to the extended
adapter of PR #30463 (PAT-only auth, reply_mode, topic sync, slash
command routing).
"""

import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from gateway.config import Platform, PlatformConfig
from gateway.platform_registry import PlatformEntry, platform_registry
from gateway.platforms.base import MessageEvent, MessageType, ProcessingOutcome
from gateway.session import (
    SessionContext,
    SessionSource,
    build_session_context_prompt,
    build_session_key,
)

# ``Platform("rocketchat")`` resolves through the runtime platform registry
# (the enum's _missing_ hook rejects arbitrary strings) — register a stub
# entry so the dynamic member exists under pytest, where the plugin system
# never runs.
if not platform_registry.is_registered("rocketchat"):
    platform_registry.register(
        PlatformEntry(
            name="rocketchat",
            label="Rocket.Chat",
            adapter_factory=lambda cfg: None,
            check_fn=lambda: True,
        )
    )


def _load_plugin():
    """Import the plugin as a package under a fixed module name.

    The repo directory name ("hermes-plugin-rocketchat") is not a valid
    Python identifier, so import by path with explicit package search
    locations — this makes the plugin's relative imports work.
    """
    name = "rocketchat_plugin"
    if name in sys.modules:
        return sys.modules[name]
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        name, root / "__init__.py", submodule_search_locations=[str(root)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_rc = _load_plugin()

RocketchatAdapter = _rc.RocketchatAdapter
check_requirements = _rc.check_requirements
validate_config = _rc.validate_config
is_connected = _rc.is_connected
register = _rc.register
_env_enablement = _rc._env_enablement


@pytest.fixture(autouse=True)
def _clean_rocketchat_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("ROCKETCHAT_"):
            monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------------
# Platform enum
# ---------------------------------------------------------------------------


class TestPlatformEnum:
    def test_dynamic_member_value(self):
        assert Platform("rocketchat").value == "rocketchat"

    def test_identity_stable(self):
        assert Platform("rocketchat") is Platform("rocketchat")


# ---------------------------------------------------------------------------
# Requirements & config validation
# ---------------------------------------------------------------------------


class TestRequirementsCheck:
    def test_fails_without_anything(self):
        assert check_requirements() is False

    def test_fails_without_token(self, monkeypatch):
        monkeypatch.setenv("ROCKETCHAT_URL", "https://rc.example.com")
        monkeypatch.setenv("ROCKETCHAT_USER_ID", "uid123")
        assert check_requirements() is False

    def test_fails_without_user_id(self, monkeypatch):
        monkeypatch.setenv("ROCKETCHAT_URL", "https://rc.example.com")
        monkeypatch.setenv("ROCKETCHAT_TOKEN", "my-pat")
        assert check_requirements() is False

    def test_passes_with_pat(self, monkeypatch):
        monkeypatch.setenv("ROCKETCHAT_URL", "https://rc.example.com")
        monkeypatch.setenv("ROCKETCHAT_TOKEN", "my-pat")
        monkeypatch.setenv("ROCKETCHAT_USER_ID", "uid123")
        assert check_requirements() is True


class TestValidateConfig:
    def _cfg(self, **extra):
        return PlatformConfig(enabled=True, extra=extra)

    def test_passes_with_env(self, monkeypatch):
        monkeypatch.setenv("ROCKETCHAT_URL", "https://rc.example.com")
        monkeypatch.setenv("ROCKETCHAT_TOKEN", "my-pat")
        monkeypatch.setenv("ROCKETCHAT_USER_ID", "uid123")
        assert validate_config(self._cfg()) is True

    def test_passes_with_extra_fields(self):
        cfg = self._cfg(url="https://rc.example.com", token="pat", user_id="uid123")
        assert validate_config(cfg) is True

    def test_fails_without_url(self):
        assert validate_config(self._cfg(token="pat", user_id="uid123")) is False

    def test_fails_without_token(self):
        assert validate_config(self._cfg(url="https://rc.example.com", user_id="uid123")) is False

    def test_is_connected_delegates_to_validate_config(self):
        cfg = self._cfg(url="https://rc.example.com", token="pat", user_id="uid123")
        assert is_connected(cfg) == validate_config(cfg)


class TestEnvEnablement:
    def test_none_when_unconfigured(self):
        assert _env_enablement() is None

    def test_seeds_credentials(self, monkeypatch):
        monkeypatch.setenv("ROCKETCHAT_URL", "https://rc.example.com")
        monkeypatch.setenv("ROCKETCHAT_TOKEN", "my-pat")
        monkeypatch.setenv("ROCKETCHAT_USER_ID", "uid123")
        seed = _env_enablement()
        assert seed == {
            "url": "https://rc.example.com",
            "token": "my-pat",
            "user_id": "uid123",
        }

    def test_seeds_home_channel_and_reply_mode(self, monkeypatch):
        monkeypatch.setenv("ROCKETCHAT_URL", "https://rc.example.com")
        monkeypatch.setenv("ROCKETCHAT_TOKEN", "my-pat")
        monkeypatch.setenv("ROCKETCHAT_USER_ID", "uid123")
        monkeypatch.setenv("ROCKETCHAT_HOME_CHANNEL", "room42")
        monkeypatch.setenv("ROCKETCHAT_REPLY_MODE", "thread")
        seed = _env_enablement()
        assert seed["reply_mode"] == "thread"
        assert seed["home_channel"]["chat_id"] == "room42"

    def test_seeds_home_notice_suppression(self, monkeypatch):
        monkeypatch.setenv("ROCKETCHAT_URL", "https://rc.example.com")
        monkeypatch.setenv("ROCKETCHAT_TOKEN", "my-pat")
        monkeypatch.setenv("ROCKETCHAT_USER_ID", "uid123")
        monkeypatch.setenv("ROCKETCHAT_SUPPRESS_HOME_CHANNEL_NOTICE", "true")
        seed = _env_enablement()
        assert seed["suppress_home_channel_notice"] == "true"


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------


class TestPluginRegistration:
    def _kwargs(self):
        ctx = MagicMock()
        register(ctx)
        ctx.register_platform.assert_called_once()
        return ctx.register_platform.call_args[1]

    def test_register_name(self):
        assert self._kwargs()["name"] == "rocketchat"

    def test_register_auth_env_vars(self):
        kwargs = self._kwargs()
        assert kwargs["allowed_users_env"] == "ROCKETCHAT_ALLOWED_USERS"
        assert kwargs["allow_all_env"] == "ROCKETCHAT_ALLOW_ALL_USERS"

    def test_register_cron_delivery(self):
        kwargs = self._kwargs()
        assert kwargs["cron_deliver_env_var"] == "ROCKETCHAT_HOME_CHANNEL"
        assert callable(kwargs["standalone_sender_fn"])

    def test_register_has_setup_fn_and_hint(self):
        kwargs = self._kwargs()
        assert callable(kwargs["setup_fn"])
        assert kwargs["platform_hint"]

    def test_register_required_env(self):
        assert set(self._kwargs()["required_env"]) == {
            "ROCKETCHAT_URL",
            "ROCKETCHAT_TOKEN",
            "ROCKETCHAT_USER_ID",
        }


# ---------------------------------------------------------------------------
# Adapter helpers
# ---------------------------------------------------------------------------


def _make_adapter(extra=None):
    config = PlatformConfig(
        enabled=True,
        extra={
            "url": "https://rc.example.com",
            "token": "pat",
            "user_id": "bot_uid",
            **(extra or {}),
        },
    )
    adapter = RocketchatAdapter(config)
    adapter._bot_username = "hermesbot"
    return adapter


def _make_event(chat_type: str) -> MessageEvent:
    return MessageEvent(
        text="hi",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform("rocketchat"),
            chat_id="room1",
            chat_type=chat_type,
            user_id="u1",
            user_name="alice",
        ),
        message_id="msg1",
    )


# ---------------------------------------------------------------------------
# Adapter init
# ---------------------------------------------------------------------------


class TestAdapterInit:
    def test_reads_url_from_extra(self):
        adapter = _make_adapter()
        assert adapter._base_url == "https://rc.example.com"

    def test_url_trailing_slash_stripped(self):
        adapter = _make_adapter({"url": "https://rc.example.com/"})
        assert adapter._base_url == "https://rc.example.com"

    def test_reads_credentials_from_extra(self):
        adapter = _make_adapter()
        assert adapter._token == "pat"
        assert adapter._bot_user_id == "bot_uid"

    def test_reads_credentials_from_env(self, monkeypatch):
        monkeypatch.setenv("ROCKETCHAT_URL", "https://env.example.com")
        monkeypatch.setenv("ROCKETCHAT_TOKEN", "env-pat")
        monkeypatch.setenv("ROCKETCHAT_USER_ID", "env_uid")
        adapter = RocketchatAdapter(PlatformConfig(enabled=True, extra={}))
        assert adapter._base_url == "https://env.example.com"
        assert adapter._token == "env-pat"
        assert adapter._bot_user_id == "env_uid"

    def test_platform_value(self):
        adapter = _make_adapter()
        assert adapter.platform.value == "rocketchat"

    def test_reply_mode_default_off(self):
        adapter = _make_adapter()
        assert adapter._reply_mode == "off"

    def test_reply_mode_from_extra(self):
        adapter = _make_adapter({"reply_mode": "thread"})
        assert adapter._reply_mode == "thread"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("is_reconnect", [False, True])
    async def test_connect_accepts_reconnect_flag(self, is_reconnect):
        adapter = RocketchatAdapter(PlatformConfig(enabled=True, extra={}))

        assert await adapter.connect(is_reconnect=is_reconnect) is False


# ---------------------------------------------------------------------------
# format_message
# ---------------------------------------------------------------------------


class TestFormatMessage:
    def setup_method(self):
        self.adapter = _make_adapter()

    def test_image_markdown_stripped_to_url(self):
        result = self.adapter.format_message("![alt](https://img.example.com/a.png)")
        assert result == "https://img.example.com/a.png"

    def test_media_directive_line_stripped(self):
        result = self.adapter.format_message("hello\nMEDIA: /tmp/x.png\nworld")
        assert "MEDIA" not in result
        assert "hello" in result and "world" in result

    def test_audio_as_voice_directive_stripped(self):
        result = self.adapter.format_message("[[audio_as_voice]]: /tmp/x.mp3\ndone")
        assert "audio_as_voice" not in result
        assert "done" in result

    def test_plain_text_unchanged(self):
        assert self.adapter.format_message("just text") == "just text"


# ---------------------------------------------------------------------------
# Reactions
# ---------------------------------------------------------------------------


class TestReactions:
    def setup_method(self):
        self.adapter = _make_adapter()

    def test_reactions_enabled_default(self):
        assert self.adapter._reactions_enabled() is True

    def test_reactions_disabled_via_env(self, monkeypatch):
        monkeypatch.setenv("ROCKETCHAT_REACTIONS", "false")
        assert self.adapter._reactions_enabled() is False

    @pytest.mark.asyncio
    async def test_on_processing_start_adds_eyes(self):
        self.adapter._add_reaction = AsyncMock(return_value=True)
        await self.adapter.on_processing_start(_make_event("channel"))
        self.adapter._add_reaction.assert_awaited_once_with("msg1", ":eyes:")

    @pytest.mark.asyncio
    async def test_on_processing_start_skips_when_disabled(self, monkeypatch):
        monkeypatch.setenv("ROCKETCHAT_REACTIONS", "false")
        self.adapter._add_reaction = AsyncMock(return_value=True)
        await self.adapter.on_processing_start(_make_event("channel"))
        self.adapter._add_reaction.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_on_processing_start_skips_without_message_id(self):
        self.adapter._add_reaction = AsyncMock(return_value=True)
        event = _make_event("channel")
        event.message_id = None
        await self.adapter.on_processing_start(event)
        self.adapter._add_reaction.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_on_processing_complete_success(self):
        self.adapter._add_reaction = AsyncMock(return_value=True)
        self.adapter._remove_reaction = AsyncMock(return_value=True)
        await self.adapter.on_processing_complete(
            _make_event("channel"), ProcessingOutcome.SUCCESS
        )
        self.adapter._remove_reaction.assert_awaited_once_with("msg1", ":eyes:")
        self.adapter._add_reaction.assert_awaited_once_with("msg1", ":white_check_mark:")

    @pytest.mark.asyncio
    async def test_on_processing_complete_failure(self):
        self.adapter._add_reaction = AsyncMock(return_value=True)
        self.adapter._remove_reaction = AsyncMock(return_value=True)
        await self.adapter.on_processing_complete(
            _make_event("channel"), ProcessingOutcome.FAILURE
        )
        self.adapter._add_reaction.assert_awaited_once_with("msg1", ":x:")


# ---------------------------------------------------------------------------
# send() / thread replies
# ---------------------------------------------------------------------------


class TestSend:
    def _adapter(self, reply_mode="off"):
        adapter = _make_adapter({"reply_mode": reply_mode})
        adapter._sync_title_to_rc_topic = AsyncMock()
        return adapter

    @pytest.mark.asyncio
    @pytest.mark.parametrize("room_type", ["channel", "group"])
    async def test_tmid_set_for_channel_or_group_in_thread_mode(self, room_type):
        adapter = self._adapter("thread")
        adapter._room_type_cache["room1"] = room_type
        posted = {}

        async def fake_post(path, payload):
            posted.update(payload)
            return {"success": True, "message": {"_id": "new_msg"}}

        adapter._api_post = fake_post
        result = await adapter.send("room1", "hello", reply_to="parent_msg")
        assert result.success is True
        assert result.message_id == "new_msg"
        assert posted["tmid"] == "parent_msg"

    @pytest.mark.asyncio
    async def test_tmid_not_set_for_dm_in_thread_mode(self):
        adapter = self._adapter("thread")
        adapter._room_type_cache["room1"] = "dm"
        posted = {}

        async def fake_post(path, payload):
            posted.update(payload)
            return {"success": True, "message": {"_id": "new_msg"}}

        adapter._api_post = fake_post
        await adapter.send("room1", "hello", reply_to="parent_msg")
        assert "tmid" not in posted

    @pytest.mark.asyncio
    async def test_existing_thread_root_from_metadata_wins(self):
        adapter = self._adapter("thread")
        adapter._room_type_cache["room1"] = "channel"
        posted = {}

        async def fake_post(path, payload):
            posted.update(payload)
            return {"success": True, "message": {"_id": "new_msg"}}

        adapter._api_post = fake_post
        await adapter.send(
            "room1",
            "hello",
            reply_to="child_msg",
            metadata={"thread_id": "root_msg"},
        )
        assert posted["tmid"] == "root_msg"

    @pytest.mark.asyncio
    async def test_clarify_prompt_uses_metadata_thread_root(self):
        adapter = self._adapter("thread")
        adapter._room_type_cache["room1"] = "channel"
        posted = {}

        async def fake_post(path, payload):
            posted.update(payload)
            return {"success": True, "message": {"_id": "clarify_msg"}}

        adapter._api_post = fake_post
        result = await adapter.send_clarify(
            chat_id="room1",
            question="When should I check?",
            choices=None,
            clarify_id="clarify-1",
            session_key="session-1",
            metadata={"thread_id": "root_msg"},
        )

        assert result.success is True
        assert posted["tmid"] == "root_msg"

    @pytest.mark.asyncio
    async def test_unknown_room_type_stays_flat(self):
        adapter = self._adapter("thread")
        adapter._api_get = AsyncMock(return_value={})
        posted = {}

        async def fake_post(path, payload):
            posted.update(payload)
            return {"success": True, "message": {"_id": "new_msg"}}

        adapter._api_post = fake_post
        await adapter.send("room1", "hello", reply_to="parent_msg")
        assert "tmid" not in posted
        assert "room1" not in adapter._room_type_cache

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("raw_type", "expected_type", "expected_tmid"),
        [
            ("d", "dm", None),
            ("c", "channel", "parent_msg"),
            ("p", "group", "parent_msg"),
        ],
    )
    async def test_uncached_room_type_is_resolved_before_threading(
        self, raw_type, expected_type, expected_tmid
    ):
        adapter = self._adapter("thread")
        adapter._api_get = AsyncMock(
            return_value={"room": {"t": raw_type}}
        )
        posted = {}

        async def fake_post(path, payload):
            posted.update(payload)
            return {"success": True, "message": {"_id": "new_msg"}}

        adapter._api_post = fake_post
        await adapter.send("room1", "hello", reply_to="parent_msg")

        assert adapter._room_type_cache["room1"] == expected_type
        if expected_tmid is None:
            assert "tmid" not in posted
        else:
            assert posted["tmid"] == expected_tmid

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("room_type", "expected_tmid"),
        [("dm", None), ("channel", "root_msg"), ("group", "root_msg")],
    )
    async def test_metadata_only_thread_target(
        self, room_type, expected_tmid
    ):
        adapter = self._adapter("thread")
        adapter._room_type_cache["room1"] = room_type
        posted = {}

        async def fake_post(path, payload):
            posted.update(payload)
            return {"success": True, "message": {"_id": "new_msg"}}

        adapter._api_post = fake_post
        await adapter.send(
            "room1", "hello", metadata={"thread_id": "root_msg"}
        )
        if expected_tmid is None:
            assert "tmid" not in posted
        else:
            assert posted["tmid"] == expected_tmid

    @pytest.mark.asyncio
    async def test_tmid_not_set_in_flat_mode(self):
        adapter = self._adapter("off")
        posted = {}

        async def fake_post(path, payload):
            posted.update(payload)
            return {"success": True, "message": {"_id": "new_msg"}}

        adapter._api_post = fake_post
        await adapter.send("room1", "hello", reply_to="parent_msg")
        assert "tmid" not in posted

    @pytest.mark.asyncio
    async def test_empty_content_short_circuits(self):
        adapter = self._adapter()
        adapter._api_post = AsyncMock()
        result = await adapter.send("room1", "")
        assert result.success is True
        adapter._api_post.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_suppresses_exact_home_channel_notice_when_enabled(
        self, monkeypatch
    ):
        monkeypatch.setenv(
            "ROCKETCHAT_SUPPRESS_HOME_CHANNEL_NOTICE", "true"
        )
        adapter = self._adapter()
        adapter._api_post = AsyncMock()
        notice = (
            "📬 No home channel is set for Rocketchat. "
            "A home channel is where Hermes delivers cron job results "
            "and cross-platform messages.\n\n"
            "Type /sethome to make this chat your home channel, "
            "or ignore to skip."
        )

        result = await adapter.send("room1", notice)

        assert result.success is True
        adapter._api_post.assert_not_awaited()
        adapter._sync_title_to_rc_topic.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_sends_home_channel_notice_by_default(self):
        adapter = self._adapter()
        adapter._api_post = AsyncMock(
            return_value={"success": True, "message": {"_id": "new_msg"}}
        )
        notice = (
            "📬 No home channel is set for Rocketchat. "
            "A home channel is where Hermes delivers cron job results "
            "and cross-platform messages.\n\n"
            "Type /sethome to make this chat your home channel, "
            "or ignore to skip."
        )

        result = await adapter.send("room1", notice)

        assert result.success is True
        adapter._api_post.assert_awaited_once()
        adapter._sync_title_to_rc_topic.assert_awaited_once_with("room1")

    @pytest.mark.asyncio
    async def test_does_not_suppress_similar_home_channel_text(
        self, monkeypatch
    ):
        monkeypatch.setenv(
            "ROCKETCHAT_SUPPRESS_HOME_CHANNEL_NOTICE", "true"
        )
        adapter = self._adapter()
        adapter._api_post = AsyncMock(
            return_value={"success": True, "message": {"_id": "new_msg"}}
        )
        similar_notice = (
            "📬 No home channel is set for Rocketchat. "
            "A home channel is where Hermes delivers cron job results "
            "and cross-platform messages.\n\n"
            "Type /sethome to make this chat your home channel, "
            "or ignore to skip. Extra context."
        )

        result = await adapter.send("room1", similar_notice)

        assert result.success is True
        adapter._api_post.assert_awaited_once()
        adapter._sync_title_to_rc_topic.assert_awaited_once_with("room1")

    @pytest.mark.asyncio
    async def test_failed_post_reported(self):
        adapter = self._adapter()
        adapter._api_post = AsyncMock(return_value={"success": False})
        result = await adapter.send("room1", "hello")
        assert result.success is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("room_type", "expected_tmid"),
        [("dm", None), ("channel", "root_msg"), ("group", "root_msg")],
    )
    async def test_media_confirm_uses_same_thread_policy(
        self, room_type, expected_tmid
    ):
        adapter = self._adapter("thread")
        adapter._room_type_cache["room1"] = room_type

        upload_response = MagicMock(status=200)
        upload_response.json = AsyncMock(
            return_value={"file": {"_id": "file1"}}
        )
        upload_context = MagicMock()
        upload_context.__aenter__ = AsyncMock(return_value=upload_response)
        upload_context.__aexit__ = AsyncMock(return_value=False)
        adapter._session = MagicMock()
        adapter._session.post.return_value = upload_context
        adapter._api_post = AsyncMock(
            return_value={"message": {"_id": "media_msg"}}
        )

        result = await adapter._upload_file(
            "room1",
            b"data",
            "file.txt",
            "text/plain",
            tmid="child_msg",
            metadata={"thread_id": "root_msg"},
        )

        assert result == "media_msg"
        confirm_payload = adapter._api_post.await_args.args[1]
        if expected_tmid is None:
            assert "tmid" not in confirm_payload
        else:
            assert confirm_payload["tmid"] == expected_tmid


# ---------------------------------------------------------------------------
# Room types & topic endpoints
# ---------------------------------------------------------------------------


class TestRoomTypes:
    @pytest.mark.asyncio
    async def test_dm_detected(self):
        adapter = _make_adapter()
        adapter._api_get = AsyncMock(return_value={"room": {"t": "d"}})
        assert await adapter._resolve_room_type("r1") == "dm"

    @pytest.mark.asyncio
    async def test_channel_detected(self):
        adapter = _make_adapter()
        adapter._api_get = AsyncMock(return_value={"room": {"t": "c"}})
        assert await adapter._resolve_room_type("r1") == "channel"

    @pytest.mark.asyncio
    async def test_private_group_detected(self):
        adapter = _make_adapter()
        adapter._api_get = AsyncMock(return_value={"room": {"t": "p"}})
        assert await adapter._resolve_room_type("r1") == "group"

    @pytest.mark.asyncio
    async def test_missing_room_falls_back_to_channel(self):
        adapter = _make_adapter()
        adapter._api_get = AsyncMock(return_value={})
        assert await adapter._resolve_room_type("r1") == "channel"
        assert "r1" not in adapter._room_type_cache

    @pytest.mark.asyncio
    async def test_get_chat_info_dm_name_from_other_user(self):
        adapter = _make_adapter()
        adapter._api_get = AsyncMock(
            return_value={"room": {"t": "d", "usernames": ["hermesbot", "alice"]}}
        )
        info = await adapter.get_chat_info("dm1")
        assert info == {"name": "alice", "type": "dm", "chat_id": "dm1"}
        assert adapter._room_type_cache["dm1"] == "dm"

    def test_set_topic_endpoint_mapping(self):
        assert RocketchatAdapter._set_topic_endpoint("dm") == "dm.setTopic"
        assert RocketchatAdapter._set_topic_endpoint("group") == "groups.setTopic"
        assert RocketchatAdapter._set_topic_endpoint("channel") == "channels.setTopic"
        assert RocketchatAdapter._set_topic_endpoint("weird") == "channels.setTopic"


# ---------------------------------------------------------------------------
# Inbound message handling
# ---------------------------------------------------------------------------


def _post(**overrides):
    post = {
        "_id": overrides.pop("post_id", "p1"),
        "rid": "room1",
        "msg": "hello",
        "u": {"_id": "u1", "username": "alice"},
    }
    post.update(overrides)
    return post


def _wired_adapter(room_type="dm"):
    adapter = _make_adapter()
    adapter.handle_message = AsyncMock()
    adapter._resolve_room_type = AsyncMock(return_value=room_type)
    adapter._download_attachments = AsyncMock(return_value=([], []))
    adapter._api_post = AsyncMock(return_value={"success": False})
    return adapter


class TestHandleMessage:
    @pytest.mark.asyncio
    async def test_own_message_ignored(self):
        adapter = _wired_adapter()
        await adapter._handle_message(_post(u={"_id": "bot_uid", "username": "hermesbot"}))
        adapter.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_duplicate_ignored(self):
        adapter = _wired_adapter()
        await adapter._handle_message(_post())
        await adapter._handle_message(_post())
        assert adapter.handle_message.await_count == 1

    @pytest.mark.asyncio
    async def test_system_message_skipped(self):
        adapter = _wired_adapter(room_type="channel")
        await adapter._handle_message(_post(t="uj"))
        adapter.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dm_dispatched_without_mention(self):
        adapter = _wired_adapter(room_type="dm")
        await adapter._handle_message(_post())
        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.await_args[0][0]
        assert event.text == "hello"
        assert event.source.chat_type == "dm"

    @pytest.mark.asyncio
    async def test_dm_prefers_display_name_over_username(self):
        adapter = _wired_adapter(room_type="dm")
        await adapter._handle_message(_post(
            u={"_id": "u-adam", "username": "marcin", "name": "Adam"},
        ))

        event = adapter.handle_message.await_args[0][0]
        assert event.source.chat_id == "room1"
        assert event.source.chat_name == "Adam"
        assert event.source.user_id == "u-adam"
        assert event.source.user_name == "Adam"
        assert build_session_key(event.source) == (
            "agent:main:rocketchat:dm:room1"
        )
        prompt = build_session_context_prompt(SessionContext(
            source=event.source,
            connected_platforms=[],
            home_channels={},
        ))
        assert "DM with Adam" in prompt
        assert '**User:** "Adam"' in prompt
        assert "marcin" not in prompt

    @pytest.mark.asyncio
    @pytest.mark.parametrize("display_name", [None, "", "   "])
    async def test_dm_falls_back_to_username_without_display_name(
        self, display_name
    ):
        adapter = _wired_adapter(room_type="dm")
        await adapter._handle_message(_post(
            u={
                "_id": "u-marcin",
                "username": "marcin",
                "name": display_name,
            },
        ))

        event = adapter.handle_message.await_args[0][0]
        assert event.source.user_name == "marcin"

    @pytest.mark.asyncio
    async def test_each_dm_uses_its_current_sender_identity(self):
        adapter = _wired_adapter(room_type="dm")
        await adapter._handle_message(_post(
            post_id="p-adam",
            rid="dm-adam",
            u={"_id": "u-adam", "username": "adam.login", "name": "Adam"},
        ))
        await adapter._handle_message(_post(
            post_id="p-marcin",
            rid="dm-marcin",
            u={
                "_id": "u-marcin",
                "username": "marcin.login",
                "name": "Marcin",
            },
        ))

        events = [call.args[0] for call in adapter.handle_message.await_args_list]
        assert [
            (event.source.chat_id, event.source.user_id, event.source.user_name)
            for event in events
        ] == [
            ("dm-adam", "u-adam", "Adam"),
            ("dm-marcin", "u-marcin", "Marcin"),
        ]

    @pytest.mark.asyncio
    async def test_channel_without_mention_gated(self):
        adapter = _wired_adapter(room_type="channel")
        await adapter._handle_message(_post())
        adapter.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_channel_with_mention_dispatched_and_stripped(self):
        adapter = _wired_adapter(room_type="channel")
        await adapter._handle_message(
            _post(
                msg="@hermesbot hello",
                mentions=[{"_id": "bot_uid", "username": "hermesbot"}],
            )
        )
        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.await_args[0][0]
        assert "@hermesbot" not in event.text
        assert "hello" in event.text

    @pytest.mark.asyncio
    @pytest.mark.parametrize("room_type", ["channel", "group"])
    async def test_top_level_mention_becomes_thread_session_root(
        self, room_type
    ):
        adapter = _wired_adapter(room_type=room_type)
        adapter._reply_mode = "thread"

        await adapter._handle_message(_post(
            post_id="root-message",
            msg="@hermesbot hello",
            mentions=[{"_id": "bot_uid", "username": "hermesbot"}],
        ))

        event = adapter.handle_message.await_args[0][0]
        assert event.source.thread_id == "root-message"

    @pytest.mark.asyncio
    async def test_top_level_channel_message_stays_flat_in_off_mode(self):
        adapter = _wired_adapter(room_type="channel")

        await adapter._handle_message(_post(
            post_id="root-message",
            msg="@hermesbot hello",
            mentions=[{"_id": "bot_uid", "username": "hermesbot"}],
        ))

        event = adapter.handle_message.await_args[0][0]
        assert event.source.thread_id is None

    @pytest.mark.asyncio
    async def test_top_level_dm_stays_flat_in_thread_mode(self):
        adapter = _wired_adapter(room_type="dm")
        adapter._reply_mode = "thread"

        await adapter._handle_message(_post(post_id="dm-message"))

        event = adapter.handle_message.await_args[0][0]
        assert event.source.thread_id is None

    @pytest.mark.asyncio
    async def test_unmentioned_reply_in_active_thread_is_dispatched(self):
        adapter = _wired_adapter(room_type="channel")
        adapter._has_active_session_for_thread = MagicMock(return_value=True)
        adapter._fetch_thread_context = AsyncMock()

        await adapter._handle_message(_post(msg="2", tmid="root-message"))

        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.await_args[0][0]
        assert event.text == "2"
        assert event.source.thread_id == "root-message"
        adapter._fetch_thread_context.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_root_and_unmentioned_reply_share_session_key(self):
        adapter = _wired_adapter(room_type="channel")
        adapter._reply_mode = "thread"

        await adapter._handle_message(_post(
            post_id="root-message",
            msg="@hermesbot choose a time",
            mentions=[{"_id": "bot_uid", "username": "hermesbot"}],
        ))
        root_event = adapter.handle_message.await_args[0][0]
        root_session_key = build_session_key(
            root_event.source,
            group_sessions_per_user=True,
            thread_sessions_per_user=False,
        )

        adapter._session_store = MagicMock()
        adapter._session_store.config.group_sessions_per_user = True
        adapter._session_store.config.thread_sessions_per_user = False
        adapter._session_store._entries = {root_session_key: object()}
        adapter._fetch_thread_context = AsyncMock()
        adapter.handle_message.reset_mock()

        await adapter._handle_message(_post(
            post_id="thread-reply",
            msg="2",
            tmid="root-message",
        ))

        reply_event = adapter.handle_message.await_args[0][0]
        reply_session_key = build_session_key(
            reply_event.source,
            group_sessions_per_user=True,
            thread_sessions_per_user=False,
        )
        assert reply_session_key == root_session_key
        adapter._fetch_thread_context.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unmentioned_reply_in_unknown_thread_is_gated(self):
        adapter = _wired_adapter(room_type="channel")
        adapter._has_active_session_for_thread = MagicMock(return_value=False)

        await adapter._handle_message(_post(
            msg="unrelated reply",
            tmid="someone-elses-thread",
        ))

        adapter.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rc_native_slash_command_routed_to_rc(self):
        adapter = _wired_adapter(room_type="dm")
        adapter._api_post = AsyncMock(return_value={"success": True})
        await adapter._handle_message(_post(msg="/giphy cat"))
        adapter._api_post.assert_awaited_once()
        assert adapter._api_post.await_args[0][0] == "commands.run"
        adapter.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_mid_sentence_slash_is_not_a_command(self):
        adapter = _wired_adapter(room_type="dm")
        await adapter._handle_message(_post(msg="ich find /status doof"))
        # No RC command routing attempted…
        for call in adapter._api_post.await_args_list:
            assert call[0][0] != "commands.run"
        # …and the message reaches the agent as plain text.
        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.await_args[0][0]
        assert event.message_type == MessageType.TEXT


# ---------------------------------------------------------------------------
# Thread context
# ---------------------------------------------------------------------------


class TestThreadContext:
    def test_no_session_store_means_no_session(self):
        adapter = _make_adapter()
        assert (
            adapter._has_active_session_for_thread("r1", "channel", "t1", "u1")
            is False
        )

    @pytest.mark.asyncio
    async def test_fetch_formats_parent_and_replies(self, monkeypatch):
        monkeypatch.setenv("ROCKETCHAT_ALLOWED_USERS", "u1")
        adapter = _make_adapter()

        async def fake_get(path, params=None):
            if path == "chat.getMessage":
                return {"message": {
                    "_id": "t1", "msg": "parent text", "ts": "1",
                    "u": {
                        "_id": "u1", "username": "alice.login", "name": "Alice",
                    },
                }}
            assert params["tmid"] == "t1"
            return {"messages": [
                {"_id": "m3", "msg": "@hermesbot help", "ts": "3",
                 "u": {"_id": "u1", "username": "alice"}},  # triggering message
                {"_id": "m2", "msg": "reply one", "ts": "2",
                 "u": {"_id": "u2", "username": "bob.login", "name": "Bob"}},
                {"_id": "mB", "msg": "own reply", "ts": "2.5",
                 "u": {"_id": "bot_uid", "username": "hermesbot"}},
            ]}

        adapter._api_get = fake_get
        ctx = await adapter._fetch_thread_context("t1", "m3")
        assert "[thread parent] Alice: parent text" in ctx
        assert "[unverified] Bob: reply one" in ctx
        assert "alice.login" not in ctx
        assert "bob.login" not in ctx
        assert "own reply" not in ctx  # bot's own replies skipped
        assert "help" not in ctx  # triggering message excluded
        assert ctx.endswith("\n\n")

    @pytest.mark.asyncio
    async def test_fetch_failure_returns_empty(self):
        adapter = _make_adapter()
        adapter._api_get = AsyncMock(side_effect=RuntimeError("boom"))
        assert await adapter._fetch_thread_context("t1", "m1") == ""

    @pytest.mark.asyncio
    async def test_injected_on_first_thread_turn(self):
        adapter = _wired_adapter(room_type="channel")
        adapter._has_active_session_for_thread = MagicMock(return_value=False)
        adapter._fetch_thread_context = AsyncMock(
            return_value="[Thread context]\nalice: hi\n\n"
        )
        await adapter._handle_message(_post(
            msg="@hermesbot summarize",
            tmid="t1",
            mentions=[{"_id": "bot_uid", "username": "hermesbot"}],
        ))
        event = adapter.handle_message.await_args[0][0]
        assert event.text.startswith("[Thread context]")
        assert event.text.rstrip().endswith("summarize")
        assert event.source.thread_id == "t1"

    @pytest.mark.asyncio
    async def test_not_injected_when_session_exists(self):
        adapter = _wired_adapter(room_type="channel")
        adapter._has_active_session_for_thread = MagicMock(return_value=True)
        adapter._fetch_thread_context = AsyncMock()
        await adapter._handle_message(_post(
            msg="@hermesbot hello",
            tmid="t1",
            mentions=[{"_id": "bot_uid", "username": "hermesbot"}],
        ))
        adapter._has_active_session_for_thread.assert_called_once()
        adapter._fetch_thread_context.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_not_fetched_outside_threads(self):
        adapter = _wired_adapter(room_type="dm")
        adapter._fetch_thread_context = AsyncMock()
        await adapter._handle_message(_post(msg="hello"))
        adapter._fetch_thread_context.assert_not_awaited()


# ---------------------------------------------------------------------------
# Agent tools
# ---------------------------------------------------------------------------

_tools = _rc.tools


class TestAgentTools:
    def test_tools_registered_into_platform_toolset(self):
        ctx = MagicMock()
        register(ctx)
        names = {c[1]["name"] for c in ctx.register_tool.call_args_list}
        assert names == {
            "rocketchat_list_channels",
            "rocketchat_create_channel",
            "rocketchat_post",
            "rocketchat_send_file",
            "rocketchat_dm",
        }
        assert {c[1]["toolset"] for c in ctx.register_tool.call_args_list} == {
            "rocketchat"
        }
        assert all(c[1]["is_async"] for c in ctx.register_tool.call_args_list)

    def test_manifest_lists_every_registered_tool(self):
        ctx = MagicMock()
        register(ctx)
        registered = {c[1]["name"] for c in ctx.register_tool.call_args_list}
        manifest_path = Path(__file__).resolve().parents[1] / "plugin.yaml"
        manifest = yaml.safe_load(manifest_path.read_text())
        assert set(manifest["provides_tools"]) == registered

    @pytest.mark.asyncio
    async def test_send_file_requires_regular_file_and_exactly_one_target(
        self, tmp_path
    ):
        missing = json.loads(await _tools.handle_send_file({}))
        assert "file_path is required" in missing["error"]

        directory = json.loads(await _tools.handle_send_file({
            "file_path": str(tmp_path),
            "room_id": "r1",
        }))
        assert "not a regular file" in directory["error"]

        file_path = tmp_path / "report.txt"
        file_path.write_text("hello")
        no_target = json.loads(await _tools.handle_send_file({
            "file_path": str(file_path),
        }))
        assert "Exactly one" in no_target["error"]

        conflicting = json.loads(await _tools.handle_send_file({
            "file_path": str(file_path),
            "room_id": "r1",
            "username": "zed",
        }))
        assert "Exactly one" in conflicting["error"]

    @pytest.mark.asyncio
    async def test_send_file_enforces_local_size_guard(self, tmp_path, monkeypatch):
        file_path = tmp_path / "large.bin"
        file_path.write_bytes(b"123")
        monkeypatch.setenv("ROCKETCHAT_AGENT_FILE_MAX_BYTES", "2")
        out = json.loads(await _tools.handle_send_file({
            "file_path": str(file_path),
            "room_id": "r1",
        }))
        assert "too large" in out["error"]
        assert "local limit is 2" in out["error"]

    @pytest.mark.asyncio
    async def test_send_file_by_room_id_with_caption_filename_and_thread(
        self, tmp_path, monkeypatch
    ):
        file_path = tmp_path / "source.bin"
        file_path.write_bytes(b"pdf-data")
        seen = {}

        async def fake_upload(room_id, file_data, filename, content_type):
            seen["upload"] = (room_id, file_data, filename, content_type)
            return {"file": {"_id": "f1"}}

        async def fake_api(method, path, **kw):
            seen["confirm"] = (method, path, kw.get("payload"))
            return {"message": {"_id": "m1"}}

        monkeypatch.setattr(_tools, "_upload_media", fake_upload)
        monkeypatch.setattr(_tools, "_api", fake_api)
        out = json.loads(await _tools.handle_send_file({
            "file_path": str(file_path),
            "room_id": "r7",
            "file_name": "report.pdf",
            "caption": "Sprint report",
            "tmid": "thread-root",
        }))

        assert seen["upload"] == (
            "r7", b"pdf-data", "report.pdf", "application/pdf"
        )
        assert seen["confirm"] == (
            "POST",
            "rooms.mediaConfirm/r7/f1",
            {"msg": "Sprint report", "tmid": "thread-root"},
        )
        assert out == {
            "sent": True,
            "target": "r7",
            "room_id": "r7",
            "message_id": "m1",
            "file": "report.pdf",
            "size": 8,
        }

    @pytest.mark.asyncio
    async def test_send_file_resolves_public_or_private_room_name(
        self, tmp_path, monkeypatch
    ):
        file_path = tmp_path / "artifact.unknown-extension"
        file_path.write_bytes(b"data")
        calls = []

        async def fake_upload(room_id, file_data, filename, content_type):
            calls.append(("upload", room_id, content_type))
            return {"file": {"_id": "f2"}}

        async def fake_api(method, path, **kw):
            calls.append((method, path, kw))
            if path == "rooms.info":
                assert kw["params"] == {"roomName": "private-reports"}
                return {"room": {"_id": "g9", "t": "p"}}
            return {"message": {"_id": "m2"}}

        monkeypatch.setattr(_tools, "_upload_media", fake_upload)
        monkeypatch.setattr(_tools, "_api", fake_api)
        out = json.loads(await _tools.handle_send_file({
            "file_path": str(file_path),
            "channel": "#private-reports",
        }))

        assert ("upload", "g9", "application/octet-stream") in calls
        assert out["target"] == "#private-reports"
        assert out["room_id"] == "g9"

    @pytest.mark.asyncio
    async def test_send_file_to_dm_accepts_username_case_insensitively(
        self, tmp_path, monkeypatch
    ):
        file_path = tmp_path / "report.txt"
        file_path.write_text("hello")
        uploaded_to = []

        async def fake_upload(room_id, file_data, filename, content_type):
            uploaded_to.append(room_id)
            return {"file": {"_id": "f3"}}

        async def fake_api(method, path, **kw):
            if path == "im.create":
                assert kw["payload"] == {"username": "zed"}
                return {
                    "room": {
                        "_id": "dm42",
                        "usernames": ["hermesbot", "Zed"],
                    }
                }
            return {"message": {"_id": "m3"}}

        monkeypatch.setattr(_tools, "_upload_media", fake_upload)
        monkeypatch.setattr(_tools, "_api", fake_api)
        out = json.loads(await _tools.handle_send_file({
            "file_path": str(file_path),
            "username": "@zed",
        }))

        assert uploaded_to == ["dm42"]
        assert out["target"] == "@zed"

    @pytest.mark.parametrize(
        "members",
        [["hermesbot"], ["hermesbot", "someone-else"]],
    )
    @pytest.mark.asyncio
    async def test_send_file_rejects_ghost_dm(
        self, members, tmp_path, monkeypatch
    ):
        file_path = tmp_path / "report.txt"
        file_path.write_text("hello")
        upload = AsyncMock()

        async def fake_api(method, path, **kw):
            return {"room": {"_id": "ghost", "usernames": members}}

        monkeypatch.setattr(_tools, "_upload_media", upload)
        monkeypatch.setattr(_tools, "_api", fake_api)
        out = json.loads(await _tools.handle_send_file({
            "file_path": str(file_path),
            "username": "zed",
        }))

        assert "no real recipient" in out["error"]
        upload.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_send_file_surfaces_upload_and_confirmation_failures(
        self, tmp_path, monkeypatch
    ):
        file_path = tmp_path / "report.txt"
        file_path.write_text("hello")

        async def upload_error(*args):
            return {"_error": "HTTP 413: too large"}

        monkeypatch.setattr(_tools, "_upload_media", upload_error)
        first = json.loads(await _tools.handle_send_file({
            "file_path": str(file_path),
            "room_id": "r1",
        }))
        assert "HTTP 413" in first["error"]

        async def upload_without_id(*args):
            return {"file": {}}

        monkeypatch.setattr(_tools, "_upload_media", upload_without_id)
        missing_file_id = json.loads(await _tools.handle_send_file({
            "file_path": str(file_path),
            "room_id": "r1",
        }))
        assert "no file id" in missing_file_id["error"]

        async def upload_ok(*args):
            return {"file": {"_id": "f1"}}

        async def confirm_error(method, path, **kw):
            return {"_error": "not-allowed"}

        monkeypatch.setattr(_tools, "_upload_media", upload_ok)
        monkeypatch.setattr(_tools, "_api", confirm_error)
        second = json.loads(await _tools.handle_send_file({
            "file_path": str(file_path),
            "room_id": "r1",
        }))
        assert "not-allowed" in second["error"]

        async def confirm_without_message_id(method, path, **kw):
            return {"message": {}}

        monkeypatch.setattr(_tools, "_api", confirm_without_message_id)
        missing_message_id = json.loads(await _tools.handle_send_file({
            "file_path": str(file_path),
            "room_id": "r1",
        }))
        assert "no message id" in missing_message_id["error"]

    @pytest.mark.asyncio
    async def test_upload_media_builds_authenticated_multipart(self, monkeypatch):
        import aiohttp

        form = MagicMock()
        captured = {}

        class FakeResponse:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def json(self, **kw):
                return {"file": {"_id": "f1"}, "success": True}

            async def text(self):
                return ""

        class FakeSession:
            def __init__(self, **kw):
                captured["session"] = kw

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            def post(self, url, **kw):
                captured["post"] = (url, kw)
                return FakeResponse()

        monkeypatch.setattr(aiohttp, "FormData", lambda: form)
        monkeypatch.setattr(aiohttp, "ClientSession", FakeSession)
        monkeypatch.setenv("ROCKETCHAT_URL", "https://rc.example.com/")
        monkeypatch.setenv("ROCKETCHAT_TOKEN", "token")
        monkeypatch.setenv("ROCKETCHAT_USER_ID", "bot-id")

        out = await _tools._upload_media(
            "r1", b"contents", "report.pdf", "application/pdf"
        )

        assert out["file"]["_id"] == "f1"
        form.add_field.assert_called_once_with(
            "file",
            b"contents",
            filename="report.pdf",
            content_type="application/pdf",
        )
        url, request = captured["post"]
        assert url == "https://rc.example.com/api/v1/rooms.media/r1"
        assert request["headers"] == {
            "X-Auth-Token": "token",
            "X-User-Id": "bot-id",
        }
        assert request["data"] is form

    @pytest.mark.asyncio
    async def test_dm_without_message_returns_room_id(self, monkeypatch):
        async def fake_api(method, path, **kw):
            assert (method, path) == ("POST", "im.create")
            assert kw["payload"] == {"username": "zed"}
            return {"room": {"_id": "dm42"}}

        monkeypatch.setattr(_tools, "_api", fake_api)
        out = json.loads(await _tools.handle_dm({"username": "@zed"}))
        assert out["room_id"] == "dm42"
        assert out["sent"] is False
        assert "rocketchat:dm42" in out["hint"]

    @pytest.mark.asyncio
    async def test_dm_with_message_sends(self, monkeypatch):
        calls = []

        async def fake_api(method, path, **kw):
            calls.append((path, kw.get("payload")))
            if path == "im.create":
                return {"room": {"_id": "dm42"}}
            return {"message": {"_id": "m9"}}

        monkeypatch.setattr(_tools, "_api", fake_api)
        out = json.loads(await _tools.handle_dm({"username": "zed", "message": "hi"}))
        assert out["sent"] is True
        assert out["message_id"] == "m9"
        assert ("chat.postMessage", {"roomId": "dm42", "text": "hi"}) in calls

    @pytest.mark.asyncio
    async def test_dm_requires_username(self):
        out = json.loads(await _tools.handle_dm({}))
        assert "error" in out

    @pytest.mark.asyncio
    async def test_post_by_channel_name_adds_hash(self, monkeypatch):
        seen = {}

        async def fake_api(method, path, **kw):
            seen["path"], seen["payload"] = path, kw.get("payload")
            return {"message": {"_id": "m1", "rid": "c9"}}

        monkeypatch.setattr(_tools, "_api", fake_api)
        out = json.loads(await _tools.handle_post(
            {"channel": "reports", "message": "summary"}
        ))
        assert seen["path"] == "chat.postMessage"
        assert seen["payload"] == {"text": "summary", "channel": "#reports"}
        assert out["sent"] is True
        assert out["room_id"] == "c9"

    @pytest.mark.asyncio
    async def test_post_by_room_id(self, monkeypatch):
        seen = {}

        async def fake_api(method, path, **kw):
            seen["payload"] = kw.get("payload")
            return {"message": {"_id": "m1", "rid": "r7"}}

        monkeypatch.setattr(_tools, "_api", fake_api)
        out = json.loads(await _tools.handle_post(
            {"room_id": "r7", "message": "hi"}
        ))
        assert seen["payload"] == {"text": "hi", "roomId": "r7"}
        assert out["message_id"] == "m1"

    @pytest.mark.asyncio
    async def test_post_requires_message_and_target(self):
        assert "error" in json.loads(await _tools.handle_post({"channel": "x"}))
        assert "error" in json.loads(await _tools.handle_post({"message": "x"}))

    @pytest.mark.asyncio
    async def test_post_surfaces_api_error(self, monkeypatch):
        async def fake_api(method, path, **kw):
            return {"_error": "not-allowed"}

        monkeypatch.setattr(_tools, "_api", fake_api)
        out = json.loads(await _tools.handle_post(
            {"channel": "reports", "message": "x"}
        ))
        assert "not-allowed" in out["error"]

    @pytest.mark.asyncio
    async def test_create_channel_private_uses_groups(self, monkeypatch):
        seen = {}

        async def fake_api(method, path, **kw):
            seen["path"], seen["payload"] = path, kw.get("payload")
            return {"group": {"_id": "g1", "name": "secret"}}

        monkeypatch.setattr(_tools, "_api", fake_api)
        out = json.loads(await _tools.handle_create_channel(
            {"name": "secret", "private": True, "members": ["@a", "b"]}
        ))
        assert seen["path"] == "groups.create"
        assert seen["payload"]["members"] == ["a", "b"]
        assert out["room_id"] == "g1"
        assert out["private"] is True

    @pytest.mark.asyncio
    async def test_create_channel_surfaces_permission_error(self, monkeypatch):
        async def fake_api(method, path, **kw):
            return {"_error": "unauthorized"}

        monkeypatch.setattr(_tools, "_api", fake_api)
        out = json.loads(await _tools.handle_create_channel({"name": "x"}))
        assert "unauthorized" in out["error"]

    @pytest.mark.asyncio
    async def test_list_channels_merges_and_filters(self, monkeypatch):
        async def fake_api(method, path, **kw):
            if path == "channels.list":
                return {"channels": [{"_id": "c1", "name": "general", "usersCount": 5}]}
            return {"groups": [{"_id": "g1", "name": "dev-private", "usersCount": 2}]}

        monkeypatch.setattr(_tools, "_api", fake_api)
        out = json.loads(await _tools.handle_list_channels({"filter": "dev"}))
        assert out["count"] == 1
        assert out["channels"][0]["room_id"] == "g1"
        assert out["channels"][0]["type"] == "group"

    @pytest.mark.asyncio
    async def test_list_channels_error_when_both_fail(self, monkeypatch):
        async def fake_api(method, path, **kw):
            return {"_error": "no permission"}

        monkeypatch.setattr(_tools, "_api", fake_api)
        out = json.loads(await _tools.handle_list_channels({}))
        assert "error" in out
