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
    get_channel_info,
    get_member_emails,
    get_user_info,
    resolve_mentions,
    install_retry_handlers,
    invalidate_channel,
    invalidate_members,
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
    if subtype in ("bot_message", "message_changed", "message_deleted", "channel_join", "channel_leave"):
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

    _, username, _ = get_user_info(client, user_id)
    text = resolve_mentions(client, text)

    # Log message immediately (without waiting for file uploads)
    try:
        sheets.insert_message(
            channel_name=channel_name,
            username=username,
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
    @bot share        → Re-sync the sheet's readers with the channel members
    @bot reset        → Backup & reset this channel's sheet
    @bot clear cache  → Clear in-memory caches
    """
    channel_id = event.get("channel", "")
    text = event.get("text", "")

    channel_name = get_channel_info(client, channel_id)["name"]

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

    elif cleaned.startswith("reset"):
        member_emails = get_member_emails(client, channel_id)
        say(f":recycle: `#{channel_name}` のシートをバックアップ＆リセットします...")

        def run_reset():
            try:
                backup_name = sheets.backup_and_reset_channel(
                    channel_name, member_emails, channel_id,
                )
                if backup_name:
                    client.chat_postMessage(
                        channel=channel_id,
                        text=f":white_check_mark: `#{channel_name}` をリセットしました。バックアップ: `{backup_name}`",
                    )
                else:
                    client.chat_postMessage(
                        channel=channel_id,
                        text=f":white_check_mark: `#{channel_name}` のシートが見つかりませんでした（既にクリーンな状態です）。",
                    )
            except Exception as e:
                logger.error(f"Reset failed: {e}")
                client.chat_postMessage(
                    channel=channel_id,
                    text=f":x: リセット中にエラーが発生しました: {e}",
                )

        threading.Thread(target=run_reset, daemon=True).start()

    elif cleaned in ("share", "sync", "共有"):
        say(f":arrows_counterclockwise: `#{channel_name}` の共有設定をメンバーに合わせます...")

        def run_share():
            try:
                granted, _ = _sync_access(client, channel_id, channel_name)
                emails = get_member_emails(client, channel_id)
                client.chat_postMessage(
                    channel=channel_id,
                    text=(
                        f":white_check_mark: `#{channel_name}` の共有を更新しました。"
                        f"\n新たに共有: {granted} 件 / 共有対象のメンバー: {len(emails)} 人"
                    ),
                )
            except Exception as e:
                logger.error(f"Share sync failed: {e}")
                client.chat_postMessage(
                    channel=channel_id,
                    text=f":x: 共有の更新でエラーが発生しました: {e}",
                )

        threading.Thread(target=run_share, daemon=True).start()

    elif cleaned in ("clear cache", "cache clear", "キャッシュクリア"):
        sheets.clear_cache()
        say(":broom: キャッシュをクリアしました。")

    elif cleaned == "url":
        url = sheets.get_spreadsheet_url(channel_name)
        if url:
            say(f":memo: `#{channel_name}` のログはこちら:\n{url}")
        else:
            say(f":memo: `#{channel_name}` のログはまだ作成されていません。メッセージが投稿されると自動的に作成されます。")

    elif cleaned == "help" or cleaned == "":
        url = sheets.get_spreadsheet_url(channel_name)
        url_line = f"\n:link: {url}" if url else ""
        say(
            f":memo: *`#{channel_name}` のログBot*{url_line}\n\n"
            f"*コマンド一覧:*\n"
            f"• `@Log Bot url` — スプレッドシートURL表示\n"
            f"• `@Log Bot backfill` — 過去90日分のログを収集\n"
            f"• `@Log Bot backfill 30` — 過去N日分を収集\n"
            f"• `@Log Bot share` — 共有設定をメンバーに合わせて更新\n"
            f"• `@Log Bot reset` — このチャンネルのシートをバックアップ＆リセット\n"
            f"• `@Log Bot clear cache` — キャッシュクリア"
        )

    else:
        say(":thinking_face: 不明なコマンドです。`@Log Bot help` でコマンド一覧を確認できます。")


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
