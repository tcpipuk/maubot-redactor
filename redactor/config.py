"""Configuration management for the Redactor plugin.

This module handles the plugin's configuration settings, which control its behaviour
in Matrix chat rooms. It uses Maubot's BaseProxyConfig to manage settings defined
in the base-config.yaml file.

Settings available under the 'redaction' section:
    max_messages (int | None):
        Maximum number of recent messages to check and potentially redact per user ban.
        Set to null or omit to disable the message count limit. Default: 50.
    max_age_hours (int | None):
        Maximum age of messages (in hours) to check and potentially redact.
        Only messages sent within this timeframe before the ban event will be considered.
        Set to null or omit to disable the time limit. Default: 2.
    mxids (list[str]):
        List of moderator MXIDs (full Matrix User IDs) whose bans should trigger redactions.
        Example: ["@moderator1:example.org", "@admin:example.org"]. Default: [].
    reasons (list[str]):
        List of regex patterns to match against the ban reason (case-insensitive).
        If a ban is issued by a moderator in 'mxids' AND the reason matches ANY pattern,
        redaction will occur. Example: ["^spam$", "unwanted advertising"]. Default: [].

Settings available under the 'reporting' section:
    room (str | None):
        Room ID where the bot should send reports (successful redactions, errors).
        Leave null or omit for no reporting. Example: "!room:server". Default: null.
    report_redactions (bool):
        Whether to report successful redactions to the reporting room. Default: true.
    post_errors (bool):
        Whether to report processing errors to the reporting room. Default: true.
    vias (list[str]):
        List of Matrix servers to use for constructing matrix.to links in reports.
        Example: ["matrix.org", "yourserver.org"]. Default: ["matrix.org"].
"""

from __future__ import annotations

from mautrix.util.config import BaseProxyConfig, ConfigUpdateHelper


class Config(BaseProxyConfig):
    """Configuration manager for the RedactorPlugin."""

    def do_update(self, helper: ConfigUpdateHelper) -> None:
        """Update the configuration with new values.

        This method is called when the user modifies the config.
        It copies values from the user-provided config into the base config.

        Args:
            helper: Helper object to copy configuration values.
        """
        # Copy main sections - this handles merging keys within these sections
        helper.copy("redaction")
        helper.copy("reporting")
