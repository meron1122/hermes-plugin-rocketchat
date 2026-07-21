# Rocket.Chat Platform Plugin — AI Agent Guide

Reference for AI coding assistants working on this plugin.

## Overview

A Hermes gateway platform adapter for self-hosted Rocket.Chat instances.
The transport, inbound routing, media, helpers, and agent tools are split into
focused modules and built on `aiohttp` (zero new dependencies).

**Architecture:** REST API v1 for outbound writes, DDP WebSocket for inbound receive.

## File Map

| File | Purpose |
|------|---------|
| `adapter.py` | Adapter composition, lifecycle, outbound text, reactions, and room metadata |
| `ddp.py` | DDP WebSocket transport and frame routing |
| `inbound.py` | Inbound parsing, mention/thread gating, and dispatch |
| `media.py` | Attachment download and two-step media upload pipeline |
| `helpers.py` | Configuration, requirements, formatting, and standalone cron sender |
| `tools.py` | Agent-callable room management, posting, upload, DM, and read-only retrieval tools |
| `setup_wizard.py` | Interactive Hermes gateway setup |
| `plugin.yaml` | Plugin manifest — env vars, discovery metadata |
| `__init__.py` | Exports `register()` for Hermes plugin discovery |
| `README.md` | User-facing setup and operations guide (English) |
| `CHANGELOG.md` | Release history and upgrade-facing behavior changes |
| `tests/test_adapter.py` | Unit and regression test suite |
| `AGENTS.md` | This file — AI agent development reference |

## Critical Design Decisions

### 1. Slash Command: Position 0 Only

The adapter scans the admitted (possibly hook-rewritten) `message_text` for `/`
— but **only at position 0**. Mid-sentence `/status` is NOT a command.

```python
# CORRECT — only position 0
slash_pos = candidate_text.find("/")
if slash_pos == 0:
    # ... parse command
```

Historical note: the original PR#14869 matched `slash_pos >= 0 and (slash_pos == 0 or candidate_text[slash_pos - 1] in (" ", "\t", "\n"))` which caused false positives. Fixed in `433b7a15d`.

### 2. DM Command Normalization Before Admission

In DMs, the @mention is sometimes NOT stripped from `raw_msg` (varies by RC version).
Strip an explicit bot prefix before the pre-dispatch hook only when the remainder
starts with `/`. After admission, use only the hook-approved/rewritten text for
slash routing and topic writes; never resurrect the raw pre-hook message.

### 3. Room Type Detection

Uses `GET /api/v1/rooms.info` with a per-room cache. Rocket.Chat returns `c`, `p`,
`d`; the adapter caches normalized `channel`, `group`, `dm` values. Failed lookups
fall back to `channel` for inbound gating but are not cached, so outbound threading
fails flat until the room type is positively known.

### 4. TTS Audio Pipeline

Voice messages arrive as WebM/OGG attachments via RC. The adapter:
1. Downloads the attachment via `_download_attachments()`
2. Converts to MP3 via `ffmpeg` (`_convert_audio_to_mp3()`)
3. Delivers the MP3 path to Hermes for STT processing

RC's `rooms.media` has no direct audio transcoding, so ffmpeg is required.

### 5. DDP Protocol

- Connect: WebSocket to `wss://<server>/websocket`
- Auth: `{"msg": "connect", "version": "1", "support": ["1", "pre2", "pre1"]}`
  → `resume` with PAT token
- Subscribe: `{"msg": "sub", "name": "stream-room-messages", "params": ["__my_messages__", {}]}`
- System messages filtered by `"t"` field (join/leave/role changes, etc.)
- Reconnect: exponential backoff 2s–60s

### 6. Bidirectional Topic Sync Is Default Off

With `ROCKETCHAT_TOPIC_SYNC=true`, Hermes session titles sync back to RC room topics via `dm.setTopic` (DMs) or
`groups.setTopic`/`channels.setTopic` for group rooms. In `_sync_title_to_rc_topic()`.

Power-on self-topic: On connect, the adapter sets the room topic to
"🤖 Hermes Agent — connected at <timestamp>" to confirm connectivity.

### 7. Async Standalone Sender (Cron)

`_standalone_send()` is a REST-only sender used by Hermes cron delivery — no
WebSocket dependency. Instantiates its own `aiohttp.ClientSession`, sends via
`chat.postMessage`, cleans up. No adapter lifecycle needed.

### 8. RC Admin: Forward Unrecognized Slash Commands

