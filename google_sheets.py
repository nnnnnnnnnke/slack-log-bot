"""Google Sheets handler for logging Slack messages.

Public channels  → shared spreadsheet (anyone with link), tabs per channel
Private channels → separate spreadsheet per channel, shared with members only
"""

import logging
import re
import threading
import time
from datetime import datetime, timezone, timedelta

import gspread
from googleapiclient.discovery import build

import config
from google_auth import load_credentials
from sheet_guide import ensure_guide_sheet

logger = logging.getLogger(__name__)

# Sheets allows 60 read + 60 write requests per minute per user. A busy
# workspace blows through that, so every API call gets backed off and retried.
MAX_RETRIES = 5
INITIAL_BACKOFF = 2.0
# 429 means the request was rejected outright, so retrying can never duplicate
# a write. 5xx is ambiguous, so it is only retried for idempotent operations.
RATE_LIMIT_STATUS = (429,)
TRANSIENT_STATUS = (429, 500, 502, 503, 504)


def _retry(fn, *args, idempotent: bool = True, **kwargs):
    """Call a gspread/Sheets operation, backing off on rate limits."""
    retry_on = TRANSIENT_STATUS if idempotent else RATE_LIMIT_STATUS
    delay = INITIAL_BACKOFF
    for attempt in range(MAX_RETRIES):
        try:
            return fn(*args, **kwargs)
        except gspread.exceptions.APIError as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status not in retry_on or attempt == MAX_RETRIES - 1:
                raise
            logger.warning(
                f"Sheets API {status} on {getattr(fn, '__name__', fn)}, "
                f"retrying in {delay:.0f}s ({attempt + 1}/{MAX_RETRIES - 1})"
            )
            time.sleep(delay)
            delay *= 2


def _appended_row_number(response) -> int | None:
    """Extract the row number a write landed on from a Sheets API response.

    The response carries updatedRange like "'general'!A42:I42"; worksheet.row_count
    is the size of the grid (1000 by default), not the last populated row.
    """
    try:
        updated_range = response["updates"]["updatedRange"]
    except (TypeError, KeyError):
        return None
    first_cell = updated_range.split("!")[-1].split(":")[0]
    digits = re.sub(r"[^0-9]", "", first_cell)
    return int(digits) if digits else None

HEADER_ROW = [
    "日時",
    "チャンネル",
    "表示名",
    "ユーザー名",
    "メッセージ",
    "添付ファイル",
    "パーマリンク",
    "メッセージTS",
    "スレッドTS",
]

# Column indices (1-indexed)
ATTACHMENT_COLUMN = 6
TS_COLUMN = 8
THREAD_TS_COLUMN = 9

# Column widths (pixels)
COLUMN_WIDTHS = [155, 100, 120, 120, 500, 200, 100, 140, 140]

# Colors (RGB 0-1 float)
COLOR_HEADER_BG = {"red": 0.118, "green": 0.557, "blue": 0.243}   # #1e8e3e Green
COLOR_HEADER_FG = {"red": 1.0, "green": 1.0, "blue": 1.0}         # white
COLOR_THREAD_BG = {"red": 0.902, "green": 0.957, "blue": 0.918}   # #e6f4ea Light Green
COLOR_TS_FG = {"red": 0.55, "green": 0.55, "blue": 0.55}          # #8C8C8C gray

JST = timezone(timedelta(hours=9))

# Thread reply prefix
THREAD_PREFIX = "└ "


