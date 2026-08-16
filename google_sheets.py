"""Google Sheets handler for logging Slack messages.

Public channels  → shared spreadsheet (anyone with link), tabs per channel
Private channels → separate spreadsheet per channel, shared with members only
"""

import colorsys
import logging
import math
import re
import threading
import time
import unicodedata
from datetime import datetime, timezone, timedelta

import gspread
from googleapiclient.discovery import build

import config
from google_auth import load_credentials
from google_drive import sync_permissions
from sheet_guide import ensure_guide_sheet, write_channel_index

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
    "表示名",
    "メッセージ",
    "添付ファイル",
    "メッセージTS",
    "スレッドTS",
]

# Layouts written by earlier versions, each with the source index of every
# column that survives into HEADER_ROW. Dropped along the way: the channel
# (the spreadsheet is the channel), the display name (a second spelling of the
# username), and the permalink — free-plan messages stop resolving after 90
# days, which is exactly when this archive becomes the only copy, and the link
# is a plain function of the workspace, channel and TS anyway.
LEGACY_LAYOUTS = [
    (
        ["日時", "チャンネル", "表示名", "ユーザー名", "メッセージ",
         "添付ファイル", "パーマリンク", "メッセージTS", "スレッドTS"],
        [0, 3, 4, 5, 7, 8],
    ),
    (
        ["日時", "ユーザー名", "メッセージ", "添付ファイル",
         "パーマリンク", "メッセージTS", "スレッドTS"],
        [0, 1, 2, 3, 5, 6],
    ),
    # Same columns, but the name column held the @handle rather than the
    # display name people actually see in Slack.
    (
        ["日時", "ユーザー名", "メッセージ", "添付ファイル",
         "メッセージTS", "スレッドTS"],
        [0, 1, 2, 3, 4, 5],
    ),
]

# Column indices (1-indexed)
USERNAME_COLUMN = 2
MESSAGE_COLUMN = 3
ATTACHMENT_COLUMN = 4
TS_COLUMN = 5
THREAD_TS_COLUMN = 6

# Column widths (pixels). Total kept near 1000 so the sheet fits a laptop
# screen without horizontal scrolling.
COLUMN_WIDTHS = [130, 120, 450, 150, 90, 90]

# Rows grew to fit their content, so one long message could take thirty lines
# and push everything else off screen. Sheets has no maximum row height —
# pixelSize is exact — so each row's height is worked out from its message and
# capped here instead: short messages stay on one line, long ones open up to
# MAX_MESSAGE_LINES and no further.
#
# Beyond the cap the text is clipped visually only. It is all still there: the
# formula bar shows the whole message, and dragging the row's edge opens it up.
ROW_HEIGHT_BASE = 21        # padding above and below the text
ROW_HEIGHT_PER_LINE = 16    # one line of the 11pt message font
MAX_MESSAGE_LINES = 6
# Half-width characters that fit across the message column, allowing for the
# cell's padding. Full-width characters count as two.
MESSAGE_CHARS_PER_LINE = 66


def _visual_width(text: str) -> int:
    """Width in half-width units, so CJK counts double."""
    return sum(
        2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text
    )


def estimate_lines(text: str) -> int:
    """How many lines a message takes once wrapped in the message column."""
    if not text:
        return 1
    lines = 0
    for paragraph in text.split("\n"):
        width = _visual_width(paragraph)
        lines += max(1, math.ceil(width / MESSAGE_CHARS_PER_LINE))
    return lines


def row_height_for(text: str) -> int:
    lines = min(estimate_lines(text), MAX_MESSAGE_LINES)
    return ROW_HEIGHT_BASE + ROW_HEIGHT_PER_LINE * lines


# Height of an empty row, used for the unwritten part of the grid
DATA_ROW_HEIGHT = row_height_for("")

# Colors (RGB 0-1 float)
COLOR_HEADER_BG = {"red": 0.118, "green": 0.557, "blue": 0.243}   # #1e8e3e Green
COLOR_HEADER_FG = {"red": 1.0, "green": 1.0, "blue": 1.0}         # white
COLOR_THREAD_BG = {"red": 0.902, "green": 0.957, "blue": 0.918}   # #e6f4ea Light Green
COLOR_TS_FG = {"red": 0.55, "green": 0.55, "blue": 0.55}          # #8C8C8C gray
COLOR_LINK = {"red": 0.066, "green": 0.333, "blue": 0.8}          # #1155cc link blue
COLOR_AUTHOR_RULE = {"red": 0.75, "green": 0.75, "blue": 0.75}    # #bfbfbf

