# Changelog

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

[1.2.0]: https://github.com/HalfbitStudio/hermes-plugin-rocketchat/compare/v1.1.1...v1.2.0
