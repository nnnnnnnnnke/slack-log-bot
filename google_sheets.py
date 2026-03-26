"""Google Sheets handler for logging Slack messages."""

import logging
from datetime import datetime, timezone, timedelta

import gspread
from google.oauth2.service_account import Credentials

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

# メッセージTS が格納される列 (1-indexed, I列 = 9)
TS_COLUMN = 9

# Timezone offset for JST
JST = timezone(timedelta(hours=9))


class SheetsHandler:
    def __init__(self):
        creds = Credentials.from_service_account_file(
            config.GOOGLE_SERVICE_ACCOUNT_FILE, scopes=SCOPES
        )
        self.gc = gspread.authorize(creds)
        self.spreadsheet = self.gc.open_by_key(config.GOOGLE_SPREADSHEET_ID)
        self._sheet_cache: dict[str, gspread.Worksheet] = {}
        # Cache of existing message TS values per channel for deduplication
        self._existing_ts: dict[str, set[str]] = {}

    def _get_or_create_sheet(self, channel_name: str) -> gspread.Worksheet:
        """Get or create a worksheet for the given channel."""
        if channel_name in self._sheet_cache:
            return self._sheet_cache[channel_name]

        try:
            worksheet = self.spreadsheet.worksheet(channel_name)
        except gspread.exceptions.WorksheetNotFound:
            worksheet = self.spreadsheet.add_worksheet(
                title=channel_name, rows=1000, cols=len(HEADER_ROW)
            )
            worksheet.append_row(HEADER_ROW)
            worksheet.format("1", {"textFormat": {"bold": True}})
            logger.info(f"Created new sheet: {channel_name}")

        self._sheet_cache[channel_name] = worksheet
        return worksheet

    def _load_existing_ts(self, channel_name: str, worksheet: gspread.Worksheet) -> set[str]:
        """Load all existing message TS values from a worksheet for deduplication."""
        if channel_name in self._existing_ts:
            return self._existing_ts[channel_name]

        try:
            ts_values = worksheet.col_values(TS_COLUMN)
            # Skip header row
            existing = set(ts_values[1:]) if len(ts_values) > 1 else set()
        except Exception as e:
            logger.warning(f"Failed to load existing TS for #{channel_name}: {e}")
            existing = set()

        self._existing_ts[channel_name] = existing
        return existing

    def _ts_to_datetime(self, ts: str) -> str:
        """Convert Slack timestamp to readable datetime string."""
        try:
            dt = datetime.fromtimestamp(float(ts), tz=JST)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            return ts

    def append_message(
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
    ) -> bool:
        """Append a message row to the appropriate channel sheet.

        Returns True if the message was added, False if it was a duplicate.
        """
        worksheet = self._get_or_create_sheet(channel_name)

        # Deduplication: skip if this message TS already exists
        existing = self._load_existing_ts(channel_name, worksheet)
        if ts in existing:
            logger.debug(f"Skipping duplicate: #{channel_name} ts={ts}")
            return False

        parent_preview = ""
        if parent_text:
            parent_preview = parent_text[:100] + ("..." if len(parent_text) > 100 else "")

        row = [
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

        worksheet.append_row(row, value_input_option="USER_ENTERED")

        # Update the cache
        existing.add(ts)

        logger.info(
            f"Logged: #{channel_name} {display_name} (@{username}) ({self._ts_to_datetime(ts)})"
        )
        return True
