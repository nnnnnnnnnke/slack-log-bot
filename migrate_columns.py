"""Rewrite existing sheets from the 9-column layout to the 7-column one.

The channel column repeated one value down every row once each channel got its
own spreadsheet, and the display name was a second spelling of the username.
Both are dropped.

This has to run before the bot writes again: dedup and thread placement read
the message TS by column number, and in the old layout that number points at a
different column.

Safe to re-run — sheets already converted are skipped — so a run that stops
part way can simply be started again.

Usage:
    python migrate_columns.py            # 対象を表示するだけ
    python migrate_columns.py --apply    # 実際に書き換える
"""

import argparse
import logging
import re
import sys
import time

import gspread

import config
from google_drive import drive_service
from google_auth import load_credentials
from google_sheets import (
    HEADER_ROW,
    LEGACY_LAYOUTS,
    MESSAGE_COLUMN,
    SheetsHandler,
    _retry,
)

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

# Sheets allows 60 reads/minute/user, and this walks every tab of every
# spreadsheet. _retry backs off when the limit is hit anyway; pausing between
# spreadsheets keeps it from getting there in the first place.
PAUSE_BETWEEN_SHEETS = 1.5


TS_PATTERN = re.compile(r"^\d{9,11}\.\d{4,8}$")

CURRENT_TS_INDEX = HEADER_ROW.index("メッセージTS")


def _already_current(padded: list[str]) -> bool:
    """Whether a row is already in the current layout.

    A sheet can hold both: a bot writing while the migration is part way
    through leaves current-layout rows in an old-layout sheet, and mapping
    those again shifts every value along. The TS column is the tell — no other
    column holds something shaped like a Slack timestamp at that position.
    """
    return bool(TS_PATTERN.match(padded[CURRENT_TS_INDEX] or ""))


def convert(rows: list[list[str]], column_map: list[int], width: int) -> list[list[str]]:
    """Keep only the surviving columns, padding rows that end early."""
    converted = []
    for row in rows:
        padded = row + [""] * (max(width, len(HEADER_ROW)) - len(row))
        if _already_current(padded):
            converted.append(padded[: len(HEADER_ROW)])
        else:
            converted.append([padded[i] for i in column_map])
    return converted


def find_targets(gc, drive) -> list[tuple]:
    """Tabs still on the old layout, with their contents already read.

    Values are kept from this pass so the conversion does not read them again;
    at 60 reads a minute, reading everything twice is what trips the limit.
    """
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
        for worksheet in _retry(spreadsheet.worksheets):
            if worksheet.col_count < len(HEADER_ROW):
                continue
            values = _retry(worksheet.get_all_values)
            header = values[0] if values else []
            if header[: len(HEADER_ROW)] == HEADER_ROW:
                logger.info(f"  {meta['name']} / {worksheet.title}: 移行済み")
                continue
            for legacy_header, column_map in LEGACY_LAYOUTS:
                if header[: len(legacy_header)] == legacy_header:
                    targets.append(
                        (spreadsheet, worksheet, values, column_map, len(legacy_header))
                    )
                    break
            else:
                if header:
                    logger.warning(
                        f"  ! {meta['name']} / {worksheet.title}: "
                        f"見覚えのない列構成のため除外 ({header[:3]}...)"
                    )
        time.sleep(PAUSE_BETWEEN_SHEETS)
    return targets


def main():
    parser = argparse.ArgumentParser(description="シートを新しい列構成へ移行")
    parser.add_argument("--apply", action="store_true", help="実際に書き換える")
    args = parser.parse_args()

    creds = load_credentials()
    gc = gspread.authorize(creds)
    drive = drive_service(creds)
    handler = SheetsHandler()

    targets = find_targets(gc, drive)
    if not targets:
        logger.info("移行が必要なシートはありません。")
        return 0

    logger.info(f"\n移行対象: {len(targets)} タブ")
    for spreadsheet, worksheet, values, _map, width in targets:
        logger.info(
            f"  {spreadsheet.title} / {worksheet.title} "
            f"({len(values) - 1} 行, {width}列 → {len(HEADER_ROW)}列)"
        )

    if not args.apply:
        logger.info("\nこれは一覧表示のみです。実行するには --apply を付けてください。")
        return 0

    failed = 0
    for spreadsheet, worksheet, old, column_map, width in targets:
        logger.info(f"\n{spreadsheet.title} を移行中...")
        try:
            new = convert(old, column_map, width)
            new[0] = list(HEADER_ROW)

            _retry(worksheet.clear, idempotent=False)
            _retry(worksheet.update, new, "A1", value_input_option="RAW", idempotent=False)

            handler._format_sheet(worksheet, spreadsheet)
            # Heights and thread backgrounds were lost when the sheet was cleared.
            handler.apply_row_heights(
                worksheet, spreadsheet, 2,
                [row[MESSAGE_COLUMN - 1] for row in new[1:]],
            )
            thread_rows = [{"thread_ts": row[len(HEADER_ROW) - 1]} for row in new[1:]]
            handler._format_thread_rows(worksheet, spreadsheet, 2, thread_rows)

            logger.info(f"  {len(new) - 1} 行を {len(HEADER_ROW)} 列に変換しました")
        except Exception as e:
            failed += 1
            logger.error(f"  ! 失敗: {e}")
            logger.error("    このタブは元のままです。再実行してください。")
        time.sleep(PAUSE_BETWEEN_SHEETS)

    logger.info(f"\n{'─' * 50}")
    logger.info(f"移行完了: {len(targets) - failed} / {len(targets)} タブ")
    if failed:
        logger.warning("失敗したタブがあります。少し待ってから再実行してください。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