class SheetsHandler:
    def __init__(self):
        creds = load_credentials()
        self.gc = gspread.authorize(creds)
        self.public_spreadsheet = self.gc.open_by_key(config.GOOGLE_SPREADSHEET_ID)
        self._drive = build("drive", "v3", credentials=creds)

        self.drive_folder_id = config.GOOGLE_DRIVE_FOLDER_ID
        self._sheet_cache: dict[str, gspread.Worksheet] = {}
        self._existing_ts: dict[str, set[str]] = {}
        self._private_spreadsheets: dict[str, gspread.Spreadsheet] = {}
        self._formatted_sheets: set[str] = set()
        # Bolt dispatches events concurrently, and backfills run on their own
        # thread. Without this, two writers can interleave their read-then-append
        # and duplicate rows or clobber each other's insert positions.
        self._lock = threading.RLock()

        # Covers spreadsheets created before the guide existed. It is skipped
        # once present, and must never stop the bot from starting.
        try:
            if ensure_guide_sheet(self.public_spreadsheet):
                logger.info("Created guide sheet in the shared spreadsheet")
        except Exception as e:
            logger.warning(f"Could not create guide sheet: {e}")

    # ── Sheet formatting ──

    def _format_sheet(self, worksheet: gspread.Worksheet, spreadsheet: gspread.Spreadsheet):
        """Apply visual formatting to a worksheet (called once on creation)."""
        sheet_id = worksheet.id

        requests = []

        # 1. Freeze header row
        requests.append({
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sheet_id,
                    "gridProperties": {"frozenRowCount": 1},
                },
                "fields": "gridProperties.frozenRowCount",
            }
        })

        # 2. Column widths
        for i, width in enumerate(COLUMN_WIDTHS):
            requests.append({
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "COLUMNS",
                        "startIndex": i,
                        "endIndex": i + 1,
                    },
                    "properties": {"pixelSize": width},
                    "fields": "pixelSize",
                }
            })

        # 3. Header row: background color + white bold text + center aligned
        requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 0,
                    "endRowIndex": 1,
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": COLOR_HEADER_BG,
                        "textFormat": {
                            "bold": True,
                            "foregroundColor": COLOR_HEADER_FG,
                            "fontSize": 10,
                        },
                        "horizontalAlignment": "CENTER",
                        "verticalAlignment": "MIDDLE",
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)",
            }
        })

        # 4. Header row height
        requests.append({
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "ROWS",
                    "startIndex": 0,
                    "endIndex": 1,
                },
                "properties": {"pixelSize": 36},
                "fields": "pixelSize",
            }
        })

        # 5. Text wrapping + larger font on message column (E)
        requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 1,
                    "startColumnIndex": 4,
                    "endColumnIndex": 5,
                },
                "cell": {
                    "userEnteredFormat": {
                        "wrapStrategy": "WRAP",
                        "verticalAlignment": "TOP",
                        "textFormat": {"fontSize": 11},
                    }
                },
                "fields": "userEnteredFormat(wrapStrategy,verticalAlignment,textFormat.fontSize)",
            }
        })

        # 6. TS columns (H, I): gray smaller font
        requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 1,
                    "startColumnIndex": 7,
                    "endColumnIndex": 9,
                },
                "cell": {
                    "userEnteredFormat": {
                        "textFormat": {
                            "foregroundColor": COLOR_TS_FG,
                            "fontSize": 8,
                        },
                    }
                },
                "fields": "userEnteredFormat.textFormat",
            }
        })

        # 7. Default vertical alignment for all data cells
        requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 1,
                    "startColumnIndex": 0,
                    "endColumnIndex": 4,
                },
                "cell": {
                    "userEnteredFormat": {"verticalAlignment": "MIDDLE"}
                },
                "fields": "userEnteredFormat.verticalAlignment",
            }
        })

        # 8. Set basic filter (auto-filter) on header
        requests.append({
            "setBasicFilter": {
                "filter": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": 0,
                        "startColumnIndex": 0,
                        "endColumnIndex": len(HEADER_ROW),
                    }
                }
            }
        })

        try:
            _retry(spreadsheet.batch_update, {"requests": requests})
            logger.info(f"Applied formatting to sheet: {worksheet.title}")
        except Exception as e:
            logger.warning(f"Failed to apply formatting: {e}")

    def _format_thread_rows(
        self,
        worksheet: gspread.Worksheet,
        spreadsheet: gspread.Spreadsheet,
        start_row: int,
        rows_data: list[dict],
    ):
        """Apply background color to thread reply rows after batch write."""
        sheet_id = worksheet.id
        requests = []

        for i, msg in enumerate(rows_data):
            if msg.get("thread_ts"):
                row_index = start_row + i - 1  # 0-indexed
                requests.append({
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": row_index,
                            "endRowIndex": row_index + 1,
                            "startColumnIndex": 0,
                            "endColumnIndex": len(HEADER_ROW),
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "backgroundColor": COLOR_THREAD_BG,
                            }
                        },
                        "fields": "userEnteredFormat.backgroundColor",
                    }
                })

        if requests:
            try:
                _retry(spreadsheet.batch_update, {"requests": requests})
            except Exception as e:
                logger.warning(f"Failed to format thread rows: {e}")

    def _format_single_thread_row(
        self,
        worksheet: gspread.Worksheet,
        spreadsheet: gspread.Spreadsheet,
        row_number: int,
    ):
        """Apply thread reply background to a single row (for realtime insert)."""
        sheet_id = worksheet.id
        row_index = row_number - 1  # 0-indexed
        try:
            _retry(spreadsheet.batch_update, {"requests": [{
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": row_index,
                        "endRowIndex": row_index + 1,
                        "startColumnIndex": 0,
                        "endColumnIndex": len(HEADER_ROW),
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": COLOR_THREAD_BG,
                        }
                    },
                    "fields": "userEnteredFormat.backgroundColor",
                }
            }]})
        except Exception as e:
            logger.warning(f"Failed to format thread row: {e}")

    # ── Public channel: tab in shared spreadsheet ──

    def _get_or_create_public_sheet(self, channel_name: str) -> gspread.Worksheet:
        if channel_name in self._sheet_cache:
            return self._sheet_cache[channel_name]

        is_new = False
        try:
            worksheet = self.public_spreadsheet.worksheet(channel_name)
        except gspread.exceptions.WorksheetNotFound:
            worksheet = _retry(
                self.public_spreadsheet.add_worksheet,
                title=channel_name, rows=1000, cols=len(HEADER_ROW),
                idempotent=False,
            )
            _retry(worksheet.append_row, HEADER_ROW, idempotent=False)
            is_new = True
            logger.info(f"Created public sheet tab: {channel_name}")

        if is_new or channel_name not in self._formatted_sheets:
            self._format_sheet(worksheet, self.public_spreadsheet)
            self._formatted_sheets.add(channel_name)

        self._sheet_cache[channel_name] = worksheet
        return worksheet

    # ── Private channel: separate spreadsheet per channel ──

    def _get_or_create_private_spreadsheet(
        self, channel_name: str, member_emails: list[str]
    ) -> gspread.Spreadsheet:
        if channel_name in self._private_spreadsheets:
            return self._private_spreadsheets[channel_name]

        ss_name = f"Slack Log - #{channel_name}"

        query = (
            f"name = '{ss_name}' and "
            f"'{self.drive_folder_id}' in parents and "
            f"mimeType = 'application/vnd.google-apps.spreadsheet' and "
            f"trashed = false"
        )
        results = self._drive.files().list(
            q=query, fields="files(id, name)", pageSize=1
        ).execute()
        files = results.get("files", [])

        if files:
            ss = self.gc.open_by_key(files[0]["id"])
        else:
            ss = self.gc.create(ss_name, folder_id=self.drive_folder_id)

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
        if channel_name in self._sheet_cache:
            return self._sheet_cache[channel_name]

        ss = self._get_or_create_private_spreadsheet(channel_name, member_emails)

        worksheet = ss.sheet1
        if worksheet.title != channel_name:
            _retry(worksheet.update_title, channel_name, idempotent=False)

        is_new = False
        if worksheet.row_count == 0 or not _retry(worksheet.row_values, 1):
            _retry(worksheet.append_row, HEADER_ROW, idempotent=False)
            is_new = True

        if is_new or channel_name not in self._formatted_sheets:
            self._format_sheet(worksheet, ss)
            self._formatted_sheets.add(channel_name)

        self._sheet_cache[channel_name] = worksheet
        return worksheet

    # ── Shared logic ──

    def _get_worksheet(
        self, channel_name: str, is_private: bool = False, member_emails: list[str] | None = None
    ) -> gspread.Worksheet:
        if is_private:
            return self._get_or_create_private_sheet(channel_name, member_emails or [])
        return self._get_or_create_public_sheet(channel_name)

    def _get_spreadsheet(self, channel_name: str, is_private: bool = False) -> gspread.Spreadsheet:
        """Get the Spreadsheet object for formatting API calls."""
        if is_private and channel_name in self._private_spreadsheets:
            return self._private_spreadsheets[channel_name]
        return self.public_spreadsheet

    def _load_existing_ts(self, channel_name: str, worksheet: gspread.Worksheet) -> set[str]:
        if channel_name in self._existing_ts:
            return self._existing_ts[channel_name]
        try:
            ts_values = _retry(worksheet.col_values, TS_COLUMN)
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
        attachment_links: list[str],
        permalink: str,
    ) -> list[str]:
        # Thread replies get a visual prefix
        is_reply = thread_ts and thread_ts != ts
        display_text = f"{THREAD_PREFIX}{text}" if is_reply else text

        return [
            self._ts_to_datetime(ts),
            channel_name,
            display_name,
            f"@{username}",
            display_text,
            "\n".join(attachment_links),
            permalink,
            ts,
            thread_ts or "",
        ]

    # ── Backup / Reset / Cache ──

    def clear_cache(self, channel_name: str | None = None):
        """Clear in-memory caches. If channel_name is None, clear all."""
        with self._lock:
            self._clear_cache_locked(channel_name)

    def _clear_cache_locked(self, channel_name: str | None = None):
        if channel_name:
            self._sheet_cache.pop(channel_name, None)
            self._existing_ts.pop(channel_name, None)
            self._private_spreadsheets.pop(channel_name, None)
            self._formatted_sheets.discard(channel_name)
        else:
            self._sheet_cache.clear()
            self._existing_ts.clear()
            self._private_spreadsheets.clear()
            self._formatted_sheets.clear()

    def backup_and_reset_channel(
        self, channel_name: str, is_private: bool = False, member_emails: list[str] | None = None,
    ) -> str | None:
        """Backup the channel tab, delete it, and clear cache. Returns backup tab name."""
        with self._lock:
            return self._backup_and_reset_channel_locked(
                channel_name, is_private, member_emails,
            )

    def _backup_and_reset_channel_locked(
        self, channel_name: str, is_private: bool = False, member_emails: list[str] | None = None,
    ) -> str | None:
        now_str = datetime.now(JST).strftime("%Y%m%d_%H%M%S")
        backup_name = f"{channel_name}_bak_{now_str}"

        if is_private:
            if channel_name not in self._private_spreadsheets:
                # Try to find it
                try:
                    self._get_or_create_private_sheet(channel_name, member_emails or [])
                except Exception:
                    return None
            if channel_name not in self._private_spreadsheets:
                return None
            ss = self._private_spreadsheets[channel_name]
            try:
                ws = ss.worksheet(channel_name)
                ss.duplicate_sheet(ws.id, new_sheet_name=backup_name)
                ss.del_worksheet(ws)
                logger.info(f"Backup & reset private #{channel_name} -> {backup_name}")
            except Exception as e:
                logger.error(f"Failed to backup/reset #{channel_name}: {e}")
                return None
        else:
            try:
                ws = self.public_spreadsheet.worksheet(channel_name)
                self.public_spreadsheet.duplicate_sheet(ws.id, new_sheet_name=backup_name)
                self.public_spreadsheet.del_worksheet(ws)
                logger.info(f"Backup & reset public #{channel_name} -> {backup_name}")
            except gspread.exceptions.WorksheetNotFound:
                return None
            except Exception as e:
                logger.error(f"Failed to backup/reset #{channel_name}: {e}")
                return None

        self.clear_cache(channel_name)
        return backup_name

    def get_spreadsheet_url(self, channel_name: str, is_private: bool = False) -> str | None:
        """Return the spreadsheet URL for a channel."""
        if is_private and channel_name in self._private_spreadsheets:
            return self._private_spreadsheets[channel_name].url
        if not is_private:
            return self.public_spreadsheet.url
        return None

    # ── Realtime insert (main.py) ──

    def insert_message(
        self,
        channel_name: str,
        display_name: str,
        username: str,
        text: str,
        ts: str,
        thread_ts: str | None,
        attachment_links: list[str],
        permalink: str,
        is_private: bool = False,
        member_emails: list[str] | None = None,
    ) -> bool:
        with self._lock:
            worksheet = self._get_worksheet(channel_name, is_private, member_emails)
            spreadsheet = self._get_spreadsheet(channel_name, is_private)

            existing = self._load_existing_ts(channel_name, worksheet)
            if ts in existing:
                return False

            row = self._build_row(
                channel_name, display_name, username, text,
                ts, thread_ts, attachment_links, permalink,
            )

            is_thread_reply = thread_ts and thread_ts != ts
            inserted_row = None

            if is_thread_reply:
                insert_pos = self._find_thread_insert_position(worksheet, thread_ts)
                if insert_pos:
                    _retry(
                        worksheet.insert_row, row, insert_pos,
                        value_input_option="RAW", idempotent=False,
                    )
                    inserted_row = insert_pos
                else:
                    # Parent isn't in the sheet (e.g. posted before the bot joined),
                    # so the reply just goes at the end.
                    resp = _retry(
                        worksheet.append_row, row,
                        value_input_option="RAW", idempotent=False,
                    )
                    inserted_row = _appended_row_number(resp)
            else:
                _retry(
                    worksheet.append_row, row,
                    value_input_option="RAW", idempotent=False,
                )

            # Color thread reply row
            if is_thread_reply and inserted_row:
                self._format_single_thread_row(worksheet, spreadsheet, inserted_row)

            existing.add(ts)

        logger.info(
            f"Logged: #{channel_name} {display_name} (@{username}) ({self._ts_to_datetime(ts)})"
        )
        return True

    def update_attachment_links(
        self, channel_name: str, ts: str, attachment_links: list[str],
        is_private: bool = False, member_emails: list[str] | None = None,
    ):
        """Update the attachment column for a message identified by TS."""
        if not attachment_links:
            return
        try:
            with self._lock:
                worksheet = self._get_worksheet(channel_name, is_private, member_emails)
                ts_values = _retry(worksheet.col_values, TS_COLUMN)
                for i, val in enumerate(ts_values):
                    if val == ts:
                        row_num = i + 1  # 1-indexed
                        _retry(
                            worksheet.update_cell, row_num, ATTACHMENT_COLUMN,
                            "\n".join(attachment_links),
                        )
                        logger.info(f"Updated attachments for ts={ts} in #{channel_name}")
                        return
            logger.warning(f"No row found for ts={ts} in #{channel_name}, attachments not linked")
        except Exception as e:
            logger.error(f"Failed to update attachment links: {e}")

    def _find_thread_insert_position(self, worksheet: gspread.Worksheet, thread_ts: str) -> int | None:
        try:
            all_ts = _retry(worksheet.col_values, TS_COLUMN)
            all_thread_ts = _retry(worksheet.col_values, THREAD_TS_COLUMN)
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
        with self._lock:
            return self._write_messages_grouped_locked(
                channel_name, messages, is_private, member_emails,
            )

    def _write_messages_grouped_locked(
        self,
        channel_name: str,
        messages: list[dict],
        is_private: bool = False,
        member_emails: list[str] | None = None,
    ) -> tuple[int, int]:
        worksheet = self._get_worksheet(channel_name, is_private, member_emails)
        spreadsheet = self._get_spreadsheet(channel_name, is_private)
        existing = self._load_existing_ts(channel_name, worksheet)

        new_messages = [m for m in messages if m["ts"] not in existing]
        skip_count = len(messages) - len(new_messages)

        if not new_messages:
            return (0, skip_count)

        # Group by thread
        threads: dict[str, list[dict]] = {}
        for msg in new_messages:
            group_key = msg.get("thread_ts") or msg["ts"]
            threads.setdefault(group_key, []).append(msg)

        for group_key in threads:
            threads[group_key].sort(key=lambda m: float(m["ts"]))

        sorted_groups = sorted(threads.items(), key=lambda item: float(item[0]))

        # Build rows (ordered: parent, reply, reply, ..., next parent, ...)
        ordered_msgs: list[dict] = []
        rows: list[list[str]] = []
        for _group_key, group_msgs in sorted_groups:
            for msg in group_msgs:
                row = self._build_row(
                    channel_name=msg["channel_name"],
                    display_name=msg["display_name"],
                    username=msg["username"],
                    text=msg["text"],
                    ts=msg["ts"],
                    thread_ts=msg.get("thread_ts"),
                    attachment_links=msg.get("attachment_links", []),
                    permalink=msg.get("permalink", ""),
                )
                rows.append(row)
                ordered_msgs.append(msg)
                existing.add(msg["ts"])

        # Batch append; the response tells us exactly where the rows landed
        resp = _retry(
            worksheet.append_rows, rows,
            value_input_option="RAW", idempotent=False,
        )
        start_row = _appended_row_number(resp)

        # Apply thread reply background colors
        if start_row:
            self._format_thread_rows(worksheet, spreadsheet, start_row, ordered_msgs)
        else:
            logger.warning(
                f"Could not determine written row range for #{channel_name}; "
                f"skipping thread row colouring"
            )

        new_count = len(rows)
        logger.info(f"Wrote {new_count} messages (grouped) to #{channel_name}")
        return (new_count, skip_count)
