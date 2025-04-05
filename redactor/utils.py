"""Utility functions supporting the Redactor plugin.

This module provides helper functions that support the core plugin functionality.

Functions:
    create_matrix_to_url: Creates matrix.to URLs.
    get_room_identifier: Attempts to resolve a room alias, falling back to ID.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from logging import Logger

    from mautrix.client import MaubotMatrixClient
    from mautrix.types import EventID, RoomID


async def get_room_identifier(client: MaubotMatrixClient, room_id: RoomID, log: Logger) -> str:
    """Try to get a room alias for user-friendly reporting, fall back to room ID.

    Args:
        client: The Maubot matrix client instance.
        room_id: The RoomID to identify.
        log: Logger instance for debugging.

    Returns:
        The room alias (if found and resolvable) or the original room ID as a string.
    """
    try:
        # This might fail if the bot isn't in the room or due to permissions.
        main_alias = await client.get_room_alias(room_id)
        if main_alias:
            return main_alias[0]  # Use the first alias found
    except Exception:
        log.debug("Could not get alias for room %s, using ID in report.", room_id)
    return str(room_id)  # Fallback to room ID string


def create_matrix_to_url(room_id: RoomID, event_id: EventID, via_servers: list[str]) -> str:
    """Create a matrix.to URL for a given room ID and event ID.

    Args:
        room_id: The room ID.
        event_id: The event ID.
        via_servers: List of via servers to include in the URL.

    Returns:
        The matrix.to URL.
    """
    via_params = "?" + "&".join(f"via={server}" for server in via_servers) if via_servers else ""
    return f"https://matrix.to/#/{room_id}/{event_id}{via_params}"
