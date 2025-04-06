"""Core Maubot plugin for automated message redaction based on bans.

This module implements the main Maubot plugin that monitors Matrix rooms for
ban events (`m.room.member` with `membership: ban`). When a ban occurs that meets
configured criteria (specific moderator MXID and ban reason pattern), the plugin
automatically redacts recent messages sent by the banned user in that room.

Core Features:
    - Monitors room membership events for bans.
    - Checks if the ban was issued by a moderator listed in the configuration.
    - Matches the ban reason against a list of configured regex patterns (case-insensitive).
    - Redacts recent messages from the banned user based on configurable limits:
        * Maximum number of messages (`max_messages`).
        * Maximum age of messages (`max_age_hours`).
    - Optionally reports successful redactions and processing errors to a designated room.

Technical Implementation:
    - Uses Maubot's event handlers (`@event.on(EventType.ROOM_MEMBER)`).
    - Fetches recent room messages using `client.get_room_messages`.
    - Performs redactions using `client.redact`.
    - Uses standard Python `re` module for reason pattern matching.
    - Handles configuration via the `Config` class (defined in `config.py`).

Configuration is handled through the Maubot admin interface or config file
derived from `base-config.yaml`. See the `config.py` module and `README.md`
for available settings and details.
"""

from __future__ import annotations

# Standard library imports
import asyncio  # Needed for sleep
import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, TypedDict

# Maubot and Mautrix imports
from maubot.handlers import event
from mautrix.errors import MatrixConnectionError, MatrixRequestError, MForbidden, MNotFound
from mautrix.types import (
    EventID,
    EventType,
    Membership,
    MessageEvent,
    PaginationDirection,
    RoomID,
    StateEvent,
    UserID,
)

# Local module imports
from .base import BasePlugin
from .utils import create_matrix_to_url, get_room_identifier

if TYPE_CHECKING:
    # Type hints for Mautrix API components and types
    from mautrix.util.logging import TraceLogger

    from .config import Config

DIRECTION_BACKWARDS = PaginationDirection.BACKWARD

# Constants for retry logic
REDACTION_RETRY_ATTEMPTS = 3
REDACTION_RETRY_DELAY_SECONDS = 2  # Initial delay, could increase


class ReportContext(TypedDict):
    """Encapsulates data needed for successful redaction reports."""

    room_id: RoomID
    banned_user_mxid: UserID
    moderator_mxid: UserID
    reason: str
    count: int
    total_considered: int
    # Add field for the most recent (first processed) redacted event
    first_redacted_event_id: EventID | None


class ErrorReportContext(TypedDict):
    """Encapsulates data needed for error reports during redaction processing."""

    room_id: RoomID
    banned_user_mxid: UserID
    error: Exception
    # Optional: context about where the error occurred
    context_message: str | None


