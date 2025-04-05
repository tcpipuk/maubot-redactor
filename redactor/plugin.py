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
import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, TypedDict

# Maubot and Mautrix imports
from maubot.handlers import event
from mautrix.errors import MForbidden, MNotFound
from mautrix.types import (
    EventID,
    EventType,
    Membership,
    MessageEvent,
    PaginationDirection,
    RoomGetMessagesResponse,
    RoomID,
    RoomMemberStateEventContent,
    StateEvent,
    UserID,
)

# Local module imports
from .base import BasePlugin
from .utils import get_room_identifier

if TYPE_CHECKING:
    # Type hints for Mautrix API components and types
    from mautrix.util.logging import TraceLogger

    from .config import Config

# Define the constant for backwards pagination direction for clarity
DIRECTION_BACKWARDS: PaginationDirection = "b"


class ReportContext(TypedDict):
    """Encapsulates data needed for successful redaction reports."""

    room_id: RoomID
    banned_user_mxid: UserID
    moderator_mxid: UserID
    reason: str
    count: int
    total_considered: int


class ErrorReportContext(TypedDict):
    """Encapsulates data needed for error reports during redaction processing."""

    room_id: RoomID
    banned_user_mxid: UserID
    error: Exception


class RedactorPlugin(BasePlugin):
    """A Maubot plugin that automatically redacts messages from users who are banned.

    This plugin monitors rooms for ban events and redacts recent messages from
    the banned user if the ban was issued by specific moderators for reasons
    matching configured patterns.
    """

    config: Config
    log: TraceLogger

    # --- Ban Event Handling ---

    @event(EventType.ROOM_MEMBER)
    async def handle_ban_event(self, evt: StateEvent[RoomMemberStateEventContent]) -> None:
        """Handles incoming m.room.member state events.

        Identifies ban events and checks if they meet the configured criteria
        (moderator MXID, ban reason pattern) to trigger message redaction.
        """
        if not isinstance(evt, StateEvent) or not isinstance(
            evt.content, RoomMemberStateEventContent
        ):
            return

        # Only process actual ban events (not profile changes, unbans, leaves, etc.)
        # Also ignore if user bans themselves (state_key == sender)
        if evt.content.membership != Membership.BAN or evt.state_key == evt.sender:
            return

        moderator_mxid: UserID = evt.sender
        banned_user_mxid_str: str | None = evt.state_key
        room_id: RoomID = evt.room_id
        reason: str = evt.content.reason or ""
        # Use timezone aware datetime objects for comparisons
        ban_ts = datetime.fromtimestamp(evt.origin_server_ts / 1000, tz=UTC)

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

        # Check if the ban meets configured criteria using helper methods
        if self._check_moderator(moderator_mxid, room_id) and self._check_reason(reason, room_id):
            self.log.info(
                "Ban by %s for '%s' matches criteria. Checking messages for %s in %s.",
                moderator_mxid,
                reason,
                banned_user_mxid,
                room_id,
            )
            # If criteria met, proceed to fetch and redact messages
            await self._redact_user_messages(
                room_id=room_id,
                banned_user_mxid=banned_user_mxid,
                moderator_mxid=moderator_mxid,
                reason=reason,
                ban_ts=ban_ts,
            )
        else:
            # Log if criteria were not met
            self.log.debug(
                "Ban criteria not met for %s in %s. No redaction action taken.",
                banned_user_mxid,
                room_id,
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
            # Log a warning if the feature is essentially disabled by lack of config
            self.log.warning(
                "Redaction triggered in %s, but no moderator MXIDs configured!", room_id
            )
            return False  # Cannot proceed without knowing allowed moderators

        if moderator_mxid not in allowed_moderators:
            self.log.debug(
                "Ignoring ban by %s in %s - not in configured list: %s",
                moderator_mxid,
                room_id,
                allowed_moderators,
            )
            return False
        # Moderator is in the list
        return True

    def _check_reason(self, reason: str, room_id: RoomID) -> bool:
        """Checks if the provided ban reason matches any configured regex patterns.

        Matching is case-insensitive.

        Args:
            reason: The reason string provided with the ban event.
            room_id: The ID of the room where the ban occurred (for logging).

        Returns:
            True if the reason matches a pattern or if no patterns are configured,
            False otherwise.
        """
        reason_patterns: list[str] = self.config["redaction.reasons"]
        if not reason_patterns:
            # If no patterns are set, consider any reason (or no reason) a match
            self.log.debug("No reason patterns configured for room %s, assuming match.", room_id)
            return True

        # Iterate through configured patterns
        for pattern in reason_patterns:
            try:
                # Use re.search for case-insensitive substring matching
                if re.search(pattern, reason, re.IGNORECASE):
                    self.log.debug(
                        "Ban reason '%s' matched pattern '%s' in %s.", reason, pattern, room_id
                    )
                    return True  # Match found, no need to check further
            except re.error as e:
                # Log invalid regex patterns but continue checking others
                self.log.warning("Invalid regex pattern '%s' in config: %s", pattern, e)

        # If loop completes without finding a match
        self.log.debug(
            "Ignoring ban reason '%s' in %s - did not match patterns: %s",
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
        try:
            # Fetch the list of EventIDs to redact based on configuration limits
            messages_to_redact = await self._fetch_messages_to_redact(
                room_id, banned_user_mxid, ban_ts
            )

            # If no messages meet the criteria, log and exit early
            if not messages_to_redact:
                self.log.info(
                    "No messages found to redact for %s in %s within configured limits.",
                    banned_user_mxid,
                    room_id,
                )
                return

            self.log.info(
                "Attempting to redact %d messages from %s in %s...",
                len(messages_to_redact),
                banned_user_mxid,
                room_id,
            )

            # Construct the reason string for the redaction events themselves
            redaction_reason_str = (
                f"Redacted due to ban of {banned_user_mxid} by {moderator_mxid} "
                f"(Reason: {reason or 'Not specified'})"
            )

            # Perform the redactions and get the count of successful ones
            redacted_count = await self._perform_redactions(
                room_id, messages_to_redact, redaction_reason_str
            )

            self.log.info(
                "Successfully redacted %d/%d messages in %s.",
                redacted_count,
                len(messages_to_redact),
                room_id,
            )

            # --- Reporting ---
            report_ctx = ReportContext(
                room_id=room_id,
                banned_user_mxid=banned_user_mxid,
                moderator_mxid=moderator_mxid,
                reason=reason,
                count=redacted_count,
                total_considered=len(messages_to_redact),
            )
            failed_count = len(messages_to_redact) - redacted_count

            # Report successful actions if enabled and actions occurred
            if redacted_count > 0 and self.config["reporting.report_redactions"]:
                await self._report_action(report_ctx)
            # Report errors if enabled and some redactions failed
            elif failed_count > 0 and self.config["reporting.post_errors"]:
                error_ctx = ErrorReportContext(
                    room_id=room_id,
                    banned_user_mxid=banned_user_mxid,
                    error=Exception(
                        f"Failed to redact {failed_count} message(s) (see logs for details)."
                    ),
                )
                await self._report_error(error_ctx)

        except Exception as e:
            # Catch errors during the overall fetching/processing
            self.log.exception(
                "Error during message fetching/processing for %s in %s", banned_user_mxid, room_id
            )
            # Report the error if enabled
            if self.config["reporting.post_errors"]:
                error_ctx = ErrorReportContext(
                    room_id=room_id, banned_user_mxid=banned_user_mxid, error=e
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
            # Ensure max_age_hours is treated as a float for timedelta
            cutoff = ban_ts - timedelta(hours=float(max_age_hours))
        except ValueError:
            # Handle cases where config value might not be a valid number
            self.log.exception(
                "Invalid value for max_age_hours: %s. Disabling time limit.", max_age_hours
            )
            cutoff = None  # Explicitly set to None on error
        else:
            # Log the calculated cutoff time if successful
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
        event_ts = datetime.fromtimestamp(message_evt.origin_server_ts / 1000, tz=UTC)

        # Check 1: Time limit
        if cutoff_time and event_ts < cutoff_time:
            self.log.debug(
                "Stopping check: Event %s from %s at %s is older than cutoff time %s.",
                message_evt.event_id,
                message_evt.sender,
                event_ts,
                cutoff_time,
            )
            return None, True  # Time limit reached for this and subsequent messages

        # Check 2: User match
        if message_evt.sender != banned_user_mxid:
            return None, False  # Not the right user, but time limit not necessarily reached

        # Message is from the target user and within time limits
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
                break  # Stop processing this batch

            if event_id_to_add:
                if max_messages is None or len(messages_to_redact) < max_messages:
                    messages_to_redact.append(event_id_to_add)
                    self.log.debug(
                        "Adding message %s to list (%d/%s).",
                        event_id_to_add,
                        len(messages_to_redact),
                        max_messages or "unlimited",
                    )
                    # Check if we *just* hit the limit
                    if max_messages is not None and len(messages_to_redact) >= max_messages:
                        self.log.debug(
                            "Reached max_messages limit (%d) after adding event.", max_messages
                        )
                        reached_message_limit_in_batch = True
                        break  # Stop processing this batch
                else:
                    # This case means we already had enough messages before this one
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
        fetch_limit = 100  # How many messages to request per API call

        # Loop fetching message batches until a limit is hit or history ends
        while True:
            # Optimization: Stop fetching if we already have enough messages
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
                # Fetch a batch of messages going backwards in time
                resp: RoomGetMessagesResponse = await self.client.get_room_messages(
                    room_id=room_id,
                    start=pagination_token,
                    limit=fetch_limit,
                    direction=DIRECTION_BACKWARDS,
                )
            except Exception:
                # Log and re-raise to be handled by the main orchestrator method
                self.log.exception("Failed to fetch messages for room %s", room_id)
                raise

            # Check if the server returned any messages in this batch
            if not resp or not resp.chunk:
                self.log.debug(
                    "No more messages found for %s from token %s.", room_id, pagination_token
                )
                break  # End of room history reached

            # Prepare for the next fetch iteration
            pagination_token = resp.end
            batch_size = len(resp.chunk)
            events_checked_total += batch_size

            # Process the fetched batch using the helper function
            reached_time_limit, reached_message_limit = self._process_message_batch(
                resp.chunk, messages, cutoff_time, banned_user_mxid, max_messages_to_redact
            )

            self.log.debug("Processed %d events in batch for %s.", batch_size, room_id)

            # Stop fetching older batches if any limit was hit within this batch
            if reached_time_limit:
                self.log.debug("Reached time limit, stopping message fetch for %s.", room_id)
                break
            if reached_message_limit:
                # Log message already handled inside _process_message_batch if limit hit
                break
            # Stop if the server indicates no more pages available
            if not pagination_token:
                self.log.debug("Server indicated no more messages for %s.", room_id)
                break

        # Log final outcome of the fetching process
        self.log.info(
            "Checked %d events in %s. Found %d messages from %s within limits.",
            events_checked_total,
            room_id,
            len(messages),
            banned_user_mxid,
        )
        return messages

    async def _perform_redactions(
        self, room_id: RoomID, event_ids: list[EventID], redaction_reason: str
    ) -> int:
        """Attempts to redact the provided list of event IDs in the specified room.

        Logs warnings for common, non-critical errors like permission issues
        or attempting to redact already-redacted messages.

        Args:
            room_id: The room where the messages exist.
            event_ids: A list of EventIDs to redact.
            redaction_reason: The reason string to include in the redaction events.

        Returns:
            The number of messages successfully redacted.
        """
        redacted_count = 0
        for event_id in event_ids:
            try:
                # Attempt the redaction via the client API
                await self.client.redact(
                    room_id=room_id, event_id=event_id, reason=redaction_reason
                )
                redacted_count += 1
                self.log.debug("Successfully redacted message %s in %s", event_id, room_id)
            except MForbidden:
                # Log permission errors as warnings - might happen if bot loses power level
                self.log.warning(
                    "Failed to redact message %s in %s: Permission denied (MForbidden).",
                    event_id,
                    room_id,
                )
            except MNotFound:
                # Log not found errors as warnings - message might have been
                # deleted/redacted already
                self.log.warning(
                    "Failed to redact message %s in %s: Not found (MNotFound - already redacted?).",
                    event_id,
                    room_id,
                )
            except Exception:
                # Log any other unexpected errors during redaction
                self.log.exception("Unexpected error redacting message %s in %s", event_id, room_id)
        # Return the count of successful redactions
        return redacted_count

    # --- Reporting ---

    async def _report_action(self, ctx: ReportContext) -> None:
        """Sends a notification message about successful redactions to the report room.

        Uses a ReportContext object for cleaner parameter passing.

        Args:
            ctx: A TypedDict containing the details needed for the report message.
        """
        report_room_id = self.config["reporting.room"]
        # Exit if reporting is disabled
        if not report_room_id:
            return

        try:
            # Use utility function to get a user-friendly room name/ID
            room_identifier = await get_room_identifier(self.client, ctx["room_id"], self.log)
            reason_text = ctx["reason"] or "Not specified"
            # Construct the report message
            message = (
                f"Redacted {ctx['count']}/{ctx['total_considered']} message(s) from "
                f"`{ctx['banned_user_mxid']}` in {room_identifier} due to ban by "
                f"`{ctx['moderator_mxid']}` (Reason: '{reason_text}')"
            )
            # Send the message to the configured report room
            await self.client.send_text(RoomID(report_room_id), message)
            self.log.info(
                "Sent redaction report to %s for ban in %s", report_room_id, ctx["room_id"]
            )
        except Exception:
            # Catch errors during the reporting process itself
            self.log.exception("Failed to send redaction report to %s", report_room_id)

    async def _report_error(self, ctx: ErrorReportContext) -> None:
        """Sends a notification message about errors encountered during processing.

        This sends the report to the configured report room, using an
        ErrorReportContext object for cleaner parameter passing.

        Args:
            ctx: A TypedDict containing the details needed for the error report.
        """
        report_room_id = self.config["reporting.room"]
        # Exit if reporting is disabled
        if not report_room_id:
            return

        try:
            # Use utility function to get a user-friendly room name/ID
            room_identifier = await get_room_identifier(self.client, ctx["room_id"], self.log)
            # Construct the error message
            message = (
                f"⚠️ Error processing ban redaction for user `{ctx['banned_user_mxid']}` "
                f"in {room_identifier}: {ctx['error']!s}"
            )
            # Send the message to the configured report room
            await self.client.send_text(RoomID(report_room_id), message)
            self.log.info("Sent error report to %s for ban in %s", report_room_id, ctx["room_id"])
        except Exception:
            # Avoid error loops if reporting itself fails
            self.log.exception("CRITICAL: Failed to send error report to %s", report_room_id)
