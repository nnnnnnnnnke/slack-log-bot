"""Split the shared spreadsheet's channel tabs into one spreadsheet per channel.

Public channels used to be tabs in a single spreadsheet. Sheets grants access
per file, so a tab could not be handed to one group without handing over every
other channel in the same file. Each tab becomes its own spreadsheet, shared
with that channel's members.

The source tabs are left in place unless --remove-source is given, so the
migration can be checked before anything is thrown away.

Usage:
    python migrate_to_per_channel.py                  # 移行内容を表示するだけ
    python migrate_to_per_channel.py --apply          # 実行（元のタブは残す）
    python migrate_to_per_channel.py --apply --remove-source
"""

import argparse
import logging
import sys

import gspread
from slack_sdk import WebClient

import config
from google_auth import load_credentials
from google_sheets import HEADER_ROW, SheetsHandler
from sheet_guide import GUIDE_SHEET_TITLE, INDEX_SHEET_TITLE, write_channel_index
from slack_utils import get_member_emails, install_retry_handlers

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

SKIP_TITLES = {GUIDE_SHEET_TITLE, INDEX_SHEET_TITLE, "シート1", "Sheet1"}


def migratable_tabs(spreadsheet) -> list:
    """Channel tabs in the shared spreadsheet, excluding guides and backups."""
    tabs = []
    for worksheet in spreadsheet.worksheets():
        if worksheet.title in SKIP_TITLES or "_bak_" in worksheet.title:
            continue
        header = worksheet.row_values(1)
        if header[: len(HEADER_ROW)] != HEADER_ROW:
            logger.warning(f"  ! {worksheet.title}: ログの列構成ではないため除外")
            continue
        tabs.append(worksheet)
    return tabs


def channel_members(client, channel_name: str) -> list[str]:
    """Email addresses of a channel's members, or [] if it can't be resolved."""
    cursor = None
    while True:
        resp = client.conversations_list(
            types="public_channel,private_channel", limit=200, cursor=cursor
        )
        for ch in resp["channels"]:
            if ch["name"] == channel_name:
                return get_member_emails(client, ch["id"])
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            return []


def main():
    parser = argparse.ArgumentParser(description="チャンネルごとのスプレッドシートへ移行")
    parser.add_argument("--apply", action="store_true", help="実際に移行する")
    parser.add_argument(
        "--remove-source", action="store_true",
        help="移行後、共有スプレッドシートの元タブを削除する",
    )
    args = parser.parse_args()

    creds = load_credentials()
    gc = gspread.authorize(creds)
    source = gc.open_by_key(config.GOOGLE_SPREADSHEET_ID)

    tabs = migratable_tabs(source)
    if not tabs:
        logger.info("移行対象のタブはありません。")
        return 0

    logger.info(f"移行対象: {len(tabs)} タブ")
    for worksheet in tabs:
        logger.info(f"  {worksheet.title} ({max(len(worksheet.col_values(1)) - 1, 0)} 行)")

    if not args.apply:
        logger.info("\nこれは一覧表示のみです。実行するには --apply を付けてください。")
        return 0

    client = WebClient(token=config.SLACK_BOT_TOKEN)
    install_retry_handlers(client)
    sheets = SheetsHandler()

    migrated: dict[str, str] = {}
    for worksheet in tabs:
        channel_name = worksheet.title
        logger.info(f"\n{channel_name} を移行中...")

        emails = channel_members(client, channel_name)
        target = sheets._get_or_create_channel_spreadsheet(channel_name, emails)

        existing = next(
            (ws for ws in target.worksheets() if ws.title == channel_name), None
        )
        if existing:
            # Already copied on an earlier run. Still honour --remove-source, but
            # only once the copy is confirmed to hold everything the tab does.
            migrated[channel_name] = target.url
            source_rows = len(worksheet.col_values(1))
            target_rows = len(existing.col_values(1))
            if source_rows != target_rows:
                logger.warning(
                    f"  既に移行済みですが行数が一致しません "
                    f"(元 {source_rows} / 先 {target_rows})。元のタブは残します。"
                )
                continue
            logger.info(f"  既に移行済み ({source_rows - 1} 行が一致)")
            if args.remove_source:
                source.del_worksheet(worksheet)
                logger.info("  元のタブを削除しました")
            continue

        # copy_to preserves values and formatting in one call.
        copied = worksheet.copy_to(target.id)
        new_ws = target.get_worksheet_by_id(copied["sheetId"])
        new_ws.update_title(channel_name)

        source_rows = len(worksheet.col_values(1))
        target_rows = len(new_ws.col_values(1))
        if source_rows != target_rows:
            logger.error(
                f"  ! 行数が一致しません (元 {source_rows} / 先 {target_rows})。"
                f"このタブは元のまま残します。"
            )
            continue

        # Drop the empty default sheet the new spreadsheet came with.
        for ws in target.worksheets():
            if ws.id != new_ws.id and ws.title in ("シート1", "Sheet1"):
                try:
                    target.del_worksheet(ws)
                except Exception:
                    pass

        migrated[channel_name] = target.url
        logger.info(f"  {source_rows - 1} 行を移行 / {len(emails)} 人に共有")
        logger.info(f"  {target.url}")

        if args.remove_source:
            source.del_worksheet(worksheet)
            logger.info("  元のタブを削除しました")

    if migrated:
        write_channel_index(source, sheets._collect_index_entries())
        logger.info(f"\n「{INDEX_SHEET_TITLE}」タブを更新しました")

    logger.info(f"\n{'─' * 50}")
    logger.info(f"移行完了: {len(migrated)} チャンネル")
    if not args.remove_source:
        logger.info(
            "元のタブは残しています。内容を確認したら "
            "--apply --remove-source で削除できます。"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
