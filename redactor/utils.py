"""Utility functions supporting the Redactor plugin.

Provides helper functions for tasks like creating matrix.to URLs, resolving
room identifiers, and caching function results with a time-to-live (TTL).
"""

from __future__ import annotations

import time
from collections import OrderedDict
from functools import wraps
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable
    from logging import Logger

    from mautrix.client import MaubotMatrixClient
    from mautrix.types import EventID, RoomID

FuncReturnType = TypeVar("FuncReturnType")


def create_matrix_to_url(room_id: RoomID, event_id: EventID, via_servers: list[str]) -> str:
    """Construct a matrix.to URL for a specific event within a room.

    Args:
        room_id: The Matrix Room ID.
        event_id: The Matrix Event ID.
        via_servers: A list of server names to include as 'via' parameters
                     in the URL for routing hints.

    Returns:
        A string containing the formatted matrix.to URL.
    """
    via_params = "?" + "&".join(f"via={server}" for server in via_servers) if via_servers else ""
    return f"https://matrix.to/#/{room_id}/{event_id}{via_params}"


async def get_room_identifier(client: MaubotMatrixClient, room_id: RoomID, log: Logger) -> str:
    """Get a user-friendly identifier for a room, preferring alias over ID.

    Attempts to fetch the canonical alias for the given `room_id`. If successful
    and the result looks like an alias (starts with '#'), it returns the alias.
    Otherwise, it logs the situation and returns the original `room_id` string.

    Args:
        client: The MaubotMatrixClient instance to use for API calls.
        room_id: The RoomID of the room to identify.
        log: The logger instance for logging debug/warning messages.

    Returns:
        The room's canonical alias (if found) or the room ID string.
    """
    try:
        main_alias = await client.get_room_alias(room_id)
        if main_alias:
            alias_str = (
                main_alias[0]
                if isinstance(main_alias, (list, tuple)) and main_alias
                else str(main_alias)
            )
            if isinstance(alias_str, str) and alias_str.startswith("#"):
                return alias_str
            log.warning("get_room_alias returned unexpected value for %s: %s", room_id, main_alias)
    except Exception:
        log.debug("Could not get alias for room %s, using ID.", room_id, exc_info=True)
    return str(room_id)


def lru_cache_with_ttl(
    maxsize: int = 128, ttl: int = 3600
) -> Callable[[Callable[..., FuncReturnType]], Callable[..., FuncReturnType]]:
    """Decorator factory providing an LRU cache with Time-To-Live (TTL) for async functions.

    Creates a decorator that caches the results of an async function based on its
    arguments. Cached entries expire after `ttl` seconds. Uses an OrderedDict
    to manage cache entries and eviction based on Least Recently Used (LRU) policy
    when `maxsize` is exceeded.

    Args:
        maxsize: The maximum number of entries to store in the cache.
        ttl: The time-to-live for cache entries, in seconds.

    Returns:
        A decorator function that can be applied to async functions.
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
                    cache.move_to_end(key)
                    return result
                del cache[key]

            result = await func(*args, **kwargs)
            cache[key] = (result, current_time)
            if len(cache) > maxsize:
                cache.popitem(last=False)

            return result

        return wrapper

    return decorator
