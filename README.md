# Rocket.Chat Plugin for Hermes Agent

Connects Hermes Agent to a self-hosted Rocket.Chat instance via REST API v1 (outbound) and DDP WebSocket (inbound). Ships as a standalone plugin — zero changes to Hermes core files, no extra Python dependencies (uses `aiohttp`, already shipped with Hermes).

---

## Installation

```bash
hermes plugins install HalfbitStudio/hermes-plugin-rocketchat
```

The installer clones this repo into `~/.hermes/plugins/rocketchat-platform/` and prompts you to enable it. If you skipped the prompt:

```bash
hermes plugins enable rocketchat-platform
hermes gateway restart
```

To update an existing installation:

```bash
hermes plugins update rocketchat-platform
hermes gateway restart
```

---

## Quick Start

### 1. Create a Bot on Rocket.Chat

1. Log into Rocket.Chat as admin
2. Go to **Admin** → **Users** → **New**
3. Set username to `hermes-bot`, role: `bot`
4. Save

### 2. Generate a Personal Access Token

1. Log in as the bot user
2. Go to **Account** → **Personal Access Tokens**
3. Give it a name (e.g. `hermes-gateway`)
4. **Check ☑ Ignore Two Factor Authentication** — this is critical
5. Copy the **Token** and **User ID** right away — you won't see them again

### 3. Configure

Either use the setup wizard:

```bash
hermes gateway setup
```

Select Rocket.Chat → enter URL, Token, and User ID when prompted.

Or configure manually in `~/.hermes/.env`:

```bash
ROCKETCHAT_URL=https://rc.example.com
ROCKETCHAT_TOKEN=your_pat_token
ROCKETCHAT_USER_ID=your_bot_user_id
ROCKETCHAT_ALLOWED_USERS=your_user_id
```

### 4. Restart the Gateway

```bash
systemctl restart hermes-gateway
# or via Telegram: /restart
```

---

## Configuring Rocket.Chat (server side)

Everything the bot needs on the Rocket.Chat side, beyond the Quick Start basics.

### Bot account

Create a dedicated user for the bot (**Admin → Users → New**):

- **Username**: e.g. `hermes-bot` — this is the name users will @mention
- **Roles**: add `bot` (keeps the bot out of "active users" counts and marks it visually)
- Uncheck **Require password change** and skip the welcome email
- Optionally enable **Join default channels** if the bot should sit in your standard rooms

### Personal Access Token

Log in **as the bot user** (not as admin):

1. **My Account → Personal Access Tokens**
2. Name it (e.g. `hermes-gateway`) and **check ☑ Ignore Two Factor Authentication** — without this, REST calls fail with `totp-required` on 2FA-enforced servers
3. Copy the **Token** and **User ID** immediately; they are shown only once

If PAT creation is blocked, enable it under **Admin → Settings → Accounts → "Allow Personal Access Tokens"**.

### Room membership

