"""Provides the BasePlugin class for core Maubot functionality.

Handles configuration loading/validation, Matrix client setup, and utility
functions like cached room alias resolution, used by the main RedactorPlugin.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from maubot import Plugin
from mautrix.types import RoomAlias, RoomID

from .config import Config
from .utils import lru_cache_with_ttl

if TYPE_CHECKING:
    from mautrix.util.config import BaseProxyConfig
    from mautrix.util.logging import TraceLogger


class BasePlugin(Plugin):
    """Base Maubot plugin class providing shared config and startup logic."""

    config: Config
    log: TraceLogger

    @classmethod
    def get_config_class(cls) -> type[BaseProxyConfig]:
        """Get the configuration class for this plugin.

        Returns:
            The Config class for this plugin.
        """
        return Config

    @lru_cache_with_ttl(maxsize=100, ttl=3600)
    async def resolve_room_alias(self, room_alias: str) -> str:
        """Resolve a room alias (e.g. #room:server) to a room ID (!id:server).

        Uses an LRU cache with a 1-hour TTL to avoid repeated lookups.
        Returns the original input if it already looks like a room ID, is not a
        string, does not start with '#', or if resolution fails.

        Args:
            room_alias: The room alias string to resolve.

        Returns:
            The resolved RoomID string, or the original input on failure/invalid input.
        """
        if not isinstance(room_alias, str) or not room_alias:
            self.log.debug("resolve_room_alias called with invalid input: %s", room_alias)
            return room_alias

        if room_alias.startswith("!"):
            return room_alias

        if not room_alias.startswith("#"):
            self.log.warning("resolve_room_alias called with unexpected format: %s", room_alias)
            return room_alias

        try:
            resolved = await self.client.resolve_room_alias(RoomAlias(room_alias))
            if resolved and resolved.room_id:
                self.log.debug("Resolved room alias %s to %s", room_alias, resolved.room_id)
                return resolved.room_id
            self.log.warning("Resolving room alias %s did not return a valid room ID.", room_alias)
        except Exception:
            self.log.exception("Failed to resolve room alias %s", room_alias)
            return room_alias
        else:
            return room_alias

    async def _send_startup_report(self, message: str, level: str = "info") -> None:
        """Send a message to the configured reporting room during startup.

        Resolves the `reporting.room` alias if necessary. Logs errors if the room cannot
        be resolved or the message cannot be sent. Does nothing if `reporting.room` is unset.

        Args:
            message: The message content to send.
            level: The log level associated with the message (e.g., "info", "error").
        """
        report_room_id = self.config["reporting.room"]

        if not report_room_id:
            self.log.info("Reporting room not configured. Startup message: %s", message)
            return

        try:
            resolved_report_room = await self.resolve_room_alias(report_room_id)
            if not resolved_report_room.startswith("!"):
                self.log.error(
                    "Failed to resolve reporting room alias '%s' to an ID for startup message.",
                    report_room_id,
                )
                return

            await self.client.send_text(RoomID(resolved_report_room), message)
            self.log.info("Sent startup %s report to %s.", level, resolved_report_room)
        except Exception:
            self.log.exception(
                "CRITICAL: Failed to send startup %s report to %s", level, report_room_id
            )

    async def _validate_config_patterns(self) -> list[str]:
        """Validate regex patterns in `config["redaction.reasons"]`.

        Attempts to compile each pattern string. Logs exceptions for invalid patterns
        or non-string types found in the list.

        Returns:
            A list of the invalid pattern strings found.
        """
        invalid_patterns = []
        reason_patterns: list[str] = self.config["redaction.reasons"]
        if not reason_patterns:
            self.log.debug("No redaction reason patterns configured.")
            return []

        self.log.debug("Validating %d reason patterns...", len(reason_patterns))
        for pattern in reason_patterns:
            try:
                re.compile(pattern)
                self.log.debug("Pattern '%s' is valid.", pattern)
            except re.error:
                self.log.exception("Invalid regex pattern '%s' in configuration", pattern)
                invalid_patterns.append(pattern)
            except TypeError:
                self.log.exception("Invalid type for pattern in configuration: %s", pattern)
                invalid_patterns.append(str(pattern))

        return invalid_patterns

    async def start(self) -> None:
        """Initialise the plugin: load config, resolve reporting room, validate regexes.

        Loads the configuration using `load_and_update`. If a reporting room alias is
        configured, attempts to resolve it to an ID and updates the config if successful.
        Calls `_validate_config_patterns` and sends a startup report if invalid patterns
        are found. Logs final loaded configuration values. Catches and reports fatal
        startup errors.
        """
        await super().start()
        try:
            if not isinstance(self.config, Config):
                self.log.error("Plugin configuration class is not loaded correctly.")
                return

            self.config.load_and_update()

            report_room_setting = self.config["reporting.room"]
            if report_room_setting:
                resolved_room = await self.resolve_room_alias(report_room_setting)
                if resolved_room != report_room_setting and resolved_room.startswith("!"):
                    self.log.info(
                        "Resolved reporting room '%s' to ID '%s'. Updating config.",
                        report_room_setting,
                        resolved_room,
                    )
                    self.config["reporting.room"] = resolved_room
                    self.config.save()
                elif resolved_room == report_room_setting and not resolved_room.startswith("!"):
                    self.log.warning(
                        "Could not resolve reporting room alias '%s' to an ID. "
                        "Reporting might fail.",
                        report_room_setting,
                    )

            invalid_patterns = await self._validate_config_patterns()
            if invalid_patterns:
                error_message = (
                    f"⚠️ Configuration Error: Invalid regex pattern(s) found in "
                    f"`redaction.reasons`: {', '.join(f'`{p}`' for p in invalid_patterns)}. "
                    f"These patterns will be ignored."
                )
                self.log.error(error_message)
                await self._send_startup_report(error_message, level="error")

            self.log.info(
                "Redactor Plugin started. Config loaded: "
                "redaction_mxids=%s, redaction_reasons=%s, "
                "max_messages=%s, max_age_hours=%s, report_room=%s",
                self.config["redaction.mxids"],
                self.config["redaction.reasons"],
                self.config["redaction.max_messages"],
                self.config["redaction.max_age_hours"],
                self.config["reporting.room"] or "(disabled)",
            )

        except Exception:
            self.log.exception("FATAL: Unexpected error during RedactorPlugin startup.")
            if self.config:
                await self._send_startup_report(
                    "❌ CRITICAL: Redactor Plugin failed to start. Check logs for details.",
                    level="error",
                )
