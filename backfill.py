"""Backfill tool - Fetch existing message history and save to Google Sheets/Drive.

Messages and their thread replies are grouped together in the spreadsheet.
Public channels → shared spreadsheet, Private channels → separate spreadsheet.

Usage:
    python backfill.py                      # All channels the bot is in
    python backfill.py --channel general     # Specific channel by name
    python backfill.py --days 30            # Last 30 days (default: 90)
"""

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta, timezone

from slack_sdk import WebClient

import config
from google_sheets import SheetsHandler
from google_drive import DriveHandler
from slack_utils import get_user_info, get_member_emails

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def backfill(channel_filter: str | None = None, days: int = 90):
    client = WebClient(token=config.SLACK_BOT_TOKEN)
    sheets = SheetsHandler()
    drive = DriveHandler()

    # Get channels
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
        logger.info(f"Backfilling #{ch_name} [{label}]...")

        member_emails = get_member_emails(client, ch_id) if is_private else None

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

                permalink = ""
                try:
                    result = client.chat_getPermalink(channel=ch_id, message_ts=ts)
                    permalink = result.get("permalink", "")
                except Exception:
                    pass

                attachment_links = []
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
                    "parent_text": None,
                    "attachment_links": attachment_links,
                    "permalink": permalink,
                })

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

                            r_permalink = ""
                            try:
                                result = client.chat_getPermalink(
                                    channel=ch_id, message_ts=r_ts
                                )
                                r_permalink = result.get("permalink", "")
                            except Exception:
                                pass

                            r_links = []
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
                                "parent_text": text,
                                "attachment_links": r_links,
                                "permalink": r_permalink,
                            })

                    except Exception as e:
                        logger.error(f"Failed to fetch thread replies: {e}")

                time.sleep(0.5)

            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
            time.sleep(1)

        # Phase 2: Write grouped by thread
        new_count, skip_count = sheets.write_messages_grouped(
            ch_name, collected, is_private, member_emails,
        )
        logger.info(f"  #{ch_name}: {new_count} new, {skip_count} duplicates skipped")
        total_new += new_count
        total_skipped += skip_count

    logger.info(
        f"Backfill complete. New: {total_new}, Skipped (duplicate): {total_skipped}"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill Slack messages to Google Sheets")
    parser.add_argument("--channel", type=str, help="Channel name to backfill (default: all)")
    parser.add_argument("--days", type=int, default=90, help="Number of days to look back (default: 90)")
    args = parser.parse_args()

    backfill(channel_filter=args.channel, days=args.days)