The bot only receives messages from rooms it is a **member** of (the DDP `__my_messages__` subscription covers exactly the bot's rooms). DMs work out of the box; for channels and private groups, invite it:

```
/invite @hermes-bot
```

In channels the bot answers only when @mentioned (unless the room ID is in `ROCKETCHAT_FREE_RESPONSE_CHANNELS` or you set `ROCKETCHAT_REQUIRE_MENTION=false`). With `ROCKETCHAT_REPLY_MODE=thread`, only the first message needs the mention: replies inside that active Hermes thread are picked up automatically, while unrelated threads remain ignored.

### Admin settings worth checking

| Setting | Where | Why |
|---------|-------|-----|
| `Message_AllowUnrecognizedSlashCommand` → **on** | Admin → Settings → Message | RC Desktop/Browser clients swallow unknown `/` commands client-side, so Hermes commands like `/new` or `/status` never reach the server. Mobile clients are unaffected. Alternatively set the env var `OVERWRITE_SETTING_Message_AllowUnrecognizedSlashCommand=true` on the RC server. |
| Rate Limiter | Admin → Settings → Rate Limiter | A busy bot can hit `429 Too Many Requests` on the REST API. Raise the API rate limits or exempt the bot's IP. |
| `Message_MaxAllowedSize` | Admin → Settings → Message | The adapter chunks long replies at 5000 characters (RC's default). If you lowered this setting below 5000, long messages will be rejected. |
| File Upload settings | Admin → Settings → File Upload | Agent file uploads follow the workspace's enabled/disabled state, MIME restrictions, maximum size, and the separate DM upload setting. Match the proxy body-size limit as well. |

### Permissions for topic sync (optional, default off)

Set `ROCKETCHAT_TOPIC_SYNC=true` to mirror Hermes session titles to room topics (`dm.setTopic` / `channels.setTopic` / `groups.setTopic`). Setting a channel topic requires room-edit rights — make the bot **owner/moderator of the room**, or grant the `edit-room` permission to the `bot` role (**Admin → Permissions**). Leave it disabled when topic writes are not part of the workflow.

### Reverse proxy (nginx / traefik)

The inbound stream is a long-lived WebSocket at `/websocket`. With nginx in front of RC, make sure it isn't killed by the default 60s read timeout:

```nginx
location /websocket {
    proxy_pass http://rocketchat;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 600s;
}
```

The adapter reconnects automatically (exponential backoff 2–60s), but a too-aggressive proxy timeout causes needless reconnect churn.

---

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `ROCKETCHAT_URL` | ✅ | — | Server URL (e.g. `https://rc.example.com`); HTTPS is required by default |
| `ROCKETCHAT_TOKEN` | ✅ | — | Personal Access Token (PAT) |
| `ROCKETCHAT_USER_ID` | ✅ | — | Bot user `_id` |
| `ROCKETCHAT_ALLOWED_USERS` | — | `""` | Comma-separated list of allowed user IDs |
| `ROCKETCHAT_ALLOW_ALL_USERS` | — | `false` | Allow all users (dev only) |
| `ROCKETCHAT_HOME_CHANNEL` | — | — | Room ID for cron / notification delivery |
| `ROCKETCHAT_SUPPRESS_HOME_CHANNEL_NOTICE` | — | `false` | Suppress the one-time `/sethome` notice when no home channel is configured |
| `ROCKETCHAT_REQUIRE_MENTION` | — | `true` | Require @mention to trigger in channels |
| `ROCKETCHAT_FREE_RESPONSE_CHANNELS` | — | — | Room IDs where @mention is not required |
| `ROCKETCHAT_REPLY_MODE` | — | `off` | `thread` keeps channel/group conversations, including clarification prompts, in one thread; `off` sends flat replies; DMs stay flat |
| `ROCKETCHAT_AGENT_FILE_MAX_BYTES` | — | `104857600` | Local upload-size guard (100 MiB); only the literal value `0` disables it, while invalid/negative values retain the default |
| `ROCKETCHAT_AGENT_FILE_UPLOADS` | — | `false` | Independently enable agent-triggered reads and uploads of local files |
| `ROCKETCHAT_AGENT_FILE_ALLOWED_ROOTS` | — | `""` | Absolute directories from which the agent may upload files; separate roots with `:` on Unix/macOS. Secure local upload currently requires POSIX descriptor APIs and stays unavailable on Windows. |
| `ROCKETCHAT_AGENT_FILE_MAX_CONCURRENCY` | — | `1` | Concurrent local file read/upload operations (accepted range 1–4) |
| `ROCKETCHAT_RETRIEVAL_ALLOWED_ROOMS` | — | `""` | Comma-separated room IDs eligible for cross-room reads; each request must also come from a trusted user |
| `ROCKETCHAT_RETRIEVAL_TRUSTED_USERS` | — | `""` | Comma-separated Rocket.Chat user IDs allowed to request a read outside the current room; the target must also be allowlisted |
| `ROCKETCHAT_RETRIEVAL_ALLOW_CONTEXTLESS` | — | `false` | Permit retrieval without a verified Rocket.Chat session context; high-risk compatibility escape hatch |
| `ROCKETCHAT_AGENT_WRITE_TOOLS` | — | `false` | Enable agent-callable channel creation, posting, file upload, and DM tools |
| `ROCKETCHAT_AGENT_WRITE_ALLOWED_ROOMS` | — | `""` | Exact room IDs eligible for cross-room writes; the requester must also be trusted |
| `ROCKETCHAT_AGENT_WRITE_TRUSTED_USERS` | — | `""` | Exact user IDs allowed to perform privileged actions and cross-room writes |
| `ROCKETCHAT_AGENT_TOOLS_ALLOW_EXTERNAL` | — | `false` | Permit enabled write tools to be called from a non-Rocket.Chat or contextless session |
| `ROCKETCHAT_ALLOW_INSECURE_HTTP` | — | `false` | Permit a plain HTTP server URL; only suitable for an isolated trusted network |
| `ROCKETCHAT_AGENT_RESPONSE_MAX_BYTES` | — | `2097152` | Maximum JSON body for every Rocket.Chat REST response (2 MiB; accepted range 64 KiB–16 MiB) |
| `ROCKETCHAT_AGENT_MAX_CONCURRENCY` | — | `4` | Maximum concurrent agent REST calls in this process |
| `ROCKETCHAT_AGENT_REQUESTS_PER_MINUTE` | — | `120` | Per-process request budget for agent REST calls |
| `ROCKETCHAT_RETRIEVAL_REDACT_SECRETS` | — | `true` | Best-effort redaction of common token, credential, and private-key patterns in retrieved text |
| `ROCKETCHAT_RETRIEVAL_INCLUDE_FILE_URLS` | — | `false` | Include attachment URLs in retrieval records |
| `ROCKETCHAT_RETRIEVAL_INCLUDE_REACTION_IDENTITIES` | — | `false` | Include usernames associated with reactions |
| `ROCKETCHAT_RETRIEVAL_INCLUDE_USER_IDS` | — | `false` | Include stable Rocket.Chat user IDs in normalized sender records |
| `ROCKETCHAT_RETRIEVAL_MAX_RESULT_CHARS` | — | `75000` | Maximum serialized retrieval result (accepted range 4,096–500,000 chars); whole records/text are truncated safely and the result is marked `truncated` |
| `ROCKETCHAT_THREAD_CONTEXT_MAX_CHARS` | — | `20000` | Maximum untrusted thread-history context added to one inbound turn (4,096–100,000 chars) |
| `ROCKETCHAT_MEDIA_DOWNLOAD_MAX_BYTES` | — | `104857600` | Total inbound attachment budget per event and outbound URL-media body limit; cannot exceed the 1 GiB hard cap |
| `ROCKETCHAT_FORWARDED_SLASH_COMMANDS` | — | `""` | Exact RC-native slash commands trusted writers may forward through `commands.run`; wildcards are rejected |
| `ROCKETCHAT_TOPIC_SYNC` | — | `false` | Enable Hermes-title and trusted `/title` writes to Rocket.Chat room topics |

Setting `ROCKETCHAT_SUPPRESS_HOME_CHANNEL_NOTICE=true` only hides the onboarding
notice. It does not configure a delivery target or change cron routing.

### Secure agent-tool defaults

Rocket.Chat authorizes API calls as the bot account, not as the human who asked
Hermes to call a tool. The plugin therefore applies a second authorization layer
before room data is returned to the agent (and before directly room-targeted
search/history calls reach Rocket.Chat):

- A retrieval call from a verified Rocket.Chat conversation may read only that
  conversation's current room by default. Thread and permalink calls authorize
  the supplied `room_id` (or current session room) before looking up an opaque
  `tmid`/`message_id`, then reject the response unless its `_id` and `rid`
  match. A cross-room call must therefore include its expected `room_id`.
- A cross-room read succeeds only when **both** the target room appears in
  `ROCKETCHAT_RETRIEVAL_ALLOWED_ROOMS` **and** the requesting user's stable ID
  appears in `ROCKETCHAT_RETRIEVAL_TRUSTED_USERS`. Setting only one list grants
  nothing.
- Retrieval from a contextless-style runtime (`cli`, `local`, `cron`, or an empty
  platform value) is rejected unless
  `ROCKETCHAT_RETRIEVAL_ALLOW_CONTEXTLESS=true`; even then, the resolved room
  must be an exact member of `ROCKETCHAT_RETRIEVAL_ALLOWED_ROOMS`. Wildcards
  are never accepted. Read calls from every other named non-Rocket.Chat
  platform are denied.
- Mutating tools are a separate capability and remain disabled until
  `ROCKETCHAT_AGENT_WRITE_TOOLS=true`. They still require a Rocket.Chat runtime
  context with non-empty task-local room and requester IDs. Current-room posts
  are allowed after that opt-in; cross-room writes require both an exact
  `ROCKETCHAT_AGENT_WRITE_ALLOWED_ROOMS` match and the requester in
  `ROCKETCHAT_AGENT_WRITE_TRUSTED_USERS`. Channel creation, DM creation, name
  resolution, and host-file access always require a trusted requester.
  Contextless or non-Rocket.Chat writes additionally require
  `ROCKETCHAT_AGENT_TOOLS_ALLOW_EXTERNAL=true`. Enabling reads does not silently
  enable channel creation, posting, uploads, or DMs.
- Local file upload is a third, narrower capability. `rocketchat_send_file`
  remains unavailable until write tools, `ROCKETCHAT_AGENT_FILE_UPLOADS=true`,
  and at least one absolute `ROCKETCHAT_AGENT_FILE_ALLOWED_ROOTS` entry are all
  configured. Requested paths must be below a configured root and cannot use
  traversal or symlinks. The same checks apply to model-emitted `MEDIA:` local
  paths, so that delivery path cannot bypass the file-upload capability.
- RC-native slash forwarding and topic writes are independent, default-off
  capabilities. Forwarding requires an exact command in
  `ROCKETCHAT_FORWARDED_SLASH_COMMANDS`, write tools enabled, and an exact
  trusted requester; `ROCKETCHAT_TOPIC_SYNC=true` is additionally required for
  topic changes.

Authorization uses Hermes' task-local session context. Legacy process-global
`HERMES_SESSION_*` variables are deliberately ignored, so stale values from a
different request cannot grant room or requester authority.

For example, this permits Alice to research `GENERAL` from a different
Rocket.Chat room, while giving no such authority to other users and leaving all
write tools disabled:

```bash
ROCKETCHAT_RETRIEVAL_ALLOWED_ROOMS=GENERAL
ROCKETCHAT_RETRIEVAL_TRUSTED_USERS=aliceRocketChatUserId
ROCKETCHAT_AGENT_WRITE_TOOLS=false
ROCKETCHAT_AGENT_TOOLS_ALLOW_EXTERNAL=false
```

The defaults also omit file URLs, reaction identities, and stable sender IDs;
apply heuristic secret redaction; enforce a serialized result budget; reject
oversized API bodies; and bound request rate and concurrency. The adapter does
not follow REST redirects, so the PAT cannot be forwarded to a redirect target.
Plain HTTP is rejected unless `ROCKETCHAT_ALLOW_INSECURE_HTTP=true`.

Retrieved messages are marked as untrusted content. Secret redaction and prompt
framing reduce accidental disclosure and instruction-following, but they cannot
prove that stored chat text is safe: a message can still contain a convincing
prompt injection. Keep write tools disabled for research-only deployments,
require human review for consequential actions, and never treat retrieved text
as policy or tool instructions.

Use a dedicated bot account with the minimum Rocket.Chat permissions and room
memberships required for the workflow; never use an administrator PAT. Protect
the PAT as a secret, rotate it, keep the bot out of rooms it does not need, and
monitor authorization denials, throttling, and unusual retrieval volume in the
gateway logs. Application allowlists complement Rocket.Chat membership—they do
not replace least privilege at the server.

---

## Features

| Feature | Status |
|---------|--------|
| DDP WebSocket (inbound) | ✅ `__my_messages__` subscription |
| REST API (outbound) | ✅ `chat.postMessage` |
| DM sender identity | ✅ Rocket.Chat display name with username/ID fallbacks |
| File upload | ✅ Gateway media sends and agent-triggered uploads via `rooms.media` + `rooms.mediaConfirm` |
| Attachment download | ✅ With image/audio/document cache |
| Thread support | ✅ Clarifications and follow-ups stay under the channel/group root via `tmid`; DMs stay flat |
| Mention gating | ✅ Configurable per room |
| Typing indicator | ✅ Rocket.Chat 8.x compatible |
| Reconnect | ✅ Exponential backoff (2s–60s) |
| Voice message → STT | ✅ ffmpeg MP3 conversion pipeline |
| Emoji reactions | ✅ 👀✅❌ on channel messages |
| Topic sync | ✅ Bidirectional (Hermes session title ↔ RC room topic) |
| Slash command routing | ✅ Position-0 only, gated via `is_gateway_known_command()` |
| Deferred attachments | ✅ File-only uploads merged with next text message |
| Cron delivery | ✅ Standalone REST-only sender |
| Setup wizard | ✅ `hermes gateway setup` |
| Plugin discovery | ✅ Auto-discover via `kind: platform` |
| Thread context | ✅ First mention in a thread pulls in prior thread messages |
| Agent tools | ✅ Nine tools for room management, posting, uploads, DMs, search, history, threads, and permalinks (see below) |

---

## Agent Tools

The plugin registers nine Rocket.Chat tools, divided into read and write
capabilities. `rocketchat_list_channels` and the four retrieval tools are read
tools. `rocketchat_create_channel`, `rocketchat_post`, `rocketchat_send_file`,
and `rocketchat_dm` are write tools and fail closed unless
`ROCKETCHAT_AGENT_WRITE_TOOLS=true`. File upload additionally requires
`ROCKETCHAT_AGENT_FILE_UPLOADS=true` and an allowed root. Every call is also checked against the
runtime platform/session policy described under
[Secure agent-tool defaults](#secure-agent-tool-defaults).

| Tool | Business use case | Key parameters and pagination | Bot permission needed |
|------|-------------------|-------------------------------|-----------------------|
| `rocketchat_list_channels` | Discover authorized retrieval targets | Optional `filter`; returns only the current room plus cross-room IDs authorized by the room/user policy | `view-c-room` for public channels; private groups only where the bot is a member |
| `rocketchat_create_channel` | Create a project, incident, or private working room | `name`; optional `private`, `members` | `create-c` / `create-p` |
| `rocketchat_post` | Publish a result or hand-off to another room | `message` plus `channel` or exact `room_id` | bot must be a room member and able to post |
| `rocketchat_send_file` | Deliver a report, export, or generated artifact | `file_path` plus exactly one of `room_id`, `username`, `channel`; optional `caption`, `file_name`, `tmid` | bot must be able to post and upload files in the target room |
| `rocketchat_dm` | Open a private workflow or schedule a direct reminder | `username`; optional `message` | bot must be allowed to create/open DMs with the user |
| `rocketchat_search_messages` | Find decisions, incidents, owners, or prior discussion inside one known room | `room_id`, `query`; `count` defaults to 25 (valid 1–100); `offset` defaults to 0 and must be non-negative | bot must be a room member with access to search/read its messages |
| `rocketchat_get_history` | Summarize or audit a bounded slice of one room's timeline | `room_id`; `count` defaults to 50 (valid 1–100); `offset` defaults to 0 and must be non-negative; optional `oldest`, `latest`, `inclusive`; `include_threads` defaults to false | bot must be a room member with access to its history |
| `rocketchat_get_thread` | Reconstruct a discussion before summarizing or acting on it | `tmid`; optional expected `room_id` (required cross-room/contextless); `limit` defaults to 100 (valid 1–500) | bot must be able to read the parent message and its room |
| `rocketchat_get_permalink` | Produce a stable link for an audit trail, ticket, or hand-off | `message_id`; optional expected `room_id` (required cross-room/contextless) | bot must be able to read the message and room metadata |

Combined with the built-in `cronjob` tool this enables natural flows like:

> *"hey, remind @zed about the deploy tomorrow at 9 — in a DM"*

The agent opens @zed's DM room via `rocketchat_dm` (which returns the `room_id`) and schedules a cron job with `deliver="rocketchat:<room_id>"`, so the reminder lands in the DM even if the gateway restarted in between.

> *"research this thread and post the summary to #reports"*

Thread context gives the agent the discussion, and `rocketchat_post` delivers the result to a different room than the one the conversation is happening in (Hermes deliberately ships no generic agent-callable `send_message` — cross-room posting on Rocket.Chat goes through this tool).

> *"send `/home/hermes/reports/sprint-28.pdf` to #reports and add the caption 'Sprint 28 report'"*

`rocketchat_send_file` accepts an absolute `file_path` on the machine running Hermes and exactly one target: a channel/private-group name, an exact `room_id`, or a real Rocket.Chat `username` (login, not display name). It can also set a displayed `file_name`, attach a `caption`, and post under a thread root using `tmid`. For DMs it opens or reuses the conversation and rejects an unresolved username before uploading. For scheduled or multi-step workflows, use the literal `room_id` returned by `rocketchat_dm` or `rocketchat_list_channels`; never derive one from a name.

The tool reads a file that is already present on the Hermes host, but only from
canonical paths below `ROCKETCHAT_AGENT_FILE_ALLOWED_ROOTS`; traversal and
symlinked paths are rejected. Enable it explicitly with
`ROCKETCHAT_AGENT_FILE_UPLOADS=true`. A 100 MiB local guard is enabled by
default through `ROCKETCHAT_AGENT_FILE_MAX_BYTES` (only literal `0` disables
it), and one file operation runs at a time unless
`ROCKETCHAT_AGENT_FILE_MAX_CONCURRENCY` is raised. Rocket.Chat and the reverse
proxy can enforce lower limits.

### Read-only retrieval examples

Search is intentionally scoped to one literal room ID; it is not a workspace-wide
discovery API:

```json
{"room_id": "GENERAL", "query": "deployment decision", "count": 25, "offset": 0}
```

Fetch a bounded history window, optionally including thread replies and using
Rocket.Chat timestamps for the lower and upper bounds:

```json
{"room_id": "GENERAL", "count": 50, "offset": 0, "oldest": "2026-07-01T00:00:00.000Z", "latest": "2026-07-21T23:59:59.999Z", "inclusive": true, "include_threads": false}
```

For public channels, private groups, and DMs, `include_threads` maps to
Rocket.Chat's `showThreadMessages` option. The plugin always sends the explicit
boolean because Rocket.Chat's endpoint defaults differ by room type.

Fetch a thread by its root message ID. `rocketchat_get_thread` retrieves the
parent separately, then returns it with a bounded set of replies in chronological
order:

```json
{"tmid": "threadRootMessageId", "limit": 100}
```

Build a permalink from a message ID without asking the agent to guess a room
name or type:

```json
{"message_id": "messageId"}
```

`rocketchat_get_permalink` resolves the message with `chat.getMessage`, looks up
its room with `rooms.info`, and URL-encodes every path component. Rocket.Chat
room type `c` uses `/channel/<room.name>?msg=<message-id>`, type `p` uses
`/group/<room.name>?msg=<message-id>`, and type `d` uses
`/direct/<rid>?msg=<message-id>`.

All retrieval tools return compact normalized message records rather than raw
Rocket.Chat payloads. Each result is identified as untrusted and obeys the
privacy opt-ins and `ROCKETCHAT_RETRIEVAL_MAX_RESULT_CHARS` budget. Increase
`offset` to request another search/history page; the server may return fewer
records than requested. Rocket.Chat releases differ in whether search/history
responses include total pagination metadata, so `total` is `null` whenever the
server omits it. Out-of-range counts/limits and negative offsets are rejected
rather than silently clamped. The plugin also enforces local slicing, bounded
thread pagination, and a no-progress stop so a misbehaving server cannot turn a
bounded request into an infinite or oversized result.

### Thread context

When the bot is mentioned in a thread it hasn't participated in yet, it fetches the earlier thread messages (thread parent + replies via `chat.getThreadMessages`) and injects them as context — so "@bot, summarize this thread" just works. Injection happens only on the bot's first turn in the thread; after that the session history carries the conversation. Messages from users not on `ROCKETCHAT_ALLOWED_USERS` are tagged `[unverified]` and framed as background information, not instructions.

In `ROCKETCHAT_REPLY_MODE=thread`, a top-level message that addresses the bot becomes the conversation's thread root immediately. Clarification questions are posted under that root, and follow-up replies in the active Hermes thread do not need another @mention. A message in a different or unknown thread still requires an explicit mention.

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `totp-required` | PAT was created without "Ignore Two Factor" — generate a new one with the checkbox checked |
| "Failed to authenticate" | Verify with: `curl -H "X-Auth-Token: TOKEN" -H "X-User-Id: ID" https://rc/api/v1/me` |
| Bot doesn't respond | Make sure the bot has been invited to the channel and check `ROCKETCHAT_ALLOWED_USERS` |
| WebSocket keeps disconnecting | Set `proxy_read_timeout 600s` in nginx; also check your Mongo Replica Set status |
| Rate-limited (429) | Tune the Rocket.Chat rate limiter for the bot's IP |
| Unrecognized slash commands on desktop | RC Desktop intercepts unknown `/` commands client-side. Set `Message_AllowUnrecognizedSlashCommand=true` in RC Admin (Settings → Message) or via env: `OVERWRITE_SETTING_Message_AllowUnrecognizedSlashCommand=true` |

---

## Verification

Once configured, `hermes status` should show:

```
Rocket.Chat 🚀 ✓ configured (plugin)
```

Send a DM to the bot in Rocket.Chat to test the connection end-to-end.

---

## Architecture

```
Rocket.Chat ←── REST /api/v1/chat.postMessage ──→ Hermes Agent
           ←── DDP WebSocket stream-room-messages ──→ (inbound)
```

- **Auth:** Personal Access Token (works for both REST and DDP)
- **Room detection:** `rooms.info` + lazy cache
- **System messages:** Filtered out by the `t` field (join/leave/role changes, etc.)
- **Desktop note:** RC Desktop/Browser may intercept unknown `/` commands. Mobile clients work out of the box.

---

## Development

Tests need a hermes-agent checkout (the adapter imports `gateway.*` at runtime):

```bash
git clone https://github.com/NousResearch/hermes-agent
python -m pip install -e ./hermes-agent pytest pytest-asyncio aiohttp
HERMES_AGENT_PATH=./hermes-agent python -m pytest tests/ -v
```

Installing the checkout is required because importing `gateway.*` also imports
Hermes' runtime dependencies. `HERMES_AGENT_PATH` only selects the source tree
and defaults to `../hermes-agent` when unset.

---

## Credits

- Original Rocket.Chat adapter: [hermes-agent#4637](https://github.com/NousResearch/hermes-agent/pull/4637) by [@meron1122](https://github.com/meron1122) and [hermes-agent#14869](https://github.com/NousResearch/hermes-agent/pull/14869) by @cyb0rgk1tty
- Extended plugin version (topic sync, slash commands, voice pipeline, reconnect): [hermes-agent#30463](https://github.com/NousResearch/hermes-agent/pull/30463) by [@HearthCore](https://github.com/HearthCore)
- Agent-callable file uploads: [hermes-plugin-rocketchat#1](https://github.com/HalfbitStudio/hermes-plugin-rocketchat/pull/1) by [@YounesAmalou](https://github.com/YounesAmalou)

Published as a standalone repo per the [hermes-agent plugin policy](https://github.com/NousResearch/hermes-agent/blob/main/CONTRIBUTING.md) — third-party integrations ship as external plugins.

MIT licensed, same as hermes-agent.
