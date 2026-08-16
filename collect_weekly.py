"""Weekly collection script - Fetch the past week's messages and save to Google Sheets/Drive.

Designed to run as a weekly cron job. Deduplication ensures no duplicate messages
even if collection periods overlap or the script runs multiple times.
Messages and their thread replies are grouped together in the spreadsheet.

Public channels  → shared spreadsheet (anyone with link)
Private channels → separate spreadsheet per channel (members only)

Usage:
    python collect_weekly.py                    # Past 7 days, all channels
    python collect_weekly.py --channel general  # Specific channel
    python collect_weekly.py --days 14          # Custom range (default: 8 for overlap margin)
"""

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone

from slack_sdk import WebClient

import config
from google_sheets import SheetsHandler
from google_drive import DriveHandler
from slack_utils import (
    build_permalink,
    get_member_emails,
    get_user_info,
    install_retry_handlers,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("collect_weekly.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


def collect(channel_filter: str | None = None, days: int = 8):
    client = WebClient(token=config.SLACK_BOT_TOKEN)
    install_retry_handlers(client)
    sheets = SheetsHandler()
    drive = DriveHandler()

    # Get channels the bot is a member of
    channels = []
    cursor = None
    while True:
        resp = client.conversations_list(
            types="public_channel,private_channel",
            limit=200,
            cursor=cursor,
        )
        channels.extend(resp["channels"])
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break

    channels = [ch for ch in channels if ch.get("is_member")]

    if channel_filter:
        channels = [ch for ch in channels if ch["name"] == channel_filter]
        if not channels:
            logger.error(f"Channel '{channel_filter}' not found or bot is not a member.")
            return

    oldest = datetime.now(timezone.utc) - timedelta(days=days)
    oldest_ts = str(oldest.timestamp())

    total_new = 0
    total_skipped = 0

    for ch in channels:
        ch_name = ch["name"]
        ch_id = ch["id"]
        is_private = ch.get("is_private", False) or ch.get("is_group", False)

        label = "private" if is_private else "public"
        logger.info(f"Collecting #{ch_name} [{label}] (past {days} days)...")

        # Get member emails for private channels
        member_emails = get_member_emails(client, ch_id) if is_private else None

        # Attachments of messages already recorded would be downloaded and
        # re-uploaded on every run: the rows get deduped at write time, the
        # Drive files do not, so each run leaves another copy behind.
        known_ts = sheets.recorded_ts(ch_name, is_private, member_emails)

        # Phase 1: Collect all messages
        collected: list[dict] = []
        cursor = None

        while True:
            try:
                resp = client.conversations_history(
                    channel=ch_id, oldest=oldest_ts, limit=200, cursor=cursor
                )
            except Exception as e:
                logger.error(f"Failed to fetch history for #{ch_name}: {e}")
                break

            messages = resp.get("messages", [])

            for msg in messages:
                subtype = msg.get("subtype")
                if subtype in ("bot_message", "channel_join", "channel_leave"):
                    continue

                user_id = msg.get("user", "")
                if not user_id:
                    continue

                ts = msg.get("ts", "")
                text = msg.get("text", "")
                files = msg.get("files", [])

                display_name, username, _ = get_user_info(client, user_id)
                permalink = build_permalink(client, ch_id, ts)

                attachment_links = []
                if ts not in known_ts:
                    for f in files:
                        link = drive.download_from_slack_and_upload(
                            f, config.SLACK_BOT_TOKEN, ch_name,
                            is_private, member_emails,
                        )
                        if link:
                            attachment_links.append(link)

                collected.append({
                    "channel_name": ch_name,
                    "display_name": display_name,
                    "username": username,
                    "text": text,
                    "ts": ts,
                    "thread_ts": None,
                    "attachment_links": attachment_links,
                    "permalink": permalink,
                })

                # Fetch thread replies
                if msg.get("reply_count", 0) > 0:
                    try:
                        thread_resp = client.conversations_replies(
                            channel=ch_id, ts=ts, limit=200
                        )
                        replies = thread_resp.get("messages", [])

                        for reply in replies[1:]:
                            r_user = reply.get("user", "")
                            if not r_user:
                                continue
                            if reply.get("subtype") in ("bot_message",):
                                continue

                            r_ts = reply.get("ts", "")
                            r_text = reply.get("text", "")
                            r_files = reply.get("files", [])

                            r_display, r_username, _ = get_user_info(client, r_user)
                            r_permalink = build_permalink(client, ch_id, r_ts, ts)

                            r_links = []
                            if r_ts not in known_ts:
                                for f in r_files:
                                    link = drive.download_from_slack_and_upload(
                                        f, config.SLACK_BOT_TOKEN, ch_name,
                                        is_private, member_emails,
                                    )
                                    if link:
                                        r_links.append(link)

                            collected.append({
                                "channel_name": ch_name,
                                "display_name": r_display,
                                "username": r_username,
                                "text": r_text,
                                "ts": r_ts,
                                "thread_ts": ts,
                                "attachment_links": r_links,
                                "permalink": r_permalink,
                            })

                    except Exception as e:
                        logger.error(f"Failed to fetch thread replies: {e}")

            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break

        # Phase 2: Write grouped by thread
        new_count, skip_count = sheets.write_messages_grouped(
            ch_name, collected, is_private, member_emails,
        )
        logger.info(f"  #{ch_name}: {new_count} new, {skip_count} duplicates skipped")
        total_new += new_count
        total_skipped += skip_count

    logger.info(
        f"Weekly collection complete. "
        f"New: {total_new}, Skipped (duplicate): {total_skipped}"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Weekly Slack message collection")
    parser.add_argument("--channel", type=str, help="Channel name (default: all)")
    parser.add_argument(
        "--days", type=int, default=8,
        help="Days to look back (default: 8 = 7 days + 1 day overlap margin)"
    )
    args = parser.parse_args()

    collect(channel_filter=args.channel, days=args.days)