Rocket.Chat Desktop/Browser intercepts unknown `/` commands client-side, so the
message never reaches Hermes. Mobile clients are unaffected.

**Fix:** `Message_AllowUnrecognizedSlashCommand = true` in RC Admin
(Administration → Workspace → Settings → Message)

**Environment alternative:** `OVERWRITE_SETTING_Message_AllowUnrecognizedSlashCommand=true`

Only Rocket.Chat administrators with `edit-privileged-setting` can change it.

### 9. Sender Identity Uses the Display Name

Rocket.Chat message objects may carry both a login (`u.username`) and the
human-facing name shown in the UI (`u.name`). Set `SessionSource.user_name`
using `u.name → u.username → u._id`; otherwise Hermes can address a DM user by
an unrelated login. Authorization and session isolation continue to use the
stable `u._id`.

### 10. DM Replies Are Always Flat

`ROCKETCHAT_REPLY_MODE=thread` applies only to channels and private groups.
Bot replies in direct messages never receive `tmid`, including text and media
replies. Existing user-created DM threads retain their own Hermes sessions and
context, but the bot's answer is delivered to the main DM timeline.

Use `_thread_target_for_reply()` for every interactive outbound path. It also
prefers `metadata["thread_id"]` over `reply_to`, because Hermes carries an
existing thread's root in metadata while `reply_to` may be a child message ID.

### 11. Thread Mode Uses the Root as the Session ID

For a top-level channel/group message in `ROCKETCHAT_REPLY_MODE=thread`, expose
the message's own `_id` as `SessionSource.thread_id`. This keeps the initial
turn, final reply, clarification prompts, and subsequent replies on one Hermes
session key and one Rocket.Chat `tmid` root.

Keep the physical inbound `post.tmid` separate from this logical conversation
thread ID. Only a physical thread reply should trigger history fetching or be
passed to `commands.run`. A channel message without an @mention may bypass the
mention gate only when its physical `tmid` maps to an existing Hermes session;
never exempt every thread globally.

### 12. Agent File Uploads Use Exact Targets

`rocketchat_send_file` is a REST-only, agent-callable two-step upload:

1. `POST rooms.media/{room_id}` uploads the bytes.
2. `POST rooms.mediaConfirm/{room_id}/{file_id}` creates the message and carries
   the optional caption and `tmid` thread root.

Require exactly one target: a literal `room_id`, a real Rocket.Chat `username`,
or a channel/private-group name resolved with `rooms.info`. For username targets,
`im.create` must return a DM containing both the bot and the requested username;
otherwise reject the send to avoid one-member ghost rooms. Compare usernames
case-insensitively, but never infer a room ID from a display name.

The tool requires all three grants: `ROCKETCHAT_AGENT_WRITE_TOOLS=true`,
`ROCKETCHAT_AGENT_FILE_UPLOADS=true`, and one or more absolute directories in
`ROCKETCHAT_AGENT_FILE_ALLOWED_ROOTS` (`:` separator on Unix/macOS). Secure
local-file delivery requires POSIX descriptor APIs and is unavailable on Windows.
Requested paths must remain below a configured root and cannot use
traversal or symlinks. Only regular files are accepted. Reads run outside the
event loop, and `ROCKETCHAT_AGENT_FILE_MAX_BYTES` provides a local guard
(100 MiB by default; only literal `0` disables it) before Rocket.Chat and proxy
limits are applied. Hold the separate file-operation semaphore across the read,
upload, and confirmation; `ROCKETCHAT_AGENT_FILE_MAX_CONCURRENCY` defaults to 1
and accepts 1–4.

### 13. Read-Only Retrieval Is Explicitly Scoped and Bounded

Retrieval tools return compact normalized records, not raw Rocket.Chat payloads.
`rocketchat_search_messages` and `rocketchat_get_history` require an exact
`room_id`; never expand them into unbounded workspace-wide reads. Search and
history accept 1–100 records, while thread retrieval accepts 1–500 replies;
reject values outside those ranges rather than silently clamping them. History
defaults `include_threads` to false. It maps to `showThreadMessages` for channels,
private groups, and DMs. Always send the explicit true/false value: Rocket.Chat's
history endpoints do not all share the same default.

`rocketchat_get_thread` accepts the exact root `tmid`. Fetch its parent separately
with `chat.getMessage`, because `chat.getThreadMessages` returns replies, then
normalize the parent and replies into one chronological result. Permalinks accept
only `message_id`, resolve the message and room server-side, and URL-encode every
dynamic path/query component. Route public channels as `channel/<room.name>`,
private groups as `group/<room.name>`, and DMs as `direct/<rid>`.

