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

from gateway.config import Platform, PlatformConfig
from gateway.platform_registry import PlatformEntry, platform_registry
from gateway.platforms.base import MessageEvent, MessageType, ProcessingOutcome
from gateway.session import SessionSource

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
    async def test_tmid_set_in_thread_mode(self):
        adapter = self._adapter("thread")
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
    async def test_failed_post_reported(self):
        adapter = self._adapter()
        adapter._api_post = AsyncMock(return_value={"success": False})
        result = await adapter.send("room1", "hello")
        assert result.success is False


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
                    "u": {"_id": "u1", "username": "alice"},
                }}
            assert params["tmid"] == "t1"
            return {"messages": [
                {"_id": "m3", "msg": "@hermesbot help", "ts": "3",
                 "u": {"_id": "u1", "username": "alice"}},  # triggering message
                {"_id": "m2", "msg": "reply one", "ts": "2",
                 "u": {"_id": "u2", "username": "bob"}},
                {"_id": "mB", "msg": "own reply", "ts": "2.5",
                 "u": {"_id": "bot_uid", "username": "hermesbot"}},
            ]}

        adapter._api_get = fake_get
        ctx = await adapter._fetch_thread_context("t1", "m3")
        assert "[thread parent] alice: parent text" in ctx
        assert "[unverified] bob: reply one" in ctx
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
        adapter = _wired_adapter(room_type="dm")
        adapter._has_active_session_for_thread = MagicMock(return_value=True)
        adapter._fetch_thread_context = AsyncMock()
        await adapter._handle_message(_post(msg="hello", tmid="t1"))
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
            "rocketchat_dm",
        }
        assert {c[1]["toolset"] for c in ctx.register_tool.call_args_list} == {
            "rocketchat"
        }
        assert all(c[1]["is_async"] for c in ctx.register_tool.call_args_list)

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
