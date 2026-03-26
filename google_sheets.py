"""Google Sheets handler for logging Slack messages.

Public channels  → shared spreadsheet (anyone with link), tabs per channel
Private channels → separate spreadsheet per channel, shared with members only
"""

import logging
from datetime import datetime, timezone, timedelta

import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

import config

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

HEADER_ROW = [
    "日時",
    "チャンネル",
    "表示名",
    "ユーザー名",
    "メッセージ",
    "スレッド元メッセージ",
    "添付ファイル",
    "パーマリンク",
    "メッセージTS",
    "スレッドTS",
]

# Column indices (1-indexed)
TS_COLUMN = 9
THREAD_TS_COLUMN = 10

JST = timezone(timedelta(hours=9))


class SheetsHandler:
    def __init__(self):
        self._creds = Credentials.from_service_account_file(
            config.GOOGLE_SERVICE_ACCOUNT_FILE, scopes=SCOPES
        )
        self.gc = gspread.authorize(self._creds)
        self.drive_service = build("drive", "v3", credentials=self._creds)
        self.public_spreadsheet = self.gc.open_by_key(config.GOOGLE_SPREADSHEET_ID)
        self.drive_folder_id = config.GOOGLE_DRIVE_FOLDER_ID
        # Cache: channel_name -> Worksheet
        self._sheet_cache: dict[str, gspread.Worksheet] = {}
        # Cache: channel_name -> set of existing message TS
        self._existing_ts: dict[str, set[str]] = {}
        # Cache: channel_name -> Spreadsheet (for private channels)
        self._private_spreadsheets: dict[str, gspread.Spreadsheet] = {}

    # ── Public channel: tab in shared spreadsheet ──

    def _get_or_create_public_sheet(self, channel_name: str) -> gspread.Worksheet:
        """Get or create a worksheet tab in the shared public spreadsheet."""
        if channel_name in self._sheet_cache:
            return self._sheet_cache[channel_name]

        try:
            worksheet = self.public_spreadsheet.worksheet(channel_name)
        except gspread.exceptions.WorksheetNotFound:
            worksheet = self.public_spreadsheet.add_worksheet(
                title=channel_name, rows=1000, cols=len(HEADER_ROW)
            )
            worksheet.append_row(HEADER_ROW)
            worksheet.format("1", {"textFormat": {"bold": True}})
            logger.info(f"Created public sheet tab: {channel_name}")

        self._sheet_cache[channel_name] = worksheet
        return worksheet

    # ── Private channel: separate spreadsheet per channel ──

    def _get_or_create_private_spreadsheet(
        self, channel_name: str, member_emails: list[str]
    ) -> gspread.Spreadsheet:
        """Get or create a dedicated spreadsheet for a private channel."""
        if channel_name in self._private_spreadsheets:
            return self._private_spreadsheets[channel_name]

        ss_name = f"Slack Log - #{channel_name}"

        # Search for existing spreadsheet in Drive folder
        query = (
            f"name = '{ss_name}' and "
            f"'{self.drive_folder_id}' in parents and "
            f"mimeType = 'application/vnd.google-apps.spreadsheet' and "
            f"trashed = false"
        )
        results = self.drive_service.files().list(
            q=query, fields="files(id, name)", pageSize=1
        ).execute()
        files = results.get("files", [])

        if files:
            ss = self.gc.open_by_key(files[0]["id"])
        else:
            # Create new spreadsheet in the Drive folder
            ss = self.gc.create(ss_name, folder_id=self.drive_folder_id)

            # Share with channel members only (not "anyone")
            for email in member_emails:
                try:
                    ss.share(email, perm_type="user", role="reader", notify=False)
                except Exception as e:
                    logger.warning(f"Failed to share spreadsheet with {email}: {e}")

            logger.info(
                f"Created private spreadsheet: {ss_name} "
                f"(shared with {len(member_emails)} members)"
            )

        self._private_spreadsheets[channel_name] = ss
        return ss

    def _get_or_create_private_sheet(
        self, channel_name: str, member_emails: list[str]
    ) -> gspread.Worksheet:
        """Get the worksheet from a private channel's dedicated spreadsheet."""
        if channel_name in self._sheet_cache:
            return self._sheet_cache[channel_name]

        ss = self._get_or_create_private_spreadsheet(channel_name, member_emails)

        # Use the first (default) sheet, rename if needed
        worksheet = ss.sheet1
        if worksheet.title != channel_name:
            worksheet.update_title(channel_name)

        # Add header if sheet is empty
        if worksheet.row_count == 0 or not worksheet.row_values(1):
            worksheet.append_row(HEADER_ROW)
            worksheet.format("1", {"textFormat": {"bold": True}})

        self._sheet_cache[channel_name] = worksheet
        return worksheet

    # ── Shared logic ──

    def _get_worksheet(
        self, channel_name: str, is_private: bool = False, member_emails: list[str] | None = None
    ) -> gspread.Worksheet:
        """Get the appropriate worksheet for a channel."""
        if is_private:
            return self._get_or_create_private_sheet(channel_name, member_emails or [])
        return self._get_or_create_public_sheet(channel_name)

    def _load_existing_ts(self, channel_name: str, worksheet: gspread.Worksheet) -> set[str]:
        if channel_name in self._existing_ts:
            return self._existing_ts[channel_name]
        try:
            ts_values = worksheet.col_values(TS_COLUMN)
            existing = set(ts_values[1:]) if len(ts_values) > 1 else set()
        except Exception as e:
            logger.warning(f"Failed to load existing TS for #{channel_name}: {e}")
            existing = set()
        self._existing_ts[channel_name] = existing
        return existing

    def _ts_to_datetime(self, ts: str) -> str:
        try:
            dt = datetime.fromtimestamp(float(ts), tz=JST)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            return ts

    def _build_row(
        self,
        channel_name: str,
        display_name: str,
        username: str,
        text: str,
        ts: str,
        thread_ts: str | None,
        parent_text: str | None,
        attachment_links: list[str],
        permalink: str,
    ) -> list[str]:
        parent_preview = ""
        if parent_text:
            parent_preview = parent_text[:100] + ("..." if len(parent_text) > 100 else "")
        return [
            self._ts_to_datetime(ts),
            channel_name,
            display_name,
            f"@{username}",
            text,
            parent_preview,
            "\n".join(attachment_links),
            permalink,
            ts,
            thread_ts or "",
        ]

    # ── Realtime insert (main.py) ──

    def insert_message(
        self,
        channel_name: str,
        display_name: str,
        username: str,
        text: str,
        ts: str,
        thread_ts: str | None,
        parent_text: str | None,
        attachment_links: list[str],
        permalink: str,
        is_private: bool = False,
        member_emails: list[str] | None = None,
    ) -> bool:
        """Insert a message, placing thread replies after their parent/siblings."""
        worksheet = self._get_worksheet(channel_name, is_private, member_emails)

        existing = self._load_existing_ts(channel_name, worksheet)
        if ts in existing:
            return False

        row = self._build_row(
            channel_name, display_name, username, text,
            ts, thread_ts, parent_text, attachment_links, permalink,
        )

        is_thread_reply = thread_ts and thread_ts != ts

        if is_thread_reply:
            insert_pos = self._find_thread_insert_position(worksheet, thread_ts)
            if insert_pos:
                worksheet.insert_row(row, insert_pos, value_input_option="USER_ENTERED")
            else:
                worksheet.append_row(row, value_input_option="USER_ENTERED")
        else:
            worksheet.append_row(row, value_input_option="USER_ENTERED")

        existing.add(ts)
        logger.info(
            f"Logged: #{channel_name} {display_name} (@{username}) ({self._ts_to_datetime(ts)})"
        )
        return True

    def _find_thread_insert_position(self, worksheet: gspread.Worksheet, thread_ts: str) -> int | None:
        try:
            all_ts = worksheet.col_values(TS_COLUMN)
            all_thread_ts = worksheet.col_values(THREAD_TS_COLUMN)
        except Exception:
            return None

        max_len = max(len(all_ts), len(all_thread_ts))
        all_ts += [""] * (max_len - len(all_ts))
        all_thread_ts += [""] * (max_len - len(all_thread_ts))

        last_match_row = None
        for i in range(1, max_len):
            if all_ts[i] == thread_ts or all_thread_ts[i] == thread_ts:
                last_match_row = i + 1

        if last_match_row:
            return last_match_row + 1
        return None

    # ── Batch write (collect_weekly.py, backfill.py) ──

    def write_messages_grouped(
        self,
        channel_name: str,
        messages: list[dict],
        is_private: bool = False,
        member_emails: list[str] | None = None,
    ) -> tuple[int, int]:
        """Write messages grouped by thread."""
        worksheet = self._get_worksheet(channel_name, is_private, member_emails)
        existing = self._load_existing_ts(channel_name, worksheet)

        new_messages = [m for m in messages if m["ts"] not in existing]
        skip_count = len(messages) - len(new_messages)

        if not new_messages:
            return (0, skip_count)

        threads: dict[str, list[dict]] = {}
        for msg in new_messages:
            group_key = msg.get("thread_ts") or msg["ts"]
            threads.setdefault(group_key, []).append(msg)

        for group_key in threads:
            threads[group_key].sort(key=lambda m: float(m["ts"]))

        sorted_groups = sorted(threads.items(), key=lambda item: float(item[0]))

        rows = []
        for _group_key, group_msgs in sorted_groups:
            for msg in group_msgs:
                row = self._build_row(
                    channel_name=msg["channel_name"],
                    display_name=msg["display_name"],
                    username=msg["username"],
                    text=msg["text"],
                    ts=msg["ts"],
                    thread_ts=msg.get("thread_ts"),
                    parent_text=msg.get("parent_text"),
                    attachment_links=msg.get("attachment_links", []),
                    permalink=msg.get("permalink", ""),
                )
                rows.append(row)
                existing.add(msg["ts"])

        worksheet.append_rows(rows, value_input_option="USER_ENTERED")

        new_count = len(rows)
        logger.info(f"Wrote {new_count} messages (grouped) to #{channel_name}")
        return (new_count, skip_count)
