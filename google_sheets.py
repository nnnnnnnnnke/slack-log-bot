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

# Column indices (1-indexed)
TS_COLUMN = 9       # メッセージTS
THREAD_TS_COLUMN = 10  # スレッドTS

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
        """Build a row list from message data."""
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
    ) -> bool:
        """Insert a message, placing thread replies after their parent/siblings.

        Used by realtime bot (main.py).
        Returns True if added, False if duplicate.
        """
        worksheet = self._get_or_create_sheet(channel_name)

        existing = self._load_existing_ts(channel_name, worksheet)
        if ts in existing:
            logger.debug(f"Skipping duplicate: #{channel_name} ts={ts}")
            return False

        row = self._build_row(
            channel_name, display_name, username, text,
            ts, thread_ts, parent_text, attachment_links, permalink,
        )

        is_thread_reply = thread_ts and thread_ts != ts

        if is_thread_reply:
            # Find the last row belonging to the same thread and insert after it
            insert_pos = self._find_thread_insert_position(worksheet, thread_ts)
            if insert_pos:
                worksheet.insert_row(row, insert_pos, value_input_option="USER_ENTERED")
            else:
                # Parent not found (edge case), append at end
                worksheet.append_row(row, value_input_option="USER_ENTERED")
        else:
            worksheet.append_row(row, value_input_option="USER_ENTERED")

        existing.add(ts)
        logger.info(
            f"Logged: #{channel_name} {display_name} (@{username}) ({self._ts_to_datetime(ts)})"
        )
        return True

    def _find_thread_insert_position(self, worksheet: gspread.Worksheet, thread_ts: str) -> int | None:
        """Find the row number to insert a thread reply (after the last row in the same thread).

        Returns the row number (1-indexed) to insert at, or None if the thread parent wasn't found.
        """
        try:
            all_ts = worksheet.col_values(TS_COLUMN)
            all_thread_ts = worksheet.col_values(THREAD_TS_COLUMN)
        except Exception:
            return None

        # Pad to same length
        max_len = max(len(all_ts), len(all_thread_ts))
        all_ts += [""] * (max_len - len(all_ts))
        all_thread_ts += [""] * (max_len - len(all_thread_ts))

        last_match_row = None
        for i in range(1, max_len):  # skip header (index 0)
            # Match: parent message (ts == thread_ts) or sibling reply (thread_ts column == thread_ts)
            if all_ts[i] == thread_ts or all_thread_ts[i] == thread_ts:
                last_match_row = i + 1  # 1-indexed row number

        if last_match_row:
            return last_match_row + 1  # insert AFTER the last match

        return None

    def write_messages_grouped(
        self, channel_name: str, messages: list[dict]
    ) -> tuple[int, int]:
        """Write messages grouped by thread (parent + replies together).

        Used by batch collection scripts (collect_weekly.py, backfill.py).
        Messages are sorted so that each parent is followed by its thread replies.
        Returns (new_count, skip_count).
        """
        worksheet = self._get_or_create_sheet(channel_name)
        existing = self._load_existing_ts(channel_name, worksheet)

        # Deduplicate
        new_messages = [m for m in messages if m["ts"] not in existing]
        skip_count = len(messages) - len(new_messages)

        if not new_messages:
            return (0, skip_count)

        # Group by thread: key = thread_ts (or ts for standalone messages)
        threads: dict[str, list[dict]] = {}
        for msg in new_messages:
            group_key = msg.get("thread_ts") or msg["ts"]
            threads.setdefault(group_key, []).append(msg)

        # Sort each thread's messages by ts
        for group_key in threads:
            threads[group_key].sort(key=lambda m: float(m["ts"]))

        # Sort thread groups by the earliest ts in each group
        sorted_groups = sorted(threads.items(), key=lambda item: float(item[0]))

        # Build rows
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

        # Batch append all rows
        worksheet.append_rows(rows, value_input_option="USER_ENTERED")

        new_count = len(rows)
        logger.info(f"Wrote {new_count} messages (grouped) to #{channel_name}")
        return (new_count, skip_count)
