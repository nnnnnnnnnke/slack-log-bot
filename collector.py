"""Reading a channel's history into rows ready for the spreadsheet.

Realtime backfill, the one-off backfill script and the weekly collection all
walked the same history-and-threads loop. Keeping three copies meant every
change had to be made three times, and a change missed in one of them would
only show up as one entry point behaving differently from the others.
"""

import logging

import config
from slack_utils import get_user_info, resolve_mentions

logger = logging.getLogger(__name__)

# Joins and leaves are channel bookkeeping, not conversation. bot_message
# covers webhooks and apps; the bot's own replies are excluded separately,
# by author, since messages posted with a bot token carry no subtype.
SKIP_SUBTYPES = ("bot_message", "channel_join", "channel_leave")

PAGE_SIZE = 200


def _collect_attachments(
    drive, files, channel_name, member_emails, channel_id
) -> list[tuple[str, str]]:
    attachments = []
    for file_info in files:
        stored = drive.download_from_slack_and_upload(
            file_info, config.SLACK_BOT_TOKEN, channel_name, member_emails, channel_id,
        )
        if stored:
            attachments.append(stored)
    return attachments


def _build_entry(
    client, drive, message, channel_name, member_emails, known_ts, thread_ts,
    channel_id,
) -> dict | None:
    user_id = message.get("user", "")
    if not user_id:
        return None

    ts = message.get("ts", "")
    # The display name is what people see in Slack; the handle is a second
    # spelling of the same person and reads as noise in a log of who said what.
    display_name, _, _ = get_user_info(client, user_id)

    # Attachments of a message already recorded would be uploaded to Drive
    # again: the row is deduped at write time, the file is not.
    attachments = []
    if ts not in known_ts:
        attachments = _collect_attachments(
            drive, message.get("files", []), channel_name, member_emails, channel_id
        )

    return {
        "display_name": display_name,
        "text": resolve_mentions(client, message.get("text", "")),
        "ts": ts,
        "thread_ts": thread_ts,
        "attachments": attachments,
    }


def fetch_channel_messages(
    client,
    drive,
    channel_id: str,
    channel_name: str,
    oldest_ts: str,
    known_ts: set[str],
    member_emails: list[str] | None = None,
    skip_user_ids: tuple[str, ...] = (),
) -> list[dict]:
    """Every message and thread reply in the channel since oldest_ts.

    `known_ts` is only used to skip attachment uploads; the rows themselves are
    returned either way and deduped when written, so a reply arriving under an
    already-recorded parent is still picked up.
    """
    collected: list[dict] = []
    cursor = None

    while True:
        try:
            resp = client.conversations_history(
                channel=channel_id, oldest=oldest_ts, limit=PAGE_SIZE, cursor=cursor
            )
        except Exception as e:
            logger.error(f"Failed to fetch history for #{channel_name}: {e}")
            break

        for message in resp.get("messages", []):
            if message.get("subtype") in SKIP_SUBTYPES:
                continue
            if message.get("user", "") in skip_user_ids:
                continue

            # Not None: a message broadcast to the channel from a thread comes
            # back from history carrying its thread_ts, and appears again in
            # that thread's replies. Forcing None here made the two copies look
            # like different rows — one a parent, one a reply — and both were
            # kept.
            entry = _build_entry(
                client, drive, message, channel_name, member_emails, known_ts,
                message.get("thread_ts"), channel_id,
            )
            if entry is None:
                continue
            collected.append(entry)

            if message.get("reply_count", 0) > 0:
                collected.extend(_fetch_replies(
                    client, drive, channel_id, channel_name, entry["ts"],
                    member_emails, known_ts, skip_user_ids,
                ))

        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break

    return collected


def _fetch_replies(
    client, drive, channel_id, channel_name, parent_ts,
    member_emails, known_ts, skip_user_ids,
) -> list[dict]:
    try:
        resp = client.conversations_replies(
            channel=channel_id, ts=parent_ts, limit=PAGE_SIZE
        )
    except Exception as e:
        logger.error(f"Failed to fetch thread replies: {e}")
        return []

    replies = []
    # The first entry is the parent, which the caller already has.
    for reply in resp.get("messages", [])[1:]:
        if reply.get("subtype") in SKIP_SUBTYPES:
            continue
        if reply.get("user", "") in skip_user_ids:
            continue
        entry = _build_entry(
            client, drive, reply, channel_name, member_emails, known_ts,
            parent_ts, channel_id,
        )
        if entry is not None:
            replies.append(entry)
    return replies
