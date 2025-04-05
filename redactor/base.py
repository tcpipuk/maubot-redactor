"""Base plugin implementation providing core Maubot functionality.

This module provides the foundation for the Redactor plugin by handling:
- Plugin configuration loading and updates.
- Room alias resolution and caching for reporting.
- Matrix client setup and management.

The BasePlugin class implements common Maubot plugin functionality, allowing
the main RedactorPlugin to focus on ban event handling and message redaction.

Technical Details:
    - Implements Maubot's Plugin interface.
    - Caches room alias resolutions for performance.
    - Handles configuration validation and updates.
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING

from maubot import Plugin
from mautrix.types import RoomAlias

from .config import Config  # Updated import

if TYPE_CHECKING:
    from mautrix.util.config import BaseProxyConfig


class BasePlugin(Plugin):
    """Base plugin providing core Maubot functionality."""

    @classmethod
    def get_config_class(cls) -> type[BaseProxyConfig]:
        """Get the configuration class for this plugin.

        Returns:
            The Config class for this plugin.
        """
        return Config

    @lru_cache(maxsize=100)
    async def resolve_room_alias(self, room_alias: str) -> str:
        """Resolve a room alias to a room ID.

        Returns:
            The resolved room ID or the original alias if resolution fails.
        """
        if not room_alias or not room_alias.startswith(("#", "!")):
            return room_alias

        try:
            if room_alias.startswith("!"):
                return room_alias
            resolved = await self.client.resolve_room_alias(RoomAlias(room_alias))
        except Exception:
            self.log.exception("Failed to resolve room alias %s", room_alias)
            return room_alias
        else:
            return resolved.room_id

    async def start(self) -> None:
        """Initialise plugin by loading config."""
        await super().start()
        try:
            if not isinstance(self.config, Config):
                self.log.error("Plugin not yet configured.")
                return

            # Load and update config from Maubot
            self.config.load_and_update()

            # Update report room if needed
            report_room_setting = self.config["reporting.room"]
            if report_room_setting:
                resolved_room = await self.resolve_room_alias(report_room_setting)
                if resolved_room != report_room_setting:
                    self.log.info(
                        "Resolved reporting room %s to %s", report_room_setting, resolved_room
                    )
                    # Update the config with the resolved room ID
                    self.config["reporting.room"] = resolved_room
                    self.config.save()

            # Log current configuration
            self.log.info(
                "Config loaded: redaction_mxids=%s, redaction_reasons=%s, "
                "max_messages=%s, max_age_hours=%s, report_room=%s, report_redactions=%s, "
                "post_errors=%s",
                self.config["redaction.mxids"],
                self.config["redaction.reasons"],
                self.config["redaction.max_messages"],
                self.config["redaction.max_age_hours"],
                self.config["reporting.room"] or "(disabled)",
                self.config["reporting.report_redactions"],
                self.config["reporting.post_errors"],
            )
        except Exception:
            self.log.exception("Error during start")
