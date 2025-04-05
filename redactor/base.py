"""Base plugin implementation providing core Maubot functionality.

This module provides the foundation for the Redactor plugin by handling:
- Plugin configuration loading and updates.
- Room alias resolution and caching for reporting.
- Matrix client setup and management.
- Startup configuration validation (e.g. regex patterns).

The BasePlugin class implements common Maubot plugin functionality, allowing
the main RedactorPlugin to focus on ban event handling and message redaction.

Technical Details:
    - Implements Maubot's Plugin interface.
    - Caches room alias resolutions for performance with TTL.
    - Handles configuration validation and updates.
    - Reports configuration errors on startup.
"""

from __future__ import annotations

# Standard library imports
import re
from typing import TYPE_CHECKING

# Maubot and Mautrix imports
from maubot import Plugin
from mautrix.types import RoomAlias, RoomID

# Local module imports
from .config import Config
from .utils import lru_cache_with_ttl  # Import the new cache

if TYPE_CHECKING:
    from mautrix.util.config import BaseProxyConfig
    from mautrix.util.logging import TraceLogger


class BasePlugin(Plugin):
    """Base plugin providing core Maubot functionality."""

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
        """Resolve a room alias to a room ID.

        Uses an LRU cache with a 1-hour TTL.

        Args:
            room_alias: The room alias string (e.g., #room:server.org).

        Returns:
            The resolved room ID or the original alias if resolution fails or input is invalid.
        """
        # Basic validation: must be a string and start with # or !
        if not isinstance(room_alias, str) or not room_alias:
            self.log.debug("resolve_room_alias called with invalid input: %s", room_alias)
            return room_alias  # Return input if not a valid-looking alias/ID

        # If it already looks like an ID, return it directly
        if room_alias.startswith("!"):
            # Could add further validation here if needed, e.g., regex check for ID format
            return room_alias

        # Only attempt resolution if it starts with #
        if not room_alias.startswith("#"):
            self.log.warning("resolve_room_alias called with unexpected format: %s", room_alias)
            return room_alias  # Return input if not starting with #

        try:
            # Ensure we use the RoomAlias type for the API call
            resolved = await self.client.resolve_room_alias(RoomAlias(room_alias))
            # Check if resolution was successful and returned a valid RoomID object
            if resolved and resolved.room_id:
                self.log.debug("Resolved room alias %s to %s", room_alias, resolved.room_id)
                return resolved.room_id
            # Handle cases where resolution might return empty/unexpected results
            self.log.warning("Resolving room alias %s did not return a valid room ID.", room_alias)
        except Exception:
            # Log any exception during resolution
            self.log.exception("Failed to resolve room alias %s", room_alias)
            return room_alias  # Fallback to original alias on error
        else:
            return room_alias  # Fallback to original alias

    async def _send_startup_report(self, message: str, level: str = "info") -> None:
        """Sends a message to the configured reporting room during startup."""
        report_room_id = self.config["reporting.room"]
        post_errors = self.config["reporting.post_errors"]

        if level == "error" and not post_errors:
            self.log.warning(
                "Startup error occurred but reporting.post_errors is false. Error: %s", message
            )
            return  # Don't report errors if disabled

        if not report_room_id:
            self.log.info("Reporting room not configured. Startup message: %s", message)
            return  # Don't attempt to send if no room is set

        try:
            # Resolve the alias just in case it wasn't resolved during initial start
            # This uses the cached version if available
            resolved_report_room = await self.resolve_room_alias(report_room_id)
            if not resolved_report_room.startswith("!"):
                self.log.error(
                    "Failed to resolve reporting room alias '%s' to an ID for startup message.",
                    report_room_id,
                )
                return  # Cannot send if resolution failed

            await self.client.send_text(RoomID(resolved_report_room), message)
            self.log.info("Sent startup %s report to %s.", level, resolved_report_room)
        except Exception:
            self.log.exception(
                "CRITICAL: Failed to send startup %s report to %s", level, report_room_id
            )

    async def _validate_config_patterns(self) -> list[str]:
        """Validates regex patterns in the configuration.

        Returns:
            A list of invalid pattern strings found.
        """
        invalid_patterns = []
        reason_patterns: list[str] = self.config["redaction.reasons"]
        if not reason_patterns:
            self.log.debug("No redaction reason patterns configured.")
            return []  # No patterns to validate

        self.log.debug("Validating %d reason patterns...", len(reason_patterns))
        for pattern in reason_patterns:
            try:
                re.compile(pattern)
                self.log.debug("Pattern '%s' is valid.", pattern)
            except re.error:
                self.log.exception("Invalid regex pattern '%s' in configuration", pattern)
                invalid_patterns.append(pattern)
            except TypeError:
                # Catch if pattern is not a string
                self.log.exception("Invalid type for pattern in configuration: %s", pattern)
                invalid_patterns.append(str(pattern))  # Add string representation

        return invalid_patterns

    async def start(self) -> None:
        """Initialise plugin by loading config and validating settings."""
        await super().start()
        try:
            # Configuration loading and updating
            if not isinstance(self.config, Config):
                self.log.error("Plugin configuration class is not loaded correctly.")
                # Potentially raise an error or prevent further startup?
                return  # Stop if config structure is wrong

            self.config.load_and_update()

            # Resolve and update reporting room ID in config *before* validation reports
            report_room_setting = self.config["reporting.room"]
            if report_room_setting:
                resolved_room = await self.resolve_room_alias(report_room_setting)
                if resolved_room != report_room_setting and resolved_room.startswith("!"):
                    self.log.info(
                        "Resolved reporting room '%s' to ID '%s'. Updating config.",
                        report_room_setting,
                        resolved_room,
                    )
                    # Update the config with the resolved room ID
                    self.config["reporting.room"] = resolved_room
                    self.config.save()
                elif resolved_room == report_room_setting and not resolved_room.startswith("!"):
                    self.log.warning(
                        "Could not resolve reporting room alias '%s' to an ID. "
                        "Reporting might fail.",
                        report_room_setting,
                    )
                    # Keep the alias, but reporting might not work

            # --- Configuration Validation ---
            invalid_patterns = await self._validate_config_patterns()
            if invalid_patterns:
                error_message = (
                    f"⚠️ Configuration Error: Invalid regex pattern(s) found in "
                    f"`redaction.reasons`: {', '.join(f'`{p}`' for p in invalid_patterns)}. "
                    f"These patterns will be ignored."
                )
                # Log the error regardless of reporting settings
                self.log.error(error_message)
                # Send report if configured
                await self._send_startup_report(error_message, level="error")

            # Log final loaded configuration values
            self.log.info(
                "Redactor Plugin started. Config loaded: "
                "redaction_mxids=%s, redaction_reasons=%s, "
                "max_messages=%s, max_age_hours=%s, report_room=%s, report_redactions=%s, "
                "post_errors=%s",
                self.config["redaction.mxids"],
                self.config[
                    "redaction.reasons"
                ],  # Log the raw list including potentially invalid ones
                self.config["redaction.max_messages"],
                self.config["redaction.max_age_hours"],
                self.config["reporting.room"] or "(disabled)",
                self.config["reporting.report_redactions"],
                self.config["reporting.post_errors"],
            )

            # Send a startup confirmation message if reporting is enabled
            # Avoid sending if there was a config error reported above
            if not invalid_patterns and self.config["reporting.room"]:
                await self._send_startup_report("✅ Redactor Plugin started successfully.")

        except Exception:
            # Catch any other unexpected errors during startup
            self.log.exception("FATAL: Unexpected error during RedactorPlugin startup.")
            # Try to report this critical failure if possible
            if self.config:  # Check if config was loaded enough to access reporting settings
                await self._send_startup_report(
                    "❌ CRITICAL: Redactor Plugin failed to start. Check logs for details.",
                    level="error",
                )
