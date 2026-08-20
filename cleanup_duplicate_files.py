"""Trash the duplicate attachment copies left behind by repeated collection runs.

Every run downloaded and re-uploaded the attachments of messages it already
had. Their rows were deduped at write time, so only the first upload was ever
linked from a spreadsheet; the rest are orphans taking up Drive quota.

Files still linked from a spreadsheet are never touched, so the links in the
sheets keep resolving. Duplicates are moved to the Drive trash rather than
deleted, so a mistake is recoverable for 30 days.

Usage:
    python cleanup_duplicate_files.py            # 一覧を出すだけ (dry run)
    python cleanup_duplicate_files.py --apply    # 実際にゴミ箱へ移動
"""

import argparse
import logging
import re
import sys
from collections import defaultdict

import gspread

import config
from google_auth import load_credentials
from google_drive import drive_service, ATTACHMENTS_FOLDER_NAME, FOLDER_MIME
from google_sheets import ATTACHMENT_COLUMN, _retry

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

DRIVE_ID_PATTERN = re.compile(r"/d/([A-Za-z0-9_-]{20,})|[?&]id=([A-Za-z0-9_-]{20,})")


class IncompleteScan(RuntimeError):
    """Raised when the set of still-referenced files could not be built in full."""


def linked_file_ids(gc, drive, root_folder_id: str) -> set[str]:
    """Every Drive file id referenced from the bot's spreadsheets.

    Only the attachment column is read. Message bodies can contain Drive URLs
    a person pasted into Slack, which have nothing to do with the uploads.

    Any failure aborts. Deciding what to delete from a partial picture is how
    a rate-limited read turns into a deleted file that a sheet still links to.
    """
    ids: set[str] = set()
    query = (
        f"'{root_folder_id}' in parents and "
        f"mimeType = 'application/vnd.google-apps.spreadsheet' and trashed = false"
    )
    sheets = drive.files().list(
        q=query, fields="files(id, name)", pageSize=1000
    ).execute().get("files", [])

    for meta in sheets:
        try:
            spreadsheet = gc.open_by_key(meta["id"])
            for worksheet in _retry(spreadsheet.worksheets):
                # The guide tab is two columns wide; asking for column F there
                # is out of range rather than empty.
                if worksheet.col_count < ATTACHMENT_COLUMN:
                    continue
                for cell in _retry(worksheet.col_values, ATTACHMENT_COLUMN):
                    for match in DRIVE_ID_PATTERN.finditer(cell):
                        ids.add(match.group(1) or match.group(2))
        except Exception as e:
            raise IncompleteScan(
                f"{meta['name']} を読めませんでした: {e}\n"
                f"  参照中のファイルを取り違える恐れがあるため中断します。"
                f"時間をおいて再実行してください。"
            )

    logger.info(f"スプレッドシートから参照されているファイル: {len(ids)} 件")
    return ids


def channel_folders(drive, root_folder_id: str) -> list[dict]:
    attachments = drive.files().list(
        q=f"name = '{ATTACHMENTS_FOLDER_NAME}' and '{root_folder_id}' in parents "
          f"and mimeType = '{FOLDER_MIME}' and trashed = false",
        fields="files(id, name)", pageSize=1,
    ).execute().get("files", [])
    if not attachments:
        return []
    return drive.files().list(
        q=f"'{attachments[0]['id']}' in parents and mimeType = '{FOLDER_MIME}' "
          f"and trashed = false",
        fields="files(id, name)", pageSize=1000,
    ).execute().get("files", [])


def main():
    parser = argparse.ArgumentParser(description="重複した添付ファイルをゴミ箱へ移動")
    parser.add_argument("--apply", action="store_true", help="実際に移動する（既定は一覧のみ）")
    args = parser.parse_args()

    creds = load_credentials()
    drive = drive_service(creds)
    gc = gspread.authorize(creds)
    root = config.GOOGLE_DRIVE_FOLDER_ID

    try:
        linked = linked_file_ids(gc, drive, root)
    except IncompleteScan as e:
        logger.error(f"中断: {e}")
        return 1

    trashed_ids: list[str] = []
    folders = channel_folders(drive, root)
    if not folders:
        logger.info("添付フォルダが見つかりません。")
        return

    total_dupes = 0
    total_bytes = 0

    for folder in sorted(folders, key=lambda f: f["name"]):
        files = drive.files().list(
            q=f"'{folder['id']}' in parents and trashed = false",
            fields="files(id, name, size, createdTime)",
            orderBy="createdTime",
            pageSize=1000,
        ).execute().get("files", [])

        by_name: dict[str, list[dict]] = defaultdict(list)
        for f in files:
            by_name[f["name"]].append(f)

        removable: list[dict] = []
        for name, copies in by_name.items():
            if len(copies) < 2:
                continue
            # Keep everything a spreadsheet points at. If nothing is linked,
            # keep the oldest, which is the copy the first run would have
            # linked before the row was written.
            keep = {f["id"] for f in copies if f["id"] in linked}
            if not keep:
                keep = {copies[0]["id"]}
            removable.extend(f for f in copies if f["id"] not in keep)

        if not removable:
            continue

        size = sum(int(f.get("size", 0)) for f in removable)
        total_dupes += len(removable)
        total_bytes += size
        logger.info(f"\n{folder['name']}: 重複 {len(removable)} 件 ({size / 1024 / 1024:.1f} MB)")
        counts: dict[str, int] = defaultdict(int)
        for f in removable:
            counts[f["name"]] += 1
        for name, n in sorted(counts.items(), key=lambda kv: -kv[1])[:8]:
            logger.info(f"    {name} x{n}")
        if len(counts) > 8:
            logger.info(f"    ... 他 {len(counts) - 8} 種類")

        if args.apply:
            for f in removable:
                try:
                    drive.files().update(fileId=f["id"], body={"trashed": True}).execute()
                    trashed_ids.append(f["id"])
                except Exception as e:
                    logger.warning(f"    ! {f['name']} を移動できませんでした: {e}")

    logger.info(f"\n{'─' * 50}")
    if not total_dupes:
        logger.info("重複はありませんでした。")
        return

    logger.info(f"重複合計: {total_dupes} 件 / {total_bytes / 1024 / 1024:.1f} MB")
    if not args.apply:
        logger.info("これは一覧表示のみです。実行するには --apply を付けてください。")
        return

    # Belt and braces: confirm nothing a sheet points at ended up in the trash,
    # and put it back if it did.
    restored = 0
    for file_id in linked:
        try:
            if drive.files().get(fileId=file_id, fields="trashed").execute().get("trashed"):
                drive.files().update(fileId=file_id, body={"trashed": False}).execute()
                restored += 1
        except Exception:
            continue

    logger.info(f"ゴミ箱へ移動: {len(trashed_ids)} 件")
    if restored:
        logger.warning(
            f"参照中のファイル {restored} 件を誤って移動していたため元に戻しました。"
        )
    logger.info("30日以内なら Drive のゴミ箱から復元できます。")


if __name__ == "__main__":
    sys.exit(main())
