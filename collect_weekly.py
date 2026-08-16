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
from collector import fetch_channel_messages
from slack_utils import (
    get_member_emails,
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

    # The bot's own replies ("collection started", "here is the log") are
    # posted with a bot token, which carries no subtype to filter on.
    bot_user_ids = (client.auth_test().get("user_id", ""),)

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
        # Every channel's sheet is shared with that channel's members. Joins are
        # normally picked up live, but a weekly pass catches anything the bot
        # missed while it was down.
        member_emails = get_member_emails(client, ch_id)
        try:
            granted, _ = sheets.sync_channel_access(ch_name, member_emails, channel_id=ch_id)
            granted += drive.sync_channel_access(ch_name, member_emails, channel_id=ch_id)[0]
            if granted:
                logger.info(f"  #{ch_name}: shared with {granted} new member(s)")
        except Exception as e:
            logger.warning(f"  #{ch_name}: could not sync access: {e}")

        # Attachments of messages already recorded would be downloaded and
        # re-uploaded on every run: the rows get deduped at write time, the
        # Drive files do not, so each run leaves another copy behind.
        known_ts = sheets.recorded_ts(ch_name, member_emails, ch_id)

        collected = fetch_channel_messages(
            client, drive, ch_id, ch_name, oldest_ts, known_ts, member_emails,
            skip_user_ids=bot_user_ids,
        )

        # Phase 2: Write grouped by thread
        new_count, skip_count = sheets.write_messages_grouped(
            ch_name, collected, member_emails, ch_id,
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
