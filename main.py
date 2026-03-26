"""Slack Log Bot - Collects messages and files from Slack and saves to Google Sheets/Drive."""

import logging
import sys

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


def get_parent_text(client, channel_id: str, thread_ts: str) -> str | None:
    try:
        result = client.conversations_replies(
            channel=channel_id, ts=thread_ts, limit=1
        )
        messages = result.get("messages", [])
        if messages:
            return messages[0].get("text", "")
    except Exception as e:
        logger.error(f"Failed to get parent message: {e}")
    return None


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

    parent_text = None
    if thread_ts and thread_ts != ts:
        parent_text = get_parent_text(client, channel_id, thread_ts)

    attachment_links = process_files(files, channel_name, is_private, member_emails) if files else []

    try:
        sheets.insert_message(
            channel_name=channel_name,
            display_name=display_name,
            username=username,
            text=text,
            ts=ts,
            thread_ts=thread_ts,
            parent_text=parent_text,
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


def main():
    logger.info("Starting Slack Log Bot...")
    logger.info("Bot is listening for messages via Socket Mode.")
    handler = SocketModeHandler(app, config.SLACK_APP_TOKEN)
    handler.start()


if __name__ == "__main__":
    main()
