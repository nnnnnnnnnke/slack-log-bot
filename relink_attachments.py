"""Turn the raw Drive URLs already in the attachment column into linked names.

Rows written before the attachment cell held rich text show a 60-character
Drive URL where a file name would do. The names are fetched from Drive by id,
so nothing is guessed.

A file that has been deleted from Drive keeps its URL: without a name there is
nothing to put in its place, and dropping the link would lose the only record
that the message had an attachment.

Usage:
    python relink_attachments.py            # 対象を表示するだけ
    python relink_attachments.py --apply    # 実際に書き換える
"""

import argparse
import logging
import re
import sys
import time

import gspread
from googleapiclient.discovery import build

import config
from google_auth import load_credentials
from google_sheets import ATTACHMENT_COLUMN, HEADER_ROW, SheetsHandler, _retry

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

DRIVE_ID_PATTERN = re.compile(r"/d/([A-Za-z0-9_-]{20,})|[?&]id=([A-Za-z0-9_-]{20,})")
PAUSE_BETWEEN_SHEETS = 1.5


def file_ids_in(cell: str) -> list[str]:
    return [m.group(1) or m.group(2) for m in DRIVE_ID_PATTERN.finditer(cell)]


def lookup_names(drive, file_ids: set[str]) -> dict[str, str]:
    """Map Drive file ids to their names, skipping any that no longer exist."""
    names = {}
    for file_id in file_ids:
        try:
            names[file_id] = drive.files().get(fileId=file_id, fields="name").execute()["name"]
        except Exception as e:
            logger.warning(f"  ! {file_id}: 名前を取得できません ({str(e)[:60]})")
    return names


def main():
    parser = argparse.ArgumentParser(description="添付セルをファイル名リンクに変換")
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

    total_rows = 0
    for meta in sorted(files, key=lambda f: f["name"]):
        if not meta["name"].startswith("Slack Log - #"):
            continue
        spreadsheet = gc.open_by_key(meta["id"])
        for worksheet in _retry(spreadsheet.worksheets):
            if worksheet.col_count < len(HEADER_ROW):
                continue
            values = _retry(worksheet.get_all_values)
            if not values or values[0][: len(HEADER_ROW)] != HEADER_ROW:
                continue

            # Only rows whose attachment cell still holds a URL need touching.
            pending = []
            for row_number, row in enumerate(values[1:], start=2):
                cell = row[ATTACHMENT_COLUMN - 1] if len(row) >= ATTACHMENT_COLUMN else ""
                if "http" not in cell:
                    continue
                ids = file_ids_in(cell)
                if ids:
                    pending.append((row_number, ids))

            if not pending:
                continue

            all_ids = {i for _, ids in pending for i in ids}
            logger.info(f"\n{meta['name']} / {worksheet.title}: {len(pending)} 行 / {len(all_ids)} ファイル")

            if not args.apply:
                total_rows += len(pending)
                continue

            names = lookup_names(drive, all_ids)
            requests = []
            for row_number, ids in pending:
                attachments = [
                    (names[i], f"https://drive.google.com/file/d/{i}/view")
                    for i in ids
                    if i in names
                ]
                if not attachments:
                    continue
                requests.append(handler._attachment_cell_request(
                    worksheet.id, row_number, attachments
                ))

            if requests:
                _retry(spreadsheet.batch_update, {"requests": requests})
                total_rows += len(requests)
                logger.info(f"  {len(requests)} 行をファイル名リンクに変換しました")

        time.sleep(PAUSE_BETWEEN_SHEETS)

    logger.info(f"\n{'─' * 50}")
    if not total_rows:
        logger.info("変換が必要な行はありません。")
    elif args.apply:
        logger.info(f"変換完了: {total_rows} 行")
    else:
        logger.info(f"対象: {total_rows} 行\n実行するには --apply を付けてください。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