# Tints for the name cell, one per author. Hues skip the green band so a
# coloured name is never mistaken for the green a thread reply sits on, and
# every tint is pale enough to read black text against.
#
# Colours are handed out in order of first appearance and remembered, not
# hashed: hashing into N slots still collides by the birthday problem — with
# 20 people and 100 colours, two of them share a colour more often than not.
USER_COLOR_HUE_RANGES = [(0, 89), (161, 359)]
USER_COLOR_HUES = 25
USER_COLOR_TIERS = [(0.35, 0.92), (0.60, 0.90), (0.35, 0.85), (0.60, 0.83)]


def _build_user_palette() -> list[dict]:
    span = sum(high - low for low, high in USER_COLOR_HUE_RANGES)
    hues = []
    for i in range(USER_COLOR_HUES):
        position = span * i / USER_COLOR_HUES
        for low, high in USER_COLOR_HUE_RANGES:
            if position <= high - low:
                hues.append(low + position)
                break
            position -= high - low

    palette = []
    for saturation, lightness in USER_COLOR_TIERS:
        for hue in hues:
            red, green, blue = colorsys.hls_to_rgb(hue / 360, lightness, saturation)
            palette.append({"red": red, "green": green, "blue": blue})
    return palette


USER_COLORS = _build_user_palette()

# Tab in the index spreadsheet holding which colour each name was given, so
# the same person keeps theirs as people come and go.
COLOR_SHEET_TITLE = "🎨 表示名の色"
COLOR_SHEET_HEADER = ["表示名", "色番号"]


# Drive appProperty tying a spreadsheet to its Slack channel, so a rename
# does not orphan the history under the old name.
CHANNEL_ID_PROPERTY = "slackChannelId"

JST = timezone(timedelta(hours=9))

# Collapsing an outline group only hides the rows; Sheets shows nothing in
# their place. The parent stays visible, so the count goes there — otherwise a
# folded thread is indistinguishable from a message that has no replies.
REPLY_COUNT_SUFFIX = "  💬{count}"
REPLY_COUNT_PATTERN = re.compile(r"\s*💬\d+$")


def strip_reply_count(text: str) -> str:
    return REPLY_COUNT_PATTERN.sub("", text)


def with_reply_count(text: str, count: int) -> str:
    """Append the reply count to a parent message, replacing any stale one."""
    base = strip_reply_count(text)
    if count <= 0:
        return base
    return base + REPLY_COUNT_SUFFIX.format(count=count)


