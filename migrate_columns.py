"""Rewrite existing sheets from the 9-column layout to the 7-column one.

The channel column repeated one value down every row once each channel got its
own spreadsheet, and the display name was a second spelling of the username.
Both are dropped.

This has to run before the bot writes again: dedup and thread placement read
the message TS by column number, and in the old layout that number points at a
different column.

Usage:
    python migrate_columns.py            # 対象を表示するだけ
    python migrate_columns.py --apply    # 実際に書き換える
"""

import argparse
import logging
import sys

import gspread
from googleapiclient.discovery import build

import config
from google_auth import load_credentials
from google_sheets import (
    HEADER_ROW,
    LEGACY_COLUMN_MAP,
    LEGACY_HEADER_ROW,
    SheetsHandler,
)

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


def convert(rows: list[list[str]]) -> list[list[str]]:
    """Keep only the surviving columns, padding rows that end early."""
    converted = []
    for row in rows:
        padded = row + [""] * (len(LEGACY_HEADER_ROW) - len(row))
        converted.append([padded[i] for i in LEGACY_COLUMN_MAP])
    return converted


def main():
    parser = argparse.ArgumentParser(description="シートを新しい列構成へ移行")
    parser.add_argument("--apply", action="store_true", help="実際に書き換える")
    args = parser.parse_args()

    creds = load_credentials()
    gc = gspread.authorize(creds)
    drive = build("drive", "v3", credentials=creds)
    handler = SheetsHandler()

    query = (
        f"'{config.GOOGLE_DRIVE_FOLDER_ID}' in parents and "
        f"mimeType = 'application/vnd.google-apps.spreadsheet' and trashed = false"
    )
    files = drive.files().list(
        q=query, fields="files(id, name)", pageSize=1000
    ).execute().get("files", [])

    targets = []
    for meta in sorted(files, key=lambda f: f["name"]):
        if not meta["name"].startswith("Slack Log - #"):
            continue
        spreadsheet = gc.open_by_key(meta["id"])
        for worksheet in spreadsheet.worksheets():
            if worksheet.col_count < len(LEGACY_HEADER_ROW):
                continue
            header = worksheet.row_values(1)
            if header[: len(LEGACY_HEADER_ROW)] == LEGACY_HEADER_ROW:
                targets.append((spreadsheet, worksheet))
            elif header[: len(HEADER_ROW)] == HEADER_ROW:
                logger.info(f"  {meta['name']} / {worksheet.title}: 移行済み")

    if not targets:
        logger.info("移行が必要なシートはありません。")
        return 0

    logger.info(f"\n移行対象: {len(targets)} タブ")
    for spreadsheet, worksheet in targets:
        rows = len(worksheet.col_values(1)) - 1
        logger.info(f"  {spreadsheet.title} / {worksheet.title} ({rows} 行)")

    if not args.apply:
        logger.info("\nこれは一覧表示のみです。実行するには --apply を付けてください。")
        return 0

    for spreadsheet, worksheet in targets:
        logger.info(f"\n{spreadsheet.title} を移行中...")
        old = worksheet.get_all_values()
        new = convert(old)
        new[0] = list(HEADER_ROW)

        if len(new) != len(old):
            logger.error("  ! 行数が変わりました。中断します。")
            continue

        worksheet.clear()
        worksheet.update(new, "A1", value_input_option="RAW")

        handler._format_sheet(worksheet, spreadsheet)
        # Thread replies lost their background when the sheet was cleared.
        thread_rows = [
            {"thread_ts": row[len(HEADER_ROW) - 1]} for row in new[1:]
        ]
        handler._format_thread_rows(worksheet, spreadsheet, 2, thread_rows)

        logger.info(f"  {len(new) - 1} 行を {len(HEADER_ROW)} 列に変換しました")

    logger.info(f"\n{'─' * 50}")
    logger.info(f"移行完了: {len(targets)} タブ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
