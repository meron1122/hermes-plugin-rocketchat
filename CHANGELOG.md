# Changelog

## [1.3.0] - 2026-07-21

### Added

- Four read-only agent tools: `rocketchat_search_messages`,
  `rocketchat_get_history`, `rocketchat_get_thread`, and
  `rocketchat_get_permalink`.
- Exact-`room_id` message search and bounded room-history retrieval with
  pagination and optional time-window filters.
- Compact normalized thread output with the root message fetched separately
  from its replies.
- Stable, URL-encoded message permalinks for public channels, private groups,
  and direct-message rooms.

### Security

- Retrieval now fails closed to the verified Rocket.Chat session's current
  room. Cross-room reads require both an exact room allowlist match and an exact
  trusted-requester match; resolved thread and permalink rooms receive the same
  check.
- Contextless retrieval is disabled by default and, when explicitly enabled,
  remains limited to exact allowlisted room IDs. Retrieval from other named
  platforms is rejected.
- Agent write tools are disabled by default and separated from read tools.
  Contextless or cross-platform writes require a second explicit opt-in.
- Local file upload has an independent default-off capability, canonical
  allowed-root policy, traversal/symlink rejection, exact two-member DM target
  verification, and a separate concurrency bound held through confirmation.
- Tool authorization now consumes task-local Hermes session provenance only;
  stale process-global `HERMES_SESSION_*` values cannot grant access.
- HTTPS is required by default, environment proxies and API redirects are
  ignored, and every JSON REST response has a pre-decode body limit. Agent REST
  calls additionally have concurrency and per-minute request budgets.
- Inbound authorization and pre-dispatch hooks now complete before slash/topic
  writes, attachment downloads, thread fetches, or audio conversion. Native
  slash forwarding and topic sync are exact-allowlisted/default-off capabilities.
- Network media has streaming byte limits and connection-time public-DNS
  enforcement; local `MEDIA:` delivery uses the same opt-in, trusted-user,
  allowed-root, descriptor, and size checks as the explicit upload tool.
- Retrieval results are marked untrusted, bounded locally, guarded against
  non-progressing pagination, and use privacy-preserving normalized records.
  File URLs, reaction identities, and stable user IDs require explicit opt-ins.
- Common credential patterns are redacted by default. This is a best-effort
  defense and does not eliminate stored prompt-injection risk.

### Changed

- Deployments that use `rocketchat_send_file` must now set all of
  `ROCKETCHAT_AGENT_WRITE_TOOLS=true`,
  `ROCKETCHAT_AGENT_FILE_UPLOADS=true`, and
  `ROCKETCHAT_AGENT_FILE_ALLOWED_ROOTS` to one or more absolute directories.
  Separate roots with `:` on Unix/macOS. Secure local upload requires POSIX
  descriptor APIs and is unavailable on Windows.
- Cross-room writes now require both `ROCKETCHAT_AGENT_WRITE_ALLOWED_ROOMS`
  and `ROCKETCHAT_AGENT_WRITE_TRUSTED_USERS`. RC-native command forwarding also
  requires `ROCKETCHAT_FORWARDED_SLASH_COMMANDS`; topic sync requires
  `ROCKETCHAT_TOPIC_SYNC=true`.

### Documentation

- Expanded the agent-tool reference from five to nine tools with business use
  cases, required permissions, parameters, pagination limits, and examples.
- Added secure-default deployment guidance covering room/requester scoping,
  read/write separation, least-privilege bot accounts, HTTPS, audit monitoring,
  data minimization, and residual prompt-injection risk.

## [1.2.0] - 2026-07-20

### Added

- Agent-callable `rocketchat_send_file` for local files.
- Channel/private-group targeting by name, exact `room_id` targeting, and DM
  targeting by real Rocket.Chat username.
- Optional captions, displayed filename overrides, automatic MIME detection,
  and thread placement through `tmid`.
- A configurable 100 MiB local safety guard through
  `ROCKETCHAT_AGENT_FILE_MAX_BYTES`.

### Changed

- File targets must be unambiguous: exactly one of `channel`, `room_id`, or
  `username` is accepted.
- Invalid or one-member DM targets are rejected before file bytes are uploaded.
- Local reads reject non-regular files and run outside the gateway event loop.
- Private groups can be resolved by name through the common `rooms.info` API.

### Documentation

- Documented installation updates, server upload settings, tool arguments,
  security considerations, and the two-step Rocket.Chat media flow.

Thanks to [@YounesAmalou](https://github.com/YounesAmalou) for the original
implementation in [PR #1](https://github.com/HalfbitStudio/hermes-plugin-rocketchat/pull/1).

[1.3.0]: https://github.com/HalfbitStudio/hermes-plugin-rocketchat/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/HalfbitStudio/hermes-plugin-rocketchat/compare/v1.1.1...v1.2.0