class SheetsHandler:
    def __init__(self):
        creds = load_credentials()
        self.gc = gspread.authorize(creds)
        # Holds the guide and the channel index; message rows live in the
        # per-channel spreadsheets beside it.
        self.index_spreadsheet = self.gc.open_by_key(config.GOOGLE_SPREADSHEET_ID)
        self._drive = build("drive", "v3", credentials=creds)

        self.drive_folder_id = config.GOOGLE_DRIVE_FOLDER_ID
        self._sheet_cache: dict[str, gspread.Worksheet] = {}
        self._existing_ts: dict[str, set[str]] = {}
        self._channel_spreadsheets: dict[str, gspread.Spreadsheet] = {}
        self._index_entries: dict[str, str] = {}
        # Last author written per channel, so a run of messages from one
        # person is not ruled off in the middle when they arrive one by one.
        self._last_author: dict[str, str] = {}
        self._color_assignments: dict[str, int] | None = None
        self._formatted_sheets: set[str] = set()
        # Bolt dispatches events concurrently, and backfills run on their own
        # thread. Without this, two writers can interleave their read-then-append
        # and duplicate rows or clobber each other's insert positions.
        self._lock = threading.RLock()

        # Covers spreadsheets created before the guide existed. It is skipped
        # once present, and must never stop the bot from starting.
        try:
            if ensure_guide_sheet(self.index_spreadsheet):
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
                    "startColumnIndex": MESSAGE_COLUMN - 1,
                    "endColumnIndex": MESSAGE_COLUMN,
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
                    "startColumnIndex": TS_COLUMN - 1,
                    "endColumnIndex": THREAD_TS_COLUMN,
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
                    "endColumnIndex": MESSAGE_COLUMN - 1,
                },
                "cell": {
                    "userEnteredFormat": {"verticalAlignment": "MIDDLE"}
                },
                "fields": "userEnteredFormat.verticalAlignment",
            }
        })

        # 8. Fixed height for data rows
        requests.append({
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "ROWS",
                    "startIndex": 1,
                    "endIndex": max(worksheet.row_count, 2),
                },
                "properties": {"pixelSize": DATA_ROW_HEIGHT},
                "fields": "pixelSize",
            }
        })

        # 9. Set basic filter (auto-filter) on header
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

    def _row_height_requests(
        self, sheet_id: int, start_row: int, texts: list[str]
    ) -> list[dict]:
        """Per-row heights, with runs of the same height merged into one request."""
        requests = []
        run_height = None
        run_start = start_row

        for offset, text in enumerate(texts):
            height = row_height_for(text)
            if height != run_height:
                if run_height is not None:
                    requests.append(self._row_height_request(
                        sheet_id, run_start, start_row + offset, run_height
                    ))
                run_height, run_start = height, start_row + offset

        if run_height is not None:
            requests.append(self._row_height_request(
                sheet_id, run_start, start_row + len(texts), run_height
            ))
        return requests

    @staticmethod
    def _row_height_request(sheet_id: int, first_row: int, after_row: int, height: int) -> dict:
        return {
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "ROWS",
                    "startIndex": first_row - 1,   # 1-indexed row -> 0-indexed
                    "endIndex": after_row - 1,
                },
                "properties": {"pixelSize": height},
                "fields": "pixelSize",
            }
        }

    def apply_row_heights(
        self, worksheet: gspread.Worksheet, spreadsheet: gspread.Spreadsheet,
        start_row: int, texts: list[str],
    ):
        requests = self._row_height_requests(worksheet.id, start_row, texts)
        if not requests:
            return
        try:
            _retry(spreadsheet.batch_update, {"requests": requests})
        except Exception as e:
            logger.warning(f"Failed to set row heights: {e}")

    def reply_count_requests(
        self, sheet_id: int, rows: list[list[str]]
    ) -> list[dict]:
        """Refresh the reply count shown on each parent message.

        Counts come from the sheet rather than the batch being written: a
        thread gains replies over time, and the parent may have been written
        long before them.
        """
        counts: dict[str, int] = {}
        for row in rows:
            thread_ts = row[THREAD_TS_COLUMN - 1] if len(row) >= THREAD_TS_COLUMN else ""
            ts = row[TS_COLUMN - 1] if len(row) >= TS_COLUMN else ""
            if thread_ts and thread_ts != ts:
                counts[thread_ts] = counts.get(thread_ts, 0) + 1

        requests = []
        for offset, row in enumerate(rows):
            ts = row[TS_COLUMN - 1] if len(row) >= TS_COLUMN else ""
            thread_ts = row[THREAD_TS_COLUMN - 1] if len(row) >= THREAD_TS_COLUMN else ""
            if not ts or (thread_ts and thread_ts != ts):
                continue
            current = row[MESSAGE_COLUMN - 1] if len(row) >= MESSAGE_COLUMN else ""
            wanted = with_reply_count(current, counts.get(ts, 0))
            if wanted == current:
                continue
            requests.append({
                "updateCells": {
                    "rows": [{"values": [{"userEnteredValue": {"stringValue": wanted}}]}],
                    "fields": "userEnteredValue",
                    "start": {
                        "sheetId": sheet_id,
                        "rowIndex": offset + 1,          # +1 for the header
                        "columnIndex": MESSAGE_COLUMN - 1,
                    },
                }
            })
        return requests

    def sync_reply_counts(
        self, worksheet: gspread.Worksheet, spreadsheet: gspread.Spreadsheet
    ):
        try:
            rows = _retry(worksheet.get_all_values)[1:]
            requests = self.reply_count_requests(worksheet.id, rows)
            if requests:
                _retry(spreadsheet.batch_update, {"requests": requests})
        except Exception as e:
            logger.warning(f"Failed to refresh reply counts: {e}")

    def _load_color_assignments(self) -> dict[str, int]:
        """Which colour each name was given, kept in the index spreadsheet.

        Stored rather than derived so a name keeps its colour for good, and
        shared through the spreadsheet so a second machine running the same
        workspace agrees.
        """
        if self._color_assignments is not None:
            return self._color_assignments

        assignments: dict[str, int] = {}
        try:
            worksheet = self.index_spreadsheet.worksheet(COLOR_SHEET_TITLE)
            for row in _retry(worksheet.get_all_values)[1:]:
                if len(row) >= 2 and row[0] and row[1].isdigit():
                    assignments[row[0]] = int(row[1])
        except gspread.exceptions.WorksheetNotFound:
            pass
        except Exception as e:
            logger.warning(f"Could not read the colour assignments: {e}")

        self._color_assignments = assignments
        return assignments

    def _save_color_assignments(self, assignments: dict[str, int]):
        rows = [COLOR_SHEET_HEADER] + [
            [name, str(index)]
            for name, index in sorted(assignments.items(), key=lambda kv: kv[1])
        ]
        try:
            try:
                worksheet = self.index_spreadsheet.worksheet(COLOR_SHEET_TITLE)
            except gspread.exceptions.WorksheetNotFound:
                worksheet = self.index_spreadsheet.add_worksheet(
                    title=COLOR_SHEET_TITLE, rows=max(len(rows) + 20, 50), cols=2
                )
            _retry(worksheet.clear, idempotent=False)
            _retry(
                worksheet.update, rows, "A1",
                value_input_option="RAW", idempotent=False,
            )
        except Exception as e:
            logger.warning(f"Could not save the colour assignments: {e}")

    def color_for(self, display_name: str) -> dict:
        """The colour for this author, assigning one the first time it is seen."""
        with self._lock:
            assignments = self._load_color_assignments()
            if display_name not in assignments:
                taken = set(assignments.values())
                nxt = next(
                    (i for i in range(len(USER_COLORS)) if i not in taken), None
                )
                if nxt is None:
                    # More authors than colours: wrap round rather than fail,
                    # which is where a duplicate can finally appear.
                    nxt = len(assignments) % len(USER_COLORS)
                    logger.warning(
                        f"More than {len(USER_COLORS)} authors; colours now repeat"
                    )
                assignments[display_name] = nxt
                self._save_color_assignments(assignments)
            return USER_COLORS[assignments[display_name] % len(USER_COLORS)]

    def author_style_requests(
        self, sheet_id: int, start_row: int, authors: list[str],
        previous_author: str | None = None,
    ) -> list[dict]:
        """Colour each author's name cell, and rule off where the author changes.

        The colour goes on the name cell alone: the row's own background is
        already carrying whether the message is a thread reply.
        """
        requests = []

        run_author = None
        run_start = start_row
        for offset, author in enumerate(authors + [None]):
            if author != run_author:
                if run_author is not None:
                    requests.append({
                        "repeatCell": {
                            "range": {
                                "sheetId": sheet_id,
                                "startRowIndex": run_start - 1,
                                "endRowIndex": start_row + offset - 1,
                                "startColumnIndex": USERNAME_COLUMN - 1,
                                "endColumnIndex": USERNAME_COLUMN,
                            },
                            "cell": {
                                "userEnteredFormat": {
                                    "backgroundColor": self.color_for(run_author)
                                }
                            },
                            "fields": "userEnteredFormat.backgroundColor",
                        }
                    })
                run_author, run_start = author, start_row + offset

        # A line above the first message of each new author, so a run of
        # messages from one person reads as one block.
        preceding = previous_author
        for offset, author in enumerate(authors):
            if preceding is not None and author != preceding:
                requests.append({
                    "updateBorders": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": start_row + offset - 1,
                            "endRowIndex": start_row + offset,
                            "startColumnIndex": 0,
                            "endColumnIndex": len(HEADER_ROW),
                        },
                        "top": {"style": "SOLID", "color": COLOR_AUTHOR_RULE},
                    }
                })
            preceding = author
        return requests

    def apply_author_styles(
        self, worksheet: gspread.Worksheet, spreadsheet: gspread.Spreadsheet,
        start_row: int, authors: list[str], previous_author: str | None = None,
    ):
        requests = self.author_style_requests(
            worksheet.id, start_row, authors, previous_author
        )
        if not requests:
            return
        try:
            _retry(spreadsheet.batch_update, {"requests": requests})
        except Exception as e:
            logger.warning(f"Failed to style authors: {e}")

    def thread_group_requests(
        self, sheet_id: int, start_row: int, rows_data: list[dict]
    ) -> list[dict]:
        """Outline groups over each thread's replies, so a thread can be folded.

        A group covers only the replies, never the parent: collapsing hides the
        replies and leaves the message they answer in view. Runs are built from
        consecutive reply rows, which is how a thread is written — parent first,
        then its replies.
        """
        requests = []
        run_start = None
        for offset, msg in enumerate(rows_data):
            is_reply = bool(msg.get("thread_ts")) and msg.get("thread_ts") != msg.get("ts")
            if is_reply:
                if run_start is None:
                    run_start = start_row + offset
            elif run_start is not None:
                requests.append(self._group_request(sheet_id, run_start, start_row + offset))
                run_start = None
        if run_start is not None:
            requests.append(
                self._group_request(sheet_id, run_start, start_row + len(rows_data))
            )
        return requests

    @staticmethod
    def _group_request(sheet_id: int, first_row: int, after_row: int) -> dict:
        return {
            "addDimensionGroup": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "ROWS",
                    "startIndex": first_row - 1,   # 1-indexed row -> 0-indexed
                    "endIndex": after_row - 1,
                }
            }
        }

    def apply_thread_groups(
        self, worksheet: gspread.Worksheet, spreadsheet: gspread.Spreadsheet,
        start_row: int, rows_data: list[dict],
    ):
        requests = self.thread_group_requests(worksheet.id, start_row, rows_data)
        if not requests:
            return
        try:
            _retry(spreadsheet.batch_update, {"requests": requests})
        except Exception as e:
            logger.warning(f"Failed to group thread replies: {e}")

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
                requests.extend(
                    self._thread_background_requests(sheet_id, start_row + i)
                )

        if requests:
            try:
                _retry(spreadsheet.batch_update, {"requests": requests})
            except Exception as e:
                logger.warning(f"Failed to format thread rows: {e}")

    @staticmethod
    def _attachment_cell_request(
        sheet_id: int, row_number: int, attachments: list[tuple[str, str]]
    ) -> dict:
        """Write the attachment cell as linked file names rather than raw URLs.

        A Drive URL is 60-odd unreadable characters; the file name says what it
        is. Rich text is used instead of a HYPERLINK formula because a formula
        yields one link per cell, and a message can carry several files.

        With a single attachment Sheets folds the one run, which spans the
        whole string, into the cell's own textFormat and reports no runs at
        all. The link works either way — worth knowing before concluding from
        an empty textFormatRuns that the link failed to apply.
        """
        text = "\n".join(name for name, _ in attachments)
        runs = []
        position = 0
        for name, url in attachments:
            runs.append({
                "startIndex": position,
                "format": {
                    "link": {"uri": url},
                    "underline": True,
                    "foregroundColor": COLOR_LINK,
                },
            })
            position += len(name)
            # The separating newline carries no link, so the run has to end.
            runs.append({"startIndex": position, "format": {}})
            position += 1

        return {
            "updateCells": {
                "rows": [{
                    "values": [{
                        "userEnteredValue": {"stringValue": text},
                        "textFormatRuns": runs[:-1],   # drop the trailing reset
                    }]
                }],
                "fields": "userEnteredValue,textFormatRuns",
                "start": {
                    "sheetId": sheet_id,
                    "rowIndex": row_number - 1,
                    "columnIndex": ATTACHMENT_COLUMN - 1,
                },
            }
        }

    @staticmethod
    def _thread_background_requests(sheet_id: int, row_number: int) -> list[dict]:
        """Thread reply background for one row, either side of the name cell.

        The name cell is left out rather than painted and repainted: it carries
        the author's colour, and whichever of the two was written last used to
        win. Skipping the column makes the order stop mattering.
        """
        spans = [(0, USERNAME_COLUMN - 1), (USERNAME_COLUMN, len(HEADER_ROW))]
        return [
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": row_number - 1,   # 0-indexed
                        "endRowIndex": row_number,
                        "startColumnIndex": first,
                        "endColumnIndex": last,
                    },
                    "cell": {"userEnteredFormat": {"backgroundColor": COLOR_THREAD_BG}},
                    "fields": "userEnteredFormat.backgroundColor",
                }
            }
            for first, last in spans
            if last > first
        ]

    # ── One spreadsheet per channel ──

    def _channel_spreadsheet_name(self, channel_name: str) -> str:
        return f"Slack Log - #{channel_name}"

    def share_with_members(self, spreadsheet, member_emails: list[str]) -> int:
        """Grant read access to a channel's members. Returns how many were added."""
        added = 0
        for email in member_emails:
            try:
                spreadsheet.share(email, perm_type="user", role="reader", notify=False)
                added += 1
            except Exception as e:
                logger.warning(f"Failed to share {spreadsheet.title} with {email}: {e}")
        return added

    def sync_channel_access(
        self, channel_name: str, member_emails: list[str],
        revoke: list[str] | None = None, channel_id: str = "",
    ) -> tuple[int, int]:
        """Match the channel spreadsheet's readers to the channel's membership."""
        ss = self._get_or_create_channel_spreadsheet(
            channel_name, member_emails, channel_id
        )
        return sync_permissions(self._drive, ss.id, member_emails, revoke)

    def _find_by_channel_id(self, channel_id: str) -> dict | None:
        """The spreadsheet stamped with this channel id, if there is one."""
        if not channel_id:
            return None
        query = (
            f"appProperties has {{ key='{CHANNEL_ID_PROPERTY}' and value='{channel_id}' }} "
            f"and '{self.drive_folder_id}' in parents and trashed = false"
        )
        try:
            files = self._drive.files().list(
                q=query, fields="files(id, name)", pageSize=1
            ).execute().get("files", [])
        except Exception as e:
            logger.warning(f"Could not search by channel id: {e}")
            return None
        return files[0] if files else None

    def _find_by_name(self, name: str) -> dict | None:
        query = (
            f"name = '{name}' and '{self.drive_folder_id}' in parents and "
            f"mimeType = 'application/vnd.google-apps.spreadsheet' and trashed = false"
        )
        files = self._drive.files().list(
            q=query, fields="files(id, name)", pageSize=1
        ).execute().get("files", [])
        return files[0] if files else None

    def _stamp_channel_id(self, file_id: str, channel_id: str, name: str | None = None):
        """Record the channel id on the file, and optionally rename it."""
        body = {"appProperties": {CHANNEL_ID_PROPERTY: channel_id}}
        if name:
            body["name"] = name
        try:
            self._drive.files().update(fileId=file_id, body=body, fields="id").execute()
        except Exception as e:
            logger.warning(f"Could not stamp {file_id} with the channel id: {e}")

    def _get_or_create_channel_spreadsheet(
        self, channel_name: str, member_emails: list[str] | None = None,
        channel_id: str = "",
    ) -> gspread.Spreadsheet:
        """The channel's own spreadsheet, shared with that channel's members.

        Every channel gets its own file rather than a tab in a shared one:
        Sheets grants access per file, so a tab cannot be handed to one group
        without handing over every other channel in the same spreadsheet.

        The file is found by the channel id stamped on it, not by its name: a
        renamed channel would otherwise not match its own spreadsheet, and a
        second one would be created with the history left behind in the first.
        """
        if channel_name in self._channel_spreadsheets:
            return self._channel_spreadsheets[channel_name]

        ss_name = self._channel_spreadsheet_name(channel_name)
        found = self._find_by_channel_id(channel_id)
        if found and found["name"] != ss_name:
            # The channel was renamed while nothing was watching.
            logger.info(f"Renaming {found['name']} -> {ss_name}")
            self._stamp_channel_id(found["id"], channel_id, ss_name)

        if not found:
            # Either a sheet from before ids were stamped, or a new channel.
            found = self._find_by_name(ss_name)
            if found and channel_id:
                self._stamp_channel_id(found["id"], channel_id)

        if found:
            ss = self.gc.open_by_key(found["id"])
        else:
            ss = self.gc.create(ss_name, folder_id=self.drive_folder_id)
            if channel_id:
                self._stamp_channel_id(ss.id, channel_id)
            shared = self.share_with_members(ss, member_emails or [])
            logger.info(f"Created spreadsheet: {ss_name} (shared with {shared} members)")
            if not shared:
                logger.warning(
                    f"{ss_name} is not shared with anyone: no member email was "
                    f"resolvable. Check the users:read.email scope."
                )
            self._register_in_index(channel_name, ss)

        self._channel_spreadsheets[channel_name] = ss
        return ss

    def _register_in_index(self, channel_name: str, spreadsheet: gspread.Spreadsheet):
        """Record the channel's spreadsheet URL in the index."""
        try:
            self._index_entries[channel_name] = spreadsheet.url
            write_channel_index(self.index_spreadsheet, self._collect_index_entries())
        except Exception as e:
            logger.warning(f"Could not update the channel index: {e}")

    def _collect_index_entries(self) -> dict[str, str]:
        """Every channel spreadsheet in the Drive folder, by channel name."""
        entries = dict(self._index_entries)
        try:
            query = (
                f"'{self.drive_folder_id}' in parents and "
                f"mimeType = 'application/vnd.google-apps.spreadsheet' and "
                f"trashed = false"
            )
            found = self._drive.files().list(
                q=query, fields="files(id, name)", pageSize=1000
            ).execute().get("files", [])
            for meta in found:
                if not meta["name"].startswith("Slack Log - #"):
                    continue
                name = meta["name"][len("Slack Log - #"):]
                entries.setdefault(
                    name, f"https://docs.google.com/spreadsheets/d/{meta['id']}/edit"
                )
        except Exception as e:
            logger.warning(f"Could not list channel spreadsheets: {e}")
        return entries

    def _get_or_create_channel_sheet(
        self, channel_name: str, member_emails: list[str] | None = None,
        channel_id: str = "",
    ) -> gspread.Worksheet:
        if channel_name in self._sheet_cache:
            return self._sheet_cache[channel_name]

        ss = self._get_or_create_channel_spreadsheet(
            channel_name, member_emails, channel_id
        )

        try:
            worksheet = ss.worksheet(channel_name)
        except gspread.exceptions.WorksheetNotFound:
            worksheet = ss.sheet1
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
        self, channel_name: str, member_emails: list[str] | None = None,
        channel_id: str = "",
    ) -> gspread.Worksheet:
        return self._get_or_create_channel_sheet(channel_name, member_emails, channel_id)

    def _get_spreadsheet(self, channel_name: str) -> gspread.Spreadsheet:
        """The Spreadsheet object a worksheet belongs to, for formatting calls."""
        return self._get_or_create_channel_spreadsheet(channel_name)

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

    @staticmethod
    def _display_text(msg: dict) -> str:
        """The message as it appears in the cell, marker included.

        Row heights are worked out from this rather than the raw text: the
        marker adds a line to a multi-line reply, and a height measured
        without it comes up one line short.
        """
        # A reply carries no marker in the text: the green background and the
        # outline group it sits in already say what it is, and a glyph in the
        # message column is one more thing to strip when the text is reused.
        return msg.get("text", "")

    def _build_row(
        self,
        display_name: str,
        text: str,
        ts: str,
        thread_ts: str | None,
        attachments: list[tuple[str, str]],
    ) -> list[str]:
        return [
            self._ts_to_datetime(ts),
            display_name,
            text,
            "\n".join(name for name, _ in attachments),
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
            self._last_author.pop(channel_name, None)
            self._existing_ts.pop(channel_name, None)
            self._channel_spreadsheets.pop(channel_name, None)
            self._formatted_sheets.discard(channel_name)
        else:
            self._sheet_cache.clear()
            self._last_author.clear()
            self._existing_ts.clear()
            self._channel_spreadsheets.clear()
            self._formatted_sheets.clear()

    def recorded_ts(
        self, channel_name: str,
        member_emails: list[str] | None = None,
        channel_id: str = "",
    ) -> set[str]:
        """Message TSs already in the sheet for this channel.

        Collectors use this to skip re-downloading and re-uploading the
        attachments of messages they already have: dedup happens at write
        time, so those uploads are discarded, but Drive keeps every copy.
        """
        with self._lock:
            worksheet = self._get_worksheet(channel_name, member_emails, channel_id)
            return set(self._load_existing_ts(channel_name, worksheet))

    def rename_channel(self, channel_id: str, new_name: str) -> bool:
        """Follow a channel rename so its history stays in one spreadsheet."""
        with self._lock:
            found = self._find_by_channel_id(channel_id)
            if not found:
                return False
            new_title = self._channel_spreadsheet_name(new_name)
            if found["name"] != new_title:
                self._stamp_channel_id(found["id"], channel_id, new_title)
            try:
                spreadsheet = self.gc.open_by_key(found["id"])
                worksheet = spreadsheet.sheet1
                if worksheet.title != new_name:
                    _retry(worksheet.update_title, new_name, idempotent=False)
            except Exception as e:
                logger.warning(f"Could not rename the tab for {new_name}: {e}")
            # Caches are keyed by channel name, so the old key must go.
            self._clear_cache_locked()
            self._index_entries.clear()
            try:
                write_channel_index(self.index_spreadsheet, self._collect_index_entries())
            except Exception as e:
                logger.warning(f"Could not refresh the channel index: {e}")
            logger.info(f"Renamed the log spreadsheet to {new_title}")
            return True

    def get_spreadsheet_url(self, channel_name: str) -> str | None:
        """Return the spreadsheet URL for a channel, or None if it has none yet."""
        if channel_name in self._channel_spreadsheets:
            return self._channel_spreadsheets[channel_name].url
        query = (
            f"name = '{self._channel_spreadsheet_name(channel_name)}' and "
            f"'{self.drive_folder_id}' in parents and "
            f"mimeType = 'application/vnd.google-apps.spreadsheet' and trashed = false"
        )
        try:
            files = self._drive.files().list(
                q=query, fields="files(id)", pageSize=1
            ).execute().get("files", [])
        except Exception as e:
            logger.warning(f"Could not look up the sheet for #{channel_name}: {e}")
            return None
        if not files:
            return None
        return f"https://docs.google.com/spreadsheets/d/{files[0]['id']}/edit"

    def index_url(self) -> str:
        """URL of the spreadsheet holding the guide and the channel index."""
        return self.index_spreadsheet.url

    # ── Realtime insert (main.py) ──

    def insert_message(
        self,
        channel_name: str,
        display_name: str,
        text: str,
        ts: str,
        thread_ts: str | None,
        attachments: list[tuple[str, str]],
        member_emails: list[str] | None = None,
        channel_id: str = "",
    ) -> bool:
        with self._lock:
            worksheet = self._get_worksheet(channel_name, member_emails, channel_id)
            spreadsheet = self._get_spreadsheet(channel_name)

            existing = self._load_existing_ts(channel_name, worksheet)
            if ts in existing:
                return False

            row = self._build_row(
                display_name, text, ts, thread_ts, attachments,
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
                resp = _retry(
                    worksheet.append_row, row,
                    value_input_option="RAW", idempotent=False,
                )

            # Height for this row, plus the reply background when it is one.
            # Bundled into a single batch so a message costs one extra call.
            if inserted_row is None and not is_thread_reply:
                inserted_row = _appended_row_number(resp)
            if inserted_row:
                requests = self._row_height_requests(
                    worksheet.id, inserted_row, [text]
                )
                requests.extend(self.author_style_requests(
                    worksheet.id, inserted_row, [display_name],
                    self._last_author.get(channel_name),
                ))
                self._last_author[channel_name] = display_name
                if attachments:
                    requests.append(self._attachment_cell_request(
                        worksheet.id, inserted_row, attachments
                    ))
                if is_thread_reply:
                    requests.extend(
                        self._thread_background_requests(worksheet.id, inserted_row)
                    )
                    # Re-adding a group over the row extends the thread's
                    # existing group rather than creating a second one.
                    requests.append(
                        self._group_request(worksheet.id, inserted_row, inserted_row + 1)
                    )
                try:
                    _retry(spreadsheet.batch_update, {"requests": requests})
                except Exception as e:
                    logger.warning(f"Failed to style row {inserted_row}: {e}")

            existing.add(ts)
            if is_thread_reply:
                self.sync_reply_counts(worksheet, spreadsheet)

        logger.info(
            f"Logged: #{channel_name} {display_name} ({self._ts_to_datetime(ts)})"
        )
        return True

    def update_attachment_links(
        self, channel_name: str, ts: str, attachments: list[tuple[str, str]],
        member_emails: list[str] | None = None,
        channel_id: str = "",
    ):
        """Fill in the attachment cell once the background uploads finish."""
        if not attachments:
            return
        try:
            with self._lock:
                worksheet = self._get_worksheet(channel_name, member_emails, channel_id)
                ts_values = _retry(worksheet.col_values, TS_COLUMN)
                spreadsheet = self._get_spreadsheet(channel_name)
                for i, val in enumerate(ts_values):
                    if val == ts:
                        row_num = i + 1  # 1-indexed
                        _retry(spreadsheet.batch_update, {"requests": [
                            self._attachment_cell_request(
                                worksheet.id, row_num, attachments
                            )
                        ]})
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
        member_emails: list[str] | None = None,
        channel_id: str = "",
    ) -> tuple[int, int]:
        with self._lock:
            return self._write_messages_grouped_locked(
                channel_name, messages, member_emails, channel_id,
            )

    def _write_messages_grouped_locked(
        self,
        channel_name: str,
        messages: list[dict],
        member_emails: list[str] | None = None,
        channel_id: str = "",
    ) -> tuple[int, int]:
        worksheet = self._get_worksheet(channel_name, member_emails, channel_id)
        spreadsheet = self._get_spreadsheet(channel_name)
        existing = self._load_existing_ts(channel_name, worksheet)

        # Deduplicate within the batch as well as against the sheet: the same
        # message can be collected twice in one pass, from the channel history
        # and from its thread.
        new_messages = []
        seen = set(existing)
        for message in messages:
            if message["ts"] in seen:
                continue
            seen.add(message["ts"])
            new_messages.append(message)
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
                    display_name=msg["display_name"],
                    text=msg["text"],
                    ts=msg["ts"],
                    thread_ts=msg.get("thread_ts"),
                    attachments=msg.get("attachments", []),
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

        # Row heights and thread reply backgrounds
        if start_row:
            self.apply_row_heights(
                worksheet, spreadsheet, start_row,
                [self._display_text(m) for m in ordered_msgs],
            )
            attachment_requests = [
                self._attachment_cell_request(
                    worksheet.id, start_row + i, msg["attachments"]
                )
                for i, msg in enumerate(ordered_msgs)
                if msg.get("attachments")
            ]
            if attachment_requests:
                try:
                    _retry(spreadsheet.batch_update, {"requests": attachment_requests})
                except Exception as e:
                    logger.warning(f"Failed to link attachment names: {e}")
            self._format_thread_rows(worksheet, spreadsheet, start_row, ordered_msgs)
            self.apply_thread_groups(worksheet, spreadsheet, start_row, ordered_msgs)
            self.apply_author_styles(
                worksheet, spreadsheet, start_row,
                [m["display_name"] for m in ordered_msgs],
                self._last_author.get(channel_name),
            )
            if ordered_msgs:
                self._last_author[channel_name] = ordered_msgs[-1]["display_name"]
            self.sync_reply_counts(worksheet, spreadsheet)
        else:
            logger.warning(
                f"Could not determine written row range for #{channel_name}; "
                f"skipping thread row colouring"
            )

        new_count = len(rows)
        logger.info(f"Wrote {new_count} messages (grouped) to #{channel_name}")
        return (new_count, skip_count)
