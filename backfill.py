"""Backfill tool - Fetch existing message history and save to Google Sheets/Drive.

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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def resolve_user(client: WebClient, user_id: str, cache: dict) -> tuple[str, str]:
    """Resolve Slack user ID to (display_name, username)."""
    if user_id in cache:
        return cache[user_id]
    try:
        result = client.users_info(user=user_id)
        user = result["user"]
        profile = user.get("profile", {})
        display_name = (
            profile.get("display_name")
            or profile.get("real_name")
            or user.get("real_name")
            or user.get("name")
            or user_id
        )
        username = user.get("name", user_id)
        cache[user_id] = (display_name, username)
        return (display_name, username)
    except Exception:
        return (user_id, user_id)


def backfill(channel_filter: str | None = None, days: int = 90):
    client = WebClient(token=config.SLACK_BOT_TOKEN)
    sheets = SheetsHandler()
    drive = DriveHandler()

    user_cache: dict[str, tuple[str, str]] = {}

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
        logger.info(f"Backfilling #{ch_name}...")

        new_count = 0
        skip_count = 0
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
                thread_ts = msg.get("thread_ts")
                files = msg.get("files", [])

                display_name, username = resolve_user(client, user_id, user_cache)

                permalink = ""
                try:
                    result = client.chat_getPermalink(channel=ch_id, message_ts=ts)
                    permalink = result.get("permalink", "")
                except Exception:
                    pass

                attachment_links = []
                for f in files:
                    link = drive.download_from_slack_and_upload(f, config.SLACK_BOT_TOKEN)
                    if link:
                        attachment_links.append(link)

                added = sheets.append_message(
                    channel_name=ch_name,
                    display_name=display_name,
                    username=username,
                    text=text,
                    ts=ts,
                    thread_ts=thread_ts,
                    parent_text=None,
                    attachment_links=attachment_links,
                    permalink=permalink,
                )
                if added:
                    new_count += 1
                else:
                    skip_count += 1

                # Fetch thread replies
                if msg.get("reply_count", 0) > 0:
                    try:
                        thread_resp = client.conversations_replies(
                            channel=ch_id, ts=ts, limit=200
                        )
                        replies = thread_resp.get("messages", [])
                        parent_text = text

                        for reply in replies[1:]:
                            r_user = reply.get("user", "")
                            if not r_user:
                                continue
                            if reply.get("subtype") in ("bot_message",):
                                continue

                            r_ts = reply.get("ts", "")
                            r_text = reply.get("text", "")
                            r_files = reply.get("files", [])

                            r_display, r_username = resolve_user(client, r_user, user_cache)

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
                                    f, config.SLACK_BOT_TOKEN
                                )
                                if link:
                                    r_links.append(link)

                            r_added = sheets.append_message(
                                channel_name=ch_name,
                                display_name=r_display,
                                username=r_username,
                                text=r_text,
                                ts=r_ts,
                                thread_ts=ts,
                                parent_text=parent_text,
                                attachment_links=r_links,
                                permalink=r_permalink,
                            )
                            if r_added:
                                new_count += 1
                            else:
                                skip_count += 1

                    except Exception as e:
                        logger.error(f"Failed to fetch thread replies: {e}")

                time.sleep(0.5)

            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
            time.sleep(1)

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
