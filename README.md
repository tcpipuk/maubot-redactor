# Redactor Maubot Plugin for Matrix

`maubot-redactor` is a [Maubot](https://github.com/maubot/maubot) plugin for Matrix that helps
automate moderation by redacting messages from users who are banned for specific reasons.

## Features

- **Ban Monitoring**: Watches for user ban events within Matrix rooms.
- **Reason Matching**: Checks if the ban reason matches configured patterns for specific user MXIDs.
- **Message Redaction**: Automatically redacts a configurable number of recent messages from the
  banned user upon a match.
- **Configurable Ban List**: Define which MXIDs and ban reason patterns should trigger redactions.
- **Error Reporting**: Option to report processing errors to a moderation room.
- **Redaction Reporting**: Option to report successful redactions to a moderation room.

## Quick Start

1. **Install the plugin** (choose one method):
    - Download from [releases](https://github.com/tcpipuk/maubot-redactor/releases)
    - Build from source:

        ```bash
        git clone https://github.com/tcpipuk/maubot-redactor
        cd maubot-redactor
        zip -r maubot-redactor.mbp redactor/ maubot.yaml base-config.yaml
        ```

2. **Upload and Configure**:
    - Upload the `.mbp` file through the Maubot admin interface.
    - Configure the settings (see Configuration section below).
    - Enable the plugin instance in the desired rooms.

## Configuration Guide

Edit settings in the Maubot admin interface or directly in the instance's configuration file
(derived from `base-config.yaml`):

```yaml
# Central reporting room for errors and/or successful redactions
report_to_room: "!moderationroom:example.org"

# Configuration for reporting actions
reporting:
  # Report successful redactions to the report_to_room
  report_redactions: true
  # Report processing errors to the report_to_room
  post_errors: true

# Number of recent messages to check and potentially redact for a banned user
redact_limit: 10

# List of users and ban reason patterns to act upon.
# 'mxid' is the full MXID of the moderator performing the ban.
# 'reason_pattern' is a regex string to match against the ban reason.
ban_list:
  - mxid: "@moderator1:example.org"
    reason_pattern: "^Spam$"
  - mxid: "@admin:example.org"
    reason_pattern: "(?i)unwanted advertising" # Case-insensitive match
```

> **Tip**: Using room IDs (like `!room:server`) is more efficient than aliases (like `#room:server`)
> for the `report_to_room` setting.

## Usage Example

1. Configure the `ban_list` with the MXID of a moderator (`@moderator1:example.org`) and a specific
   reason pattern (`^Spam$`).
2. Set `redact_limit` to `5`.
3. Enable `report_redactions`.
4. User `@spammer:example.net` posts several messages in a room where the bot is active.
5. `@moderator1:example.org` bans `@spammer:example.net` from the room with the exact reason "Spam".
6. The bot detects the ban event.
7. It checks the ban list and finds a match for the moderator MXID and the reason pattern.
8. The bot redacts the last 5 messages sent by `@spammer:example.net` in that room.
9. If `report_to_room` is configured and `report_redactions` is true, the bot sends a message to the
   reporting room, for example:
   `Redacted 5 messages from @spammer:example.net in !targetroom:example.org due to ban by @moderator1:example.org (Reason: Spam)`

## Contributing

Contributions are welcome! Please open an issue or submit a pull request on GitHub.

## Licence

This project is licensed under the AGPLv3 Licence. See the [LICENCE](LICENCE) file for details.
