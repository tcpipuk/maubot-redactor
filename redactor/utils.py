"""Utility functions supporting the Redactor plugin.

This module provides helper functions that support the core plugin functionality.

Functions:
    create_matrix_to_url: Creates matrix.to URLs.
    get_room_identifier: Attempts to resolve a room alias, falling back to ID.
    lru_cache_with_ttl: An LRU cache implementation with a time-to-live (TTL).
"""

from __future__ import annotations

# Standard library imports
import time
from collections import OrderedDict
from functools import wraps
from typing import TYPE_CHECKING, Any, TypeVar

# Define a generic type variable for the cache decorator
FuncReturnType = TypeVar("FuncReturnType")


if TYPE_CHECKING:
    from collections.abc import Callable
    from logging import Logger

    from mautrix.client import MaubotMatrixClient
    from mautrix.types import EventID, RoomID


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
    # Ensure room_id and event_id are properly encoded if they contain special chars
    # For simplicity, assuming standard IDs. Use urllib.parse.quote if needed.
    return f"https://matrix.to/#/{room_id}/{event_id}{via_params}"


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
            # Ensure we return only the alias string, not the entire response object if applicable
            # Assuming get_room_alias returns a list or similar structure containing the alias
            # string. Adjust based on actual return type if necessary.
            alias_str = (
                main_alias[0]
                if isinstance(main_alias, (list, tuple)) and main_alias
                else str(main_alias)
            )
            # Basic validation that it looks like an alias
            if isinstance(alias_str, str) and alias_str.startswith("#"):
                return alias_str
            log.warning("get_room_alias returned unexpected value for %s: %s", room_id, main_alias)
    except Exception:
        log.debug("Could not get alias for room %s, using ID in report.", room_id)
    return str(room_id)  # Fallback to room ID string


def lru_cache_with_ttl(
    maxsize: int = 128, ttl: int = 3600
) -> Callable[[Callable[..., FuncReturnType]], Callable[..., FuncReturnType]]:
    """Decorator that provides an LRU cache with a Time-To-Live (TTL).

    Args:
        maxsize: The maximum number of items to store in the cache.
        ttl: The time-to-live for cache entries, in seconds.

    Returns:
        A decorator function.
    """

    def decorator(func: Callable[..., FuncReturnType]) -> Callable[..., FuncReturnType]:
        cache: OrderedDict[Any, tuple[Any, float]] = OrderedDict()

        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> FuncReturnType:
            key = (args, tuple(sorted(kwargs.items())))
            current_time = time.monotonic()

            if key in cache:
                result, timestamp = cache[key]
                if current_time - timestamp <= ttl:
                    # Move item to the end (most recently used)
                    cache.move_to_end(key)
                    return result
                # TTL expired, remove the item
                del cache[key]

            # Item not in cache or expired, call the function
            result = await func(*args, **kwargs)
            # Store the new result with the current timestamp
            cache[key] = (result, current_time)
            # Ensure the cache does not exceed maxsize
            if len(cache) > maxsize:
                # Remove the least recently used item (first item)
                cache.popitem(last=False)

            return result

        return wrapper

    return decorator
