"""Maubot plugin for automatically redacting messages from banned users.

This package provides a Maubot plugin that helps automate moderation tasks
by redacting messages from users who are banned for specific, configured reasons
by designated moderators.

The plugin can:
- Monitor ban events in Matrix rooms.
- Check if the ban reason matches configured patterns for specified moderators.
- Automatically redact a configurable number of recent messages from the banned user.
- Report redactions and errors to a designated room.

The plugin is structured into several modules:
- config: Configuration management and settings.
- plugin: Core plugin implementation and Matrix event handling.
- base: Base class with common plugin functionality.
- utils: Helper functions (if needed in the future).

For setup and usage instructions, see the README.md file.
"""

from __future__ import annotations

from .plugin import RedactorPlugin

__all__ = ["RedactorPlugin"]
