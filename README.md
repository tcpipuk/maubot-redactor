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
(derived from `base-config.yaml`).

```yaml
# Settings related to message redaction
redaction:
  # Maximum number of recent messages to check and potentially redact per user ban.
  # Set to null or omit to disable the message count limit.
  max_messages: 50

  # Maximum age of messages (in hours) to check and potentially redact.
  # Only messages sent within this timeframe before the ban event will be considered.
  # Set to null or omit to disable the time limit.
  max_age_hours: 24 # e.g., 24 hours = 1 day

  # List of moderator MXIDs (full Matrix User IDs) whose bans should trigger redactions.
  # Example: ["@moderator1:example.org", "@admin:example.org"]
  mxids: []

  # List of regex patterns to match against the ban reason.
  # If a ban is issued by a moderator listed in 'mxids' AND the reason matches
  # ANY pattern in this list, redaction will occur.
  # Matching is ALWAYS case-insensitive.
  # Example: ["^spam$", "unwanted advertising", "rule violation \d+"]
  reasons: []

# Settings related to reporting bot actions
reporting:
  # Room ID where the bot should send reports (successful redactions, errors).
  # Leave null or omit for no reporting. Use Room ID (e.g., "!room:server") for efficiency.
  room: null

  # Whether to report successful redactions to the reporting room.
  report_redactions: true

  # Whether to report processing errors to the reporting room.
  post_errors: true

  # Optional: List of Matrix servers to use for constructing matrix.to links in reports.
  # Helps ensure links are accessible.
  # Example: ["matrix.org", "yourserver.org"]
  vias:
    - matrix.org

```

> **Tip**: Using room IDs (like `!room:server`) is generally more reliable and efficient than aliases
> (like `#room:server`) for the `reporting.room` setting.

## Usage Example

1. Configure `redaction.mxids` with the moderators to monitor (e.g., `@moderator1:example.org`).
2. Configure `redaction.reasons` with reason patterns (e.g., `spam`, `unwanted advertising`).
   Note that `spam` will match "Spam", "spam", "SPAM", etc. due to case-insensitivity.
3. Set redaction limits, e.g., `redaction.max_messages: 5`, `redaction.max_age_hours: 48`.
4. Configure `reporting.room` to your moderation room ID and set `reporting.report_redactions: true`.
5. User `@spammer:example.net` posts several messages in a room where the bot is active over the
   last 3 days.
6. `@moderator1:example.org` (listed in `redaction.mxids`) bans `@spammer:example.net` with the
   reason "Spam".
7. The bot detects the ban, checks `redaction.mxids`, and confirms the moderator matches.
8. It checks the reason "Spam" against `redaction.reasons`. It finds a match with the pattern `spam`
   (case-insensitive).
9. The bot retrieves recent messages from `@spammer:example.net`. It only considers messages sent
   within the last 48 hours (`max_age_hours`). From that subset, it redacts up to the 5 most recent
   ones (`max_messages`). Let's say 4 messages meet these criteria.
10. The bot redacts those 4 messages.
11. The bot sends a report to the `reporting.room`, for example:
    `Redacted 4 messages from @spammer:example.net in !targetroom:example.org due to ban by @moderator1:example.org (reason: Spam)`

## Contributing

Contributions are welcome! Please open an issue or submit a pull request on GitHub.

## Licence

This project is licensed under the AGPLv3 Licence. See the [LICENCE](LICENCE) file for details.
