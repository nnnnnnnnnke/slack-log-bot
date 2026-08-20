"""Slack Log Bot - Collects messages and files from Slack and saves to Google Sheets/Drive."""

import logging
import re
import sys
import threading
from datetime import datetime, timedelta, timezone

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

import config
from google_sheets import SheetsHandler
from google_drive import DriveHandler
from collector import fetch_channel_messages
from slack_utils import (
    _user_info_cache,
    get_channel_info,
    get_member_emails,
    get_user_info,
    resolve_mentions,
    install_retry_handlers,
    invalidate_channel,
    invalidate_members,
    invalidate_user,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

app = App(token=config.SLACK_BOT_TOKEN)
install_retry_handlers(app.client)
sheets = SheetsHandler()
drive = DriveHandler()


_bot_user_ids: tuple[str, ...] | None = None
_bot_handle: str | None = None


def bot_handle(client) -> str:
    """How to address the bot, as Slack currently spells it.

    Read from Slack rather than hard-coded: the app can be renamed at any
    time, and help text naming a handle that no longer works is worse than
    no help text.
    """
    global _bot_handle
    if _bot_handle is None:
        try:
            user_id = client.auth_test().get("user_id", "")
            profile = client.users_info(user=user_id)["user"]
            _bot_handle = (
                profile.get("profile", {}).get("display_name")
                or profile.get("real_name")
                or profile.get("name")
                or "bot"
            )
        except Exception as e:
            logger.warning(f"Could not resolve the bot's own name: {e}")
            _bot_handle = "bot"
    return _bot_handle


def bot_user_ids(client) -> tuple[str, ...]:
    """The bot's own user id, so its replies stay out of the log.

    Messages posted with a bot token carry no subtype, so the bot_message
    filter never sees them and its "collection started" notices end up
    recorded as conversation.
    """
    global _bot_user_ids
    if _bot_user_ids is None:
        try:
            _bot_user_ids = (client.auth_test().get("user_id", ""),)
        except Exception as e:
            logger.warning(f"Could not resolve the bot's own user id: {e}")
            _bot_user_ids = ()
    return _bot_user_ids


def process_files(
    files: list[dict], channel_name: str, member_emails: list[str] | None,
    channel_id: str,
) -> list[str]:
    links = []
    for file_info in files:
        link = drive.download_from_slack_and_upload(
            file_info, config.SLACK_BOT_TOKEN, channel_name,
            member_emails, channel_id,
        )
        if link:
            links.append(link)
    return links


@app.event("message")
def handle_message(event, client, logger):
    subtype = event.get("subtype")
    if subtype == "message_changed":
        handle_message_edited(event, client, logger)
        return
    if subtype in ("bot_message", "message_deleted", "channel_join", "channel_leave"):
        return

    channel_id = event.get("channel", "")
    user_id = event.get("user", "")
    text = event.get("text", "")
    ts = event.get("ts", "")
    thread_ts = event.get("thread_ts")
    files = event.get("files", [])

    if not user_id or user_id in bot_user_ids(client):
        return

    channel_name = get_channel_info(client, channel_id)["name"]
    # The channel's sheet is shared with exactly this channel's members.
    member_emails = get_member_emails(client, channel_id)

    display_name, _, _ = get_user_info(client, user_id)
    text = resolve_mentions(client, text)

    # Log message immediately (without waiting for file uploads)
    try:
        sheets.insert_message(
            channel_name=channel_name,
            display_name=display_name,
            text=text,
            ts=ts,
            thread_ts=thread_ts,
            attachments=[],
            member_emails=member_emails,
            channel_id=channel_id,
        )
    except Exception as e:
        logger.error(f"Failed to log message to Sheets: {e}")

    # Upload files in background and update attachment column
    if files:
        def upload_files():
            links = process_files(files, channel_name, member_emails, channel_id)
            if links:
                sheets.update_attachment_links(
                    channel_name, ts, links, member_emails, channel_id,
                )

        threading.Thread(target=upload_files, daemon=True).start()


def handle_message_edited(event, client, logger):
    """Follow an edit made in Slack, so the row keeps saying what the message says."""
    edited = event.get("message") or {}
    ts = edited.get("ts", "")
    author = edited.get("user", "")
    if not ts or not author or author in bot_user_ids(client):
        return

    channel_id = event.get("channel", "")
    channel_name = get_channel_info(client, channel_id)["name"]
    try:
        sheets.update_message(
            channel_name,
            ts,
            resolve_mentions(client, edited.get("text", "")),
            get_member_emails(client, channel_id),
            channel_id,
        )
    except Exception as e:
        logger.error(f"Failed to apply an edit in #{channel_name}: {e}")


@app.event("user_change")
def handle_user_change(event, client, logger):
    """Follow a display name change across the rows already written.

    Slack shows a renamed person's current name on their old messages; leaving
    the sheet on the old one would split one person into two, each with their
    own colour.
    """
    user = event.get("user") or {}
    user_id = user.get("id", "")
    if not user_id:
        return

    previous = _user_info_cache.get(user_id)
    invalidate_user(user_id)
    if previous is None:
        return

    old_name = previous[0]
    new_name, _, _ = get_user_info(client, user_id)
    if not new_name or new_name == old_name:
        return

    def run_rename():
        try:
            rows = sheets.rename_author(old_name, new_name)
            logger.info(f"Display name {old_name} -> {new_name} ({rows} row(s))")
        except Exception as e:
            logger.error(f"Failed to follow the rename of {user_id}: {e}")

    threading.Thread(target=run_rename, daemon=True).start()


@app.event("file_shared")
def handle_file_shared(event, client, logger):
    pass


def _sync_access(client, channel_id: str, channel_name: str, revoke: list[str] | None = None):
    """Bring the channel's spreadsheet and folder in line with its membership."""
    invalidate_members(channel_id)
    member_emails = get_member_emails(client, channel_id)
    granted = revoked = 0
    for handler in (sheets, drive):
        try:
            g, r = handler.sync_channel_access(
                channel_name, member_emails, revoke, channel_id
            )
            granted += g
            revoked += r
        except Exception as e:
            logger.error(f"Failed to sync access for #{channel_name}: {e}")
    return granted, revoked


@app.event("member_joined_channel")
def handle_member_joined(event, client, logger):
    """Share the channel's log with whoever just joined."""
    channel_id = event.get("channel", "")
    user_id = event.get("user", "")
    if not channel_id or not user_id:
        return

    channel_name = get_channel_info(client, channel_id)["name"]
    _, _, email = get_user_info(client, user_id)
    if not email:
        # Bots have no email, and neither do accounts the token cannot read.
        logger.info(f"Joined #{channel_name}: {user_id} has no email, nothing to share")
        return

    granted, _ = _sync_access(client, channel_id, channel_name)
    if granted:
        logger.info(f"Shared #{channel_name} with {granted} new member(s)")


@app.event("member_left_channel")
def handle_member_left(event, client, logger):
    """Revoke the channel's log from whoever just left."""
    channel_id = event.get("channel", "")
    user_id = event.get("user", "")
    if not channel_id or not user_id:
        return

    channel_name = get_channel_info(client, channel_id)["name"]
    _, _, email = get_user_info(client, user_id)
    if not email:
        return

    _, revoked = _sync_access(client, channel_id, channel_name, revoke=[email])
    if revoked:
        logger.info(f"Revoked {email} from #{channel_name}")


@app.event("channel_rename")
@app.event("group_rename")
def handle_channel_rename(event, client, logger):
    """Keep the channel's spreadsheet and folder with it when it is renamed."""
    channel = event.get("channel") or {}
    channel_id, new_name = channel.get("id", ""), channel.get("name", "")
    if not channel_id or not new_name:
        return

    invalidate_channel(channel_id)
    moved = False
    for handler in (sheets, drive):
        try:
            moved |= handler.rename_channel(channel_id, new_name)
        except Exception as e:
            logger.error(f"Failed to follow the rename of {channel_id}: {e}")
    if moved:
        logger.info(f"Followed channel rename to #{new_name}")


@app.event("app_mention")
def handle_mention(event, client, say, logger):
    """Handle @bot mentions. Commands:
    @bot              → Show spreadsheet URL + help
    @bot backfill     → Collect past messages (default 90 days)
    @bot backfill 30  → Collect past 30 days
    """
    channel_id = event.get("channel", "")
    text = event.get("text", "")

    channel_name = get_channel_info(client, channel_id)["name"]

    # Strip the mention to leave the command. Slack usually sends <@U012ABC>,
    # but the labelled form <@U012ABC|name> also occurs; without the optional
    # label the id stayed in the string and every command read as unknown.
    cleaned = re.sub(r"<@[A-Za-z0-9]+(\|[^>]*)?>", "", text).strip().lower()

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
                _backfill_channel(client, channel_id, channel_name, days)
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

    elif cleaned == "url":
        url = sheets.get_spreadsheet_url(channel_name)
        if url:
            say(f":memo: `#{channel_name}` のログはこちら:\n{url}")
        else:
            say(f":memo: `#{channel_name}` のログはまだ作成されていません。メッセージが投稿されると自動的に作成されます。")

    elif cleaned == "help" or cleaned == "":
        me = bot_handle(client)
        url = sheets.get_spreadsheet_url(channel_name)
        url_line = f"\n:link: {url}" if url else ""
        say(
            f":memo: *`#{channel_name}` のログBot*{url_line}\n\n"
            f"*コマンド一覧:*\n"
            f"• `@{me} url` — スプレッドシートURL表示\n"
            f"• `@{me} backfill` — 過去90日分のログを収集\n"
            f"• `@{me} backfill 30` — 過去N日分を収集"
        )

    else:
        say(
            f":thinking_face: 不明なコマンドです。"
            f"`@{bot_handle(client)} help` でコマンド一覧を確認できます。"
        )


def _backfill_channel(client, channel_id: str, channel_name: str, days: int):
    """Collect past messages for a single channel."""
    member_emails = get_member_emails(client, channel_id)

    # Attachments of messages already recorded would be downloaded and
    # re-uploaded on every run: the rows get deduped at write time, the Drive
    # files do not, so each run leaves another copy behind.
    known_ts = sheets.recorded_ts(channel_name, member_emails, channel_id)

    oldest = datetime.now(timezone.utc) - timedelta(days=days)
    oldest_ts = str(oldest.timestamp())

    collected = fetch_channel_messages(
        client, drive, channel_id, channel_name, oldest_ts, known_ts,
        member_emails, skip_user_ids=bot_user_ids(client),
    )

    new_count, skip_count = sheets.write_messages_grouped(
        channel_name, collected, member_emails, channel_id,
    )
    logger.info(f"Backfill #{channel_name}: {new_count} new, {skip_count} skipped")


def main():
    logger.info("Starting Slack Log Bot...")
    logger.info("Bot is listening for messages via Socket Mode.")
    handler = SocketModeHandler(app, config.SLACK_APP_TOKEN)
    handler.start()


if __name__ == "__main__":
    main()