### 14. Agent Tools Fail Closed at a Second Authorization Layer

Rocket.Chat evaluates REST calls as the PAT owner, so server-side bot membership
does not prove that the human requester may read the same data. Preserve the
plugin's application authorization boundary:

- A verified `rocketchat` runtime may retrieve from its exact task-local
  `HERMES_SESSION_CHAT_ID` without an additional allowlist entry.
- A different room is readable only when its exact ID is in
  `ROCKETCHAT_RETRIEVAL_ALLOWED_ROOMS` **and** the requester's exact
  `HERMES_SESSION_USER_ID` is in `ROCKETCHAT_RETRIEVAL_TRUSTED_USERS`.
- For thread and permalink tools, authorize the supplied `room_id` (or the
  current session room) **before** looking up the opaque `tmid`/`message_id`,
  then require the returned `_id` and `rid` to match. Cross-room and
  contextless calls must therefore provide an explicit expected `room_id`.
- Contextless-style `cli`/`local`/`cron`/empty-platform retrieval is disabled unless
  `ROCKETCHAT_RETRIEVAL_ALLOW_CONTEXTLESS=true`, and even then the resolved room
  must be explicitly allowlisted. Do not implement wildcard room access.
- Retrieval calls from every other named platform remain denied. Write tools
  (`create_channel`, `post`, `send_file`, `dm`) require
  `ROCKETCHAT_AGENT_WRITE_TOOLS=true`; a Rocket.Chat write context must include
  task-local room and requester IDs. Non-Rocket.Chat or contextless writes
  additionally require `ROCKETCHAT_AGENT_TOOLS_ALLOW_EXTERNAL=true`.

Read authorization only from Hermes' task-local `ContextVar` provenance. Never
fall back to process-global `HERMES_SESSION_*` values: they may be stale or
belong to another concurrent request. An unavailable or partial task context
must fail closed. File upload additionally requires its independent opt-in and
configured allowed roots.

Keep the remaining defenses independent of authorization. Require HTTPS unless
`ROCKETCHAT_ALLOW_INSECURE_HTTP=true`, reject redirects, cap response bodies
with `ROCKETCHAT_AGENT_RESPONSE_MAX_BYTES` (2 MiB default, applied to every JSON
REST response), and bound agent REST
traffic with `ROCKETCHAT_AGENT_MAX_CONCURRENCY` (4) and
`ROCKETCHAT_AGENT_REQUESTS_PER_MINUTE` (120). Search/history results must be
locally sliced even if Rocket.Chat ignores `count`; thread pagination needs a
page bound and a no-progress break. Serialized retrieval output is limited by
`ROCKETCHAT_RETRIEVAL_MAX_RESULT_CHARS` (75,000 default; valid range
4,096–500,000). Truncate whole records/text without producing invalid JSON and
set `truncated` in the result.

Normalized retrieval records minimize identity data by default. File URLs,
reaction usernames, and stable sender IDs are opt-ins through
`ROCKETCHAT_RETRIEVAL_INCLUDE_FILE_URLS`,
`ROCKETCHAT_RETRIEVAL_INCLUDE_REACTION_IDENTITIES`, and
`ROCKETCHAT_RETRIEVAL_INCLUDE_USER_IDS`. Keep
`ROCKETCHAT_RETRIEVAL_REDACT_SECRETS=true`; redaction is heuristic and must not
be presented as a complete DLP control. Retrieved messages remain untrusted
content and can contain stored prompt injection. Do not remove the untrusted
result marker or conflate read authorization with permission to invoke writes.

## Known Pitfalls

