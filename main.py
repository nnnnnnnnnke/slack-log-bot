"""Slack Log Bot - Collects messages and files from Slack and saves to Google Sheets/Drive."""

import logging
import re
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

import config
from google_sheets import SheetsHandler
from google_drive import DriveHandler
from slack_utils import get_channel_info, get_user_info, get_member_emails

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

app = App(token=config.SLACK_BOT_TOKEN)
sheets = SheetsHandler()
drive = DriveHandler()


def get_permalink(client, channel_id: str, message_ts: str) -> str:
    try:
        result = client.chat_getPermalink(channel=channel_id, message_ts=message_ts)
        return result.get("permalink", "")
    except Exception:
        return ""


def process_files(
    files: list[dict], channel_name: str,
    is_private: bool, member_emails: list[str] | None
) -> list[str]:
    links = []
    for file_info in files:
        link = drive.download_from_slack_and_upload(
            file_info, config.SLACK_BOT_TOKEN, channel_name,
            is_private, member_emails,
        )
        if link:
            links.append(link)
    return links


@app.event("message")
def handle_message(event, client, logger):
    subtype = event.get("subtype")
    if subtype in ("bot_message", "message_changed", "message_deleted", "channel_join", "channel_leave"):
        return

    channel_id = event.get("channel", "")
    user_id = event.get("user", "")
    text = event.get("text", "")
    ts = event.get("ts", "")
    thread_ts = event.get("thread_ts")
    files = event.get("files", [])

    if not user_id:
        return

    # Resolve channel info (name + public/private)
    ch_info = get_channel_info(client, channel_id)
    channel_name = ch_info["name"]
    is_private = ch_info["is_private"]

    # For private channels, get member emails for sharing
    member_emails = get_member_emails(client, channel_id) if is_private else None

    display_name, username, _ = get_user_info(client, user_id)
    permalink = get_permalink(client, channel_id, ts)

    attachment_links = process_files(files, channel_name, is_private, member_emails) if files else []

    try:
        sheets.insert_message(
            channel_name=channel_name,
            display_name=display_name,
            username=username,
            text=text,
            ts=ts,
            thread_ts=thread_ts,
            attachment_links=attachment_links,
            permalink=permalink,
            is_private=is_private,
            member_emails=member_emails,
        )
    except Exception as e:
        logger.error(f"Failed to log message to Sheets: {e}")


@app.event("file_shared")
def handle_file_shared(event, client, logger):
    pass


@app.event("app_mention")
def handle_mention(event, client, say, logger):
    """Handle @bot mentions. Commands:
    @bot           → Show spreadsheet URL
    @bot backfill  → Collect past messages (default 90 days)
    @bot backfill 30 → Collect past 30 days
    """
    channel_id = event.get("channel", "")
    text = event.get("text", "")

    ch_info = get_channel_info(client, channel_id)
    channel_name = ch_info["name"]
    is_private = ch_info["is_private"]

    # Parse command from mention text (strip bot mention)
    cleaned = re.sub(r"<@[A-Z0-9]+>", "", text).strip().lower()

    if cleaned.startswith("backfill"):
        parts = cleaned.split()
        days = 90
        if len(parts) >= 2:
            try:
                days = int(parts[1])
            except ValueError:
                pass

        say(f":hourglass_flowing_sand: `#{channel_name}` の過去 {days} 日分のログ収集を開始します...")

        def run_backfill():
            try:
                _backfill_channel(client, channel_id, channel_name, is_private, days)
                client.chat_postMessage(
                    channel=channel_id,
                    text=f":white_check_mark: `#{channel_name}` のバックフィルが完了しました。",
                )
            except Exception as e:
                logger.error(f"Backfill failed: {e}")
                client.chat_postMessage(
                    channel=channel_id,
                    text=f":x: バックフィル中にエラーが発生しました: {e}",
                )

        threading.Thread(target=run_backfill, daemon=True).start()
    else:
        # Show spreadsheet URL
        url = sheets.get_spreadsheet_url(channel_name, is_private)
        if url:
            say(f":memo: `#{channel_name}` のログはこちら:\n{url}")
        else:
            say(f":memo: `#{channel_name}` のログはまだ作成されていません。メッセージが投稿されると自動的に作成されます。")


def _backfill_channel(client, channel_id: str, channel_name: str, is_private: bool, days: int):
    """Collect past messages for a single channel."""
    member_emails = get_member_emails(client, channel_id) if is_private else None

    oldest = datetime.now(timezone.utc) - timedelta(days=days)
    oldest_ts = str(oldest.timestamp())

    collected: list[dict] = []
    cursor = None

    while True:
        try:
            resp = client.conversations_history(
                channel=channel_id, oldest=oldest_ts, limit=200, cursor=cursor
            )
        except Exception as e:
            logger.error(f"Failed to fetch history for #{channel_name}: {e}")
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
            msg_text = msg.get("text", "")
            files = msg.get("files", [])

            display_name, username, _ = get_user_info(client, user_id)

            permalink = ""
            try:
                result = client.chat_getPermalink(channel=channel_id, message_ts=ts)
                permalink = result.get("permalink", "")
            except Exception:
                pass

            attachment_links = []
            for f in files:
                link = drive.download_from_slack_and_upload(
                    f, config.SLACK_BOT_TOKEN, channel_name,
                    is_private, member_emails,
                )
                if link:
                    attachment_links.append(link)

            collected.append({
                "channel_name": channel_name,
                "display_name": display_name,
                "username": username,
                "text": msg_text,
                "ts": ts,
                "thread_ts": None,
                "attachment_links": attachment_links,
                "permalink": permalink,
            })

            # Fetch thread replies
            if msg.get("reply_count", 0) > 0:
                try:
                    thread_resp = client.conversations_replies(
                        channel=channel_id, ts=ts, limit=200
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
                                channel=channel_id, message_ts=r_ts
                            )
                            r_permalink = result.get("permalink", "")
                        except Exception:
                            pass

                        r_links = []
                        for f in r_files:
                            link = drive.download_from_slack_and_upload(
                                f, config.SLACK_BOT_TOKEN, channel_name,
                                is_private, member_emails,
                            )
                            if link:
                                r_links.append(link)

                        collected.append({
                            "channel_name": channel_name,
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

            time.sleep(0.5)

        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
        time.sleep(1)

    new_count, skip_count = sheets.write_messages_grouped(
        channel_name, collected, is_private, member_emails,
    )
    logger.info(f"Backfill #{channel_name}: {new_count} new, {skip_count} skipped")


def main():
    logger.info("Starting Slack Log Bot...")
    logger.info("Bot is listening for messages via Socket Mode.")
    handler = SocketModeHandler(app, config.SLACK_APP_TOKEN)
    handler.start()


if __name__ == "__main__":
    main()
