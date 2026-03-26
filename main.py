"""Slack Log Bot - Collects messages and files from Slack and saves to Google Sheets/Drive."""

import logging
import sys

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

import config
from google_sheets import SheetsHandler
from google_drive import DriveHandler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

app = App(token=config.SLACK_BOT_TOKEN)
sheets = SheetsHandler()
drive = DriveHandler()

# Cache for user ID -> (display_name, username)
_user_cache: dict[str, tuple[str, str]] = {}
# Cache for channel ID -> channel name
_channel_cache: dict[str, str] = {}


def get_user_info(client, user_id: str) -> tuple[str, str]:
    """Resolve Slack user ID to (display_name, username)."""
    if user_id in _user_cache:
        return _user_cache[user_id]
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
        _user_cache[user_id] = (display_name, username)
        return (display_name, username)
    except Exception as e:
        logger.error(f"Failed to resolve user {user_id}: {e}")
        return (user_id, user_id)


def get_channel_name(client, channel_id: str) -> str:
    """Resolve Slack channel ID to channel name."""
    if channel_id in _channel_cache:
        return _channel_cache[channel_id]
    try:
        result = client.conversations_info(channel=channel_id)
        name = result["channel"]["name"]
        _channel_cache[channel_id] = name
        return name
    except Exception as e:
        logger.error(f"Failed to resolve channel {channel_id}: {e}")
        return channel_id


def get_permalink(client, channel_id: str, message_ts: str) -> str:
    """Get permalink for a message."""
    try:
        result = client.chat_getPermalink(channel=channel_id, message_ts=message_ts)
        return result.get("permalink", "")
    except Exception:
        return ""


def get_parent_text(client, channel_id: str, thread_ts: str) -> str | None:
    """Get the text of the parent message in a thread."""
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


def process_files(files: list[dict]) -> list[str]:
    """Download files from Slack and upload to Google Drive. Returns list of Drive links."""
    links = []
    for file_info in files:
        link = drive.download_from_slack_and_upload(file_info, config.SLACK_BOT_TOKEN)
        if link:
            links.append(link)
    return links


@app.event("message")
def handle_message(event, client, logger):
    """Handle all message events (channel messages + thread replies)."""
    # Skip bot messages, message_changed, message_deleted etc.
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

    channel_name = get_channel_name(client, channel_id)
    display_name, username = get_user_info(client, user_id)
    permalink = get_permalink(client, channel_id, ts)

    # If this is a thread reply, get parent message text
    parent_text = None
    if thread_ts and thread_ts != ts:
        parent_text = get_parent_text(client, channel_id, thread_ts)

    # Process file attachments
    attachment_links = process_files(files) if files else []

    # Log to Google Sheets (deduplication handled inside)
    try:
        sheets.append_message(
            channel_name=channel_name,
            display_name=display_name,
            username=username,
            text=text,
            ts=ts,
            thread_ts=thread_ts,
            parent_text=parent_text,
            attachment_links=attachment_links,
            permalink=permalink,
        )
    except Exception as e:
        logger.error(f"Failed to log message to Sheets: {e}")


@app.event("file_shared")
def handle_file_shared(event, client, logger):
    """Handle file_shared events (files uploaded without a message)."""
    pass


def main():
    logger.info("Starting Slack Log Bot...")
    logger.info("Bot is listening for messages via Socket Mode.")
    handler = SocketModeHandler(app, config.SLACK_APP_TOKEN)
    handler.start()


if __name__ == "__main__":
    main()