| Pitfall | Detail | Mitigation |
|---------|--------|------------|
| `totp-required` | PAT without "Ignore Two Factor" generates TOTP challenge | User must re-create PAT with checkbox |
| DDP subscription lost on reconnect | RC does NOT resume DDP subs across reconnects | Full re-login + re-sub in `_ws_loop()` |
| Image URLs truncated | RC has a ~2KB URL limit in messages | `_send_url_as_file()` uploads as file attachment |
| Room type ambiguity | `rooms.info` can fail for archived rooms | Inbound falls back to `channel` without caching; outbound threading fails flat |
| ffmpeg not installed | Audio processing breaks silently | `_convert_audio_to_mp3()` returns None, logs warning |
| Nginx close WS on 60s idle | Default proxy timeout kills long connections | Set `proxy_read_timeout 600s` |
| `Message_AllowUnrecognizedSlashCommand` | Desktop browser shows "invalid command" error | RC admin setting required (not an adapter fix) |
| File upload target ambiguity | Multiple target fields could upload to one room but report another | `rocketchat_send_file` rejects calls unless exactly one target is set |
| Agent upload memory pressure | Local files are buffered before upload | Size guard plus a separate 1-by-default file-operation semaphore held across read/upload/confirm |
| Local file exfiltration | A write-enabled agent can name sensitive host paths | Require independent file-upload opt-in, canonical allowed roots, and reject traversal/symlinks |
| DM ghost rooms | An invalid username can yield a one-member DM | Require an exact two-member DM with matching `usernames`/`uids`, including the bot and requested login |
| Bot PAT is a confused deputy | Rocket.Chat checks the bot's access, not the requester's | Enforce current-room scope or the room+trusted-user cross-room conjunction before every read |
| Stored prompt injection | Retrieved chat text can contain instructions aimed at the model | Mark results untrusted, redact likely secrets, keep writes disabled by default, and require review for consequential actions |
| Retrieval data leakage | File URLs, reaction identities, and stable user IDs expose extra metadata | Keep all three privacy opt-ins false unless the workflow requires them |
| Runaway agent REST calls | Large bodies, concurrency, or broken pagination can exhaust resources | Enforce body/result budgets, local slicing, page/no-progress guards, concurrency, and per-minute limits |
| PAT exposed in transit or redirects | HTTP and redirects can send credentials outside the intended origin | Require HTTPS by default and never follow agent REST redirects |

## Tools & Functions Reference

**Transport:**
- `adapter.py`: `connect()`, `disconnect()`
- `ddp.py`: `_ws_loop()`, `_ws_connect_and_listen()`, `_handle_ddp_frame()`

**Send:**
- `send(chat_id, text, msg_id)`, `send_image()`, `send_image_file()`, `send_document()`,
  `send_voice()`, `send_video()`, `send_typing()`, `stop_typing()`

**Receive:**
- `inbound.py`: `_handle_message(post)`, thread history and deferred attachments

**Media:**
- `inbound.py`: `_download_attachments()`, `_convert_audio_to_mp3()`
- `media.py`: `_upload_file()` and outbound media send helpers

**Agent tools:**
- `tools.py`: `handle_list_channels()`, `handle_create_channel()`, `handle_post()`,
  `handle_send_file()`, `handle_dm()`, `handle_search_messages()`,
  `handle_get_history()`, `handle_get_thread()`, `handle_get_permalink()`

**Reactions:**
- `_add_reaction(msg_id, emoji)`, `_remove_reaction(msg_id, emoji)` — 👀✅❌

**Meta:**
- `edit_message(chat_id, msg_id, text)`, `get_chat_info()`
- `_sync_title_to_rc_topic()`, `_resolve_room_type()`
- `adapter.py`: `format_message(content)` — Rocket.Chat-specific Markdown
- `helpers.py`: `check_requirements()`, `validate_config()`, `_env_enablement()`,
  `_standalone_send()`

## PR History

This plugin is a refactor of **PR #14869** (`@cyb0rgk1tty`, `gateway/platforms/` core adapter)
into the modern Hermes plugin format (`plugins/platforms/`, `kind: platform`).
Parallel independent work: **PR #4637** (`@meron1122`, same plugin structure).

**Key commits (local):**
| SHA | Change |
|-----|--------|
| `ce4852bb3` | Initial port from PR#14869 → plugin format |
| `7103c75ea` | TTS audio pipeline (ffmpeg MP3 conversion) |
| `84ddeb401` | RC-native slash command routing |
| Various | Debug logging, reaction fixes, topic sync |
| `433b7a15d` | **/status mid-sentence fix** (position 0 only) |
| `f0bf51e` | Merge PR #1: agent-callable local file uploads |

## Testing

Tests live in `tests/test_adapter.py` and need a hermes-agent checkout
(the adapter imports `gateway.*`):

```bash
git clone https://github.com/NousResearch/hermes-agent
python -m pip install -e ./hermes-agent pytest pytest-asyncio aiohttp
HERMES_AGENT_PATH=./hermes-agent python -m pytest tests/ -q
```

The editable install is required: `HERMES_AGENT_PATH` adds the checkout to
`sys.path`, but does not install Hermes runtime dependencies such as `requests`.

Live test: DM the bot or @mention in a channel after config.