class RedactorPlugin(BasePlugin):
    """A Maubot plugin that automatically redacts messages from users who are banned.

    This plugin monitors rooms for ban events and redacts recent messages from
    the banned user if the ban was issued by specific moderators for reasons
    matching configured patterns.
    """

    config: Config
    log: TraceLogger

    # --- Ban Event Handling ---

    @event.on(EventType.ROOM_MEMBER)
    async def handle_ban_event(self, evt: StateEvent) -> None:
        """Handles incoming m.room.member state events.

        Identifies ban events and checks if they meet the configured criteria
        (moderator MXID, ban reason pattern) to trigger message redaction.
        """
        if not isinstance(evt, StateEvent) or not hasattr(evt.content, "membership"):
            self.log.debug("Ignoring event that is not a StateEvent or lacks content.membership")
            return

        if evt.content.membership != Membership.BAN or evt.state_key == evt.sender:
            return

        moderator_mxid: UserID = evt.sender
        banned_user_mxid_str: str | None = evt.state_key
        room_id: RoomID = evt.room_id
        reason: str = evt.content.reason or ""
        ban_ts = datetime.fromtimestamp(evt.timestamp / 1000, tz=UTC)

        if not banned_user_mxid_str:
            self.log.warning("Received ban event in %s without state_key (target user).", room_id)
            return

        banned_user_mxid = UserID(banned_user_mxid_str)
        self.log.debug(
            "Ban event received in %s: %s banned by %s (Reason: '%s') at %s",
            room_id,
            banned_user_mxid,
            moderator_mxid,
            reason,
            ban_ts,
        )

        if self._check_moderator(moderator_mxid, room_id) and self._check_reason(reason, room_id):
            self.log.info(
                "Ban by %s for '%s' matches criteria. Checking messages for %s in %s.",
                moderator_mxid,
                reason,
                banned_user_mxid,
                room_id,
            )
            await self._redact_user_messages(
                room_id=room_id,
                banned_user_mxid=banned_user_mxid,
                moderator_mxid=moderator_mxid,
                reason=reason,
                ban_ts=ban_ts,
            )
        else:
            self.log.debug(
                "Ban criteria not met for %s in %s by %s (Reason: '%s'). "
                "No redaction action taken.",
                banned_user_mxid,
                room_id,
                moderator_mxid,
                reason,
            )

    def _check_moderator(self, moderator_mxid: UserID, room_id: RoomID) -> bool:
        """Checks if the moderator who issued the ban is listed in the configuration.

        Args:
            moderator_mxid: The MXID of the moderator who issued the ban.
            room_id: The ID of the room where the ban occurred (for logging).

        Returns:
            True if the moderator is allowed, False otherwise.
        """
        allowed_moderators: list[str] = self.config["redaction.mxids"]
        if not allowed_moderators:
            self.log.warning(
                "Redaction check triggered in %s, but no moderator MXIDs configured in "
                "`redaction.mxids`!",
                room_id,
            )
            return False

        if moderator_mxid not in allowed_moderators:
            self.log.debug(
                "Ignoring ban in %s by %s - not in configured list: %s",
                room_id,
                moderator_mxid,
                allowed_moderators,
            )
            return False
        self.log.debug("Moderator %s confirmed in allowed list for %s.", moderator_mxid, room_id)
        return True

    def _check_reason(self, reason: str, room_id: RoomID) -> bool:
        """Checks if the provided ban reason matches any configured regex patterns.

        Matching is case-insensitive. Invalid patterns logged during startup are ignored here.

        Args:
            reason: The reason string provided with the ban event.
            room_id: The ID of the room where the ban occurred (for logging).

        Returns:
            True if the reason matches a pattern or if no patterns are configured,
            False otherwise.
        """
        reason_patterns: list[str] = self.config["redaction.reasons"]
        if not reason_patterns:
            self.log.debug("No reason patterns configured for room %s, assuming match.", room_id)
            return True

        for pattern in reason_patterns:
            try:
                if re.search(pattern, reason, re.IGNORECASE):
                    self.log.debug(
                        "Ban reason '%s' matched pattern '%s' in %s.", reason, pattern, room_id
                    )
                    return True
            except re.error:
                self.log.exception(
                    "Unexpected regex error during check for pattern '%s' in %s. "
                    "This pattern should have been caught at startup.",
                    pattern,
                    room_id,
                )
                continue
            except TypeError:
                self.log.exception(
                    "Unexpected type error during regex check for pattern '%s' in %s. "
                    "Config validation might have failed.",
                    pattern,
                    room_id,
                )

        self.log.debug(
            "Ban reason '%s' in %s did not match any valid configured patterns: %s",
            reason,
            room_id,
            reason_patterns,
        )
        return False

    # --- Message Redaction Logic ---

    async def _redact_user_messages(
        self,
        room_id: RoomID,
        banned_user_mxid: UserID,
        moderator_mxid: UserID,
        reason: str,
        ban_ts: datetime,
    ) -> None:
        """Orchestrates the process of fetching, redacting messages, and reporting.

        Args:
            room_id: The room where the ban occurred.
            banned_user_mxid: The MXID of the banned user.
            moderator_mxid: The MXID of the moderator who issued the ban.
            reason: The reason for the ban.
            ban_ts: The timestamp of the ban event.
        """
        messages_to_redact: list[EventID] = []
        redacted_count = 0
        first_redacted_event_id: EventID | None = None
        error_context_message: str | None = None

        try:
            error_context_message = "fetching messages"
            messages_to_redact = await self._fetch_messages_to_redact(
                room_id, banned_user_mxid, ban_ts
            )

            if not messages_to_redact:
                self.log.info(
                    "No messages found to redact for %s in %s within configured limits.",
                    banned_user_mxid,
                    room_id,
                )
                return

            self.log.info(
                "Attempting to redact %d message(s) from %s in %s...",
                len(messages_to_redact),
                banned_user_mxid,
                room_id,
            )

            error_context_message = "performing redactions"
            redacted_count, first_redacted_event_id = await self._perform_redactions(
                room_id, messages_to_redact, reason
            )

            self.log.info(
                "Successfully redacted %d/%d messages in %s.",
                redacted_count,
                len(messages_to_redact),
                room_id,
            )

            # --- Reporting ---
            error_context_message = "reporting results"
            report_ctx = ReportContext(
                room_id=room_id,
                banned_user_mxid=banned_user_mxid,
                moderator_mxid=moderator_mxid,
                reason=reason,
                count=redacted_count,
                total_considered=len(messages_to_redact),
                first_redacted_event_id=first_redacted_event_id,
            )
            failed_count = len(messages_to_redact) - redacted_count

            if redacted_count > 0 and self.config["reporting.report_redactions"]:
                await self._report_action(report_ctx)
            elif failed_count > 0 and self.config["reporting.post_errors"]:
                error_ctx = ErrorReportContext(
                    room_id=room_id,
                    banned_user_mxid=banned_user_mxid,
                    error=Exception(
                        f"Failed to redact {failed_count} message(s) due to "
                        "permissions, message already gone, or other non-retriable issues "
                        "(see logs for details)."
                    ),
                    context_message="performing redactions (non-retriable failures)",
                )
                await self._report_error(error_ctx)

        except Exception as e:
            self.log.exception(
                "Error during message %s for %s in %s",
                error_context_message or "processing",
                banned_user_mxid,
                room_id,
            )
            if self.config["reporting.post_errors"]:
                error_ctx = ErrorReportContext(
                    room_id=room_id,
                    banned_user_mxid=banned_user_mxid,
                    error=e,
                    context_message=error_context_message,
                )
                await self._report_error(error_ctx)

    def _calculate_cutoff_time(self, ban_ts: datetime) -> datetime | None:
        """Calculates the earliest timestamp for message redaction consideration.

        This is based on the `max_age_hours` configuration setting.

        Args:
            ban_ts: The timestamp of the ban event.

        Returns:
            A timezone-aware datetime object representing the cutoff time,
            or None if `max_age_hours` is not set or invalid.
        """
        max_age_hours: int | float | None = self.config["redaction.max_age_hours"]
        if max_age_hours is None:
            return None
        try:
            cutoff = ban_ts - timedelta(hours=float(max_age_hours))
        except ValueError:
            self.log.exception(
                "Invalid value for max_age_hours: %s. Disabling time limit.", max_age_hours
            )
            cutoff = None
        else:
            self.log.debug(
                "Calculated redaction cutoff time: %s (%s hours before ban at %s)",
                cutoff,
                max_age_hours,
                ban_ts,
            )
        return cutoff

    def _process_message_for_redaction(
        self, message_evt: MessageEvent, cutoff_time: datetime | None, banned_user_mxid: UserID
    ) -> tuple[EventID | None, bool]:
        """Checks a single message event against the redaction criteria (time, sender).

        Args:
            message_evt: The MessageEvent to check.
            cutoff_time: The earliest allowed timestamp for a message.
            banned_user_mxid: The MXID of the user whose messages should be redacted.

        Returns:
            A tuple containing:
            - The EventID if the message should be redacted, else None.
            - A boolean: True if the message was older than the cutoff time (signaling
              that subsequent messages in the batch will also be too old), False otherwise.
        """
        event_ts = datetime.fromtimestamp(message_evt.timestamp / 1000, tz=UTC)

        if cutoff_time and event_ts < cutoff_time:
            self.log.debug(
                "Stopping check: Event %s from %s at %s is older than cutoff time %s.",
                message_evt.event_id,
                message_evt.sender,
                event_ts,
                cutoff_time,
            )
            return None, True

        if message_evt.sender != banned_user_mxid:
            return None, False

        return message_evt.event_id, False

    def _process_message_batch(
        self,
        batch: list[MessageEvent],
        messages_to_redact: list[EventID],
        cutoff_time: datetime | None,
        banned_user_mxid: UserID,
        max_messages: int | None,
    ) -> tuple[bool, bool]:
        """Processes a batch of fetched messages, adding eligible ones to the list.

        Args:
            batch: The list of MessageEvent objects fetched from the server.
            messages_to_redact: The list of EventIDs collected so far (will be mutated).
            cutoff_time: The earliest allowed timestamp for a message.
            banned_user_mxid: The MXID of the target user.
            max_messages: The maximum number of messages to collect.

        Returns:
            A tuple (reached_time_limit, reached_message_limit):
            - reached_time_limit: True if a message older than cutoff_time was encountered.
            - reached_message_limit: True if max_messages was reached.
        """
        reached_time_limit_in_batch = False
        reached_message_limit_in_batch = False

        for message_evt in batch:
            if not isinstance(message_evt, MessageEvent) or not message_evt.event_id:
                continue

            event_id_to_add, reached_time_limit = self._process_message_for_redaction(
                message_evt, cutoff_time, banned_user_mxid
            )

            if reached_time_limit:
                reached_time_limit_in_batch = True
                break

            if event_id_to_add:
                if max_messages is None or len(messages_to_redact) < max_messages:
                    messages_to_redact.append(event_id_to_add)
                    self.log.debug(
                        "Adding message %s to list (%d/%s).",
                        event_id_to_add,
                        len(messages_to_redact),
                        max_messages or "unlimited",
                    )
                    if max_messages is not None and len(messages_to_redact) >= max_messages:
                        self.log.debug(
                            "Reached max_messages limit (%d) after adding event.", max_messages
                        )
                        reached_message_limit_in_batch = True
                        break
                else:
                    self.log.debug(
                        "Reached max_messages limit (%d). Stopping batch processing.", max_messages
                    )
                    reached_message_limit_in_batch = True
                    break

        return reached_time_limit_in_batch, reached_message_limit_in_batch

    async def _fetch_messages_to_redact(
        self, room_id: RoomID, banned_user_mxid: UserID, ban_ts: datetime
    ) -> list[EventID]:
        """Fetches and filters recent messages based on user, time, and count limits.

        Args:
            room_id: The room to fetch messages from.
            banned_user_mxid: The MXID of the user whose messages to find.
            ban_ts: The timestamp of the ban event, used for time calculations.

        Returns:
            A list of EventIDs corresponding to messages that should be redacted.
        """
        max_messages_to_redact: int | None = self.config["redaction.max_messages"]
        cutoff_time = self._calculate_cutoff_time(ban_ts)

        messages: list[EventID] = []
        events_checked_total = 0
        pagination_token: str | None = None
        fetch_limit = 100

        while True:
            if max_messages_to_redact is not None and len(messages) >= max_messages_to_redact:
                self.log.debug(
                    "Reached max_messages limit (%d). Stopping message fetch.",
                    max_messages_to_redact,
                )
                break

            self.log.debug(
                "Fetching message batch for %s starting from token %s", room_id, pagination_token
            )
            try:
                resp = await self.client.get_messages(
                    room_id=room_id,
                    direction=DIRECTION_BACKWARDS,
                    from_token=pagination_token,
                    limit=fetch_limit,
                )
            except Exception:
                self.log.exception("Failed to fetch messages for room %s", room_id)
                raise

            if not resp or not resp.events:
                self.log.debug(
                    "No more messages found for %s from token %s.", room_id, pagination_token
                )
                break

            pagination_token = resp.end
            batch_size = len(resp.events)
            events_checked_total += batch_size

            reached_time_limit, reached_message_limit = self._process_message_batch(
                resp.events, messages, cutoff_time, banned_user_mxid, max_messages_to_redact
            )

            self.log.debug("Processed %d events in batch for %s.", batch_size, room_id)

            if reached_time_limit:
                self.log.debug("Reached time limit, stopping message fetch for %s.", room_id)
                break
            if reached_message_limit:
                break
            if not pagination_token:
                self.log.debug("Server indicated no more messages for %s.", room_id)
                break

        self.log.info(
            "Checked %d events in %s. Found %d messages from %s within limits.",
            events_checked_total,
            room_id,
            len(messages),
            banned_user_mxid,
        )
        return messages

    async def _perform_redactions(
        self, room_id: RoomID, event_ids: list[EventID], original_ban_reason: str
    ) -> tuple[int, EventID | None]:
        """Attempts to redact the provided list of event IDs with retries for transient errors.

        Uses the original ban reason for the redaction event.

        Args:
            room_id: The room where the messages exist.
            event_ids: A list of EventIDs to redact.
            original_ban_reason: The reason string from the original ban event.

        Returns:
            A tuple containing:
            - The number of messages successfully redacted.
            - The EventID of the *first* successfully redacted message.
        """
        redacted_count = 0
        first_success_event_id: EventID | None = None

        for event_id in event_ids:
            should_break_outer = False
            for attempt in range(REDACTION_RETRY_ATTEMPTS):
                try:
                    await self.client.redact(
                        room_id=room_id, event_id=event_id, reason=original_ban_reason
                    )
                    redacted_count += 1
                    if first_success_event_id is None:
                        first_success_event_id = event_id
                    self.log.debug(
                        "Successfully redacted message %s in %s (Attempt %d)",
                        event_id,
                        room_id,
                        attempt + 1,
                    )
                    should_break_outer = True
                    break

                except MForbidden:
                    self.log.warning(
                        "Failed to redact message %s in %s: Permission denied (MForbidden). "
                        "No retry.",
                        event_id,
                        room_id,
                    )
                    should_break_outer = True
                    break
                except MNotFound:
                    self.log.warning(
                        "Failed to redact message %s in %s: Not found (MNotFound - "
                        "already redacted?). No retry.",
                        event_id,
                        room_id,
                    )
                    should_break_outer = True
                    break

                except (MatrixConnectionError, MatrixRequestError) as e:
                    error_type = type(e).__name__
                    self.log.warning(
                        "Failed to redact message %s in %s (Attempt %d/%d): %s (%s). Retrying...",
                        event_id,
                        room_id,
                        attempt + 1,
                        REDACTION_RETRY_ATTEMPTS,
                        error_type,
                        e,
                    )
                    if attempt + 1 == REDACTION_RETRY_ATTEMPTS:
                        self.log.exception(
                            "Failed to redact message %s in %s after %d attempts due to %s",
                            event_id,
                            room_id,
                            REDACTION_RETRY_ATTEMPTS,
                            error_type,
                        )
                        should_break_outer = True
                        break
                    await asyncio.sleep(REDACTION_RETRY_DELAY_SECONDS * (attempt + 1))

                except Exception:
                    self.log.exception(
                        "Unexpected error redacting message %s in %s (Attempt %d)",
                        event_id,
                        room_id,
                        attempt + 1,
                    )
                    should_break_outer = True
                    break
            if should_break_outer:
                continue

        return redacted_count, first_success_event_id

    # --- Reporting ---

    async def _report_action(self, ctx: ReportContext) -> None:
        """Sends a notification message about successful redactions to the report room.

        Includes a matrix.to link to the most recent redacted message if available.
        """
        report_room_id = self.config["reporting.room"]
        if not report_room_id:
            return

        try:
            room_identifier = await get_room_identifier(self.client, ctx["room_id"], self.log)
            reason_text = ctx["reason"] or "Not specified"
            via_servers = self.config["reporting.vias"]

            message = (
                f"Redacted {ctx['count']}/{ctx['total_considered']} message(s) from "
                f"`{ctx['banned_user_mxid']}` in {room_identifier} due to ban by "
                f"`{ctx['moderator_mxid']}` (Reason: '{reason_text}')"
            )

            if ctx["first_redacted_event_id"]:
                event_link = create_matrix_to_url(
                    ctx["room_id"], ctx["first_redacted_event_id"], via_servers
                )
                message += f". [Link to most recent redaction]({event_link})"
            else:
                message += ". (Could not determine link to specific redaction)."

            resolved_report_room = await self.resolve_room_alias(report_room_id)
            if not resolved_report_room.startswith("!"):
                self.log.error(
                    "Failed to resolve reporting room alias '%s' to an ID for action report.",
                    report_room_id,
                )
                return

            await self.client.send_markdown(RoomID(resolved_report_room), message)
            self.log.info(
                "Sent redaction report to %s for ban in %s", resolved_report_room, ctx["room_id"]
            )
        except Exception:
            self.log.exception("Failed to send redaction report to %s", report_room_id)

    async def _report_error(self, ctx: ErrorReportContext) -> None:
        """Sends a notification message about errors encountered during processing."""
        report_room_id = self.config["reporting.room"]
        if not report_room_id or not self.config["reporting.post_errors"]:
            return

        try:
            room_identifier = await get_room_identifier(self.client, ctx["room_id"], self.log)
            context_msg = f" during {ctx['context_message']}" if ctx.get("context_message") else ""

            message = (
                f"⚠️ Error processing ban redaction for user `{ctx['banned_user_mxid']}` "
                f"in {room_identifier}{context_msg}: {ctx['error']!s}"
            )

            resolved_report_room = await self.resolve_room_alias(report_room_id)
            if not resolved_report_room.startswith("!"):
                self.log.error(
                    "Failed to resolve reporting room alias '%s' to an ID for error report.",
                    report_room_id,
                )
                return

            await self.client.send_text(RoomID(resolved_report_room), message)
            self.log.info(
                "Sent error report to %s for ban in %s", resolved_report_room, ctx["room_id"]
            )
        except Exception:
            self.log.exception("CRITICAL: Failed to send error report to %s", report_room_id)
