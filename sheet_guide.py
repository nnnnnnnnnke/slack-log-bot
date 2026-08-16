"""The guide written into the spreadsheet's default sheet.

Creating a spreadsheet leaves an empty "シート1" behind, and the channel tabs
that appear next explain nothing about themselves. People who open the sheet
are usually not the person who set the bot up, so the tab becomes the guide.

Kept free of any config import so the setup wizard can use it before .env is
complete.
"""

import logging

logger = logging.getLogger(__name__)

GUIDE_SHEET_TITLE = "📖 このシートについて"
INDEX_SHEET_TITLE = "📇 チャンネル一覧"
INDEX_HEADER = ["チャンネル", "ログのスプレッドシート"]

# Names Google gives the default sheet, depending on account locale
DEFAULT_SHEET_TITLES = {"シート1", "シート 1", "Sheet1", "Sheet 1"}

SECTION_PREFIX = "▍"

GUIDE_ROWS: list[tuple[str, str]] = [
    ("Slack ログ", ""),
    ("", ""),
    (f"{SECTION_PREFIX} これは何か", ""),
    ("", "Slack の投稿・スレッド返信・添付ファイルを自動で記録しています。"),
    ("", "Slack 無料プランでは 90 日を過ぎたメッセージが読めなくなるため、消える前に保存しています。"),
    ("", ""),
    (f"{SECTION_PREFIX} ファイルの構成", ""),
    ("", "ログ本体はこのファイルではなく、チャンネルごとの別スプレッドシートにあります。"),
    ("", f"「{INDEX_SHEET_TITLE}」タブに一覧とリンクがあります。"),
    ("", "各スプレッドシートは、そのチャンネルのメンバーにだけ共有されています。"),
    ("", "メンバーが参加・退出すると、共有先も自動で追従します。"),
    ("", "（Google スプレッドシートはタブ単位で権限を分けられないため、ファイルを分けています）"),
    ("", ""),
    (f"{SECTION_PREFIX} 列の意味", ""),
    ("日時", "投稿日時（Asia/Tokyo）"),
    ("ユーザー名", "@メンション名"),
    ("メッセージ", "投稿本文。スレッド返信は先頭に「└ 」が付きます"),
    ("添付ファイル", "Google Drive 上のファイルへのリンク"),
    ("メッセージTS", "Slack 固有のID。重複判定に使っています"),
    ("スレッドTS", "スレッド親メッセージのID。返信でなければ空です"),
    ("", ""),
    (f"{SECTION_PREFIX} 見方", ""),
    ("", "薄い緑色の行はスレッド返信で、親メッセージのすぐ下に並びます。"),
    ("", "ヘッダー行のフィルタから、投稿者やキーワードで絞り込めます。"),
    ("", "添付ファイルは Google Drive のチャンネル別フォルダに保存されています。"),
    ("", ""),
    (f"{SECTION_PREFIX} 編集するときの注意", ""),
    ("", "行の削除・並べ替えはしないでください。"),
    ("", "bot は「メッセージTS」列を見て重複を判定し、スレッド返信の挿入位置を決めています。"),
    ("", "行を消しても自動では戻りません（@Log Bot clear cache のあと backfill が必要です）。"),
    ("", "自由に加工したい場合は、ファイルのコピーを作ってそちらを編集してください。"),
    ("", ""),
    (f"{SECTION_PREFIX} Slack からの操作", ""),
    ("@Log Bot help", "コマンド一覧とこのシートのURLを表示"),
    ("@Log Bot url", "このシートのURLを表示"),
    ("@Log Bot backfill", "そのチャンネルの過去90日分を収集"),
    ("@Log Bot backfill 30", "過去N日分を収集（日数指定）"),
    ("@Log Bot share", "共有設定をチャンネルメンバーに合わせて更新"),
    ("@Log Bot reset", "シートをバックアップして初期化"),
    ("@Log Bot clear cache", "bot のメモリ内キャッシュをクリア"),
    ("", ""),
    ("", "── このタブは setup.py が自動生成しています。編集しても bot の動作には影響しません。"),
]

COLOR_ACCENT = {"red": 0.118, "green": 0.557, "blue": 0.243}  # #1e8e3e
COLOR_MUTED = {"red": 0.55, "green": 0.55, "blue": 0.55}


def _format_requests(sheet_id: int) -> list[dict]:
    requests = [
        # Reads as a document rather than a grid
        {
            "updateSheetProperties": {
                "properties": {"sheetId": sheet_id, "gridProperties": {"hideGridlines": True}},
                "fields": "gridProperties.hideGridlines",
            }
        },
        {
            "updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1},
                "properties": {"pixelSize": 190},
                "fields": "pixelSize",
            }
        },
        {
            "updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 1, "endIndex": 2},
                "properties": {"pixelSize": 660},
                "fields": "pixelSize",
            }
        },
        # Title
        {
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1},
                "cell": {
                    "userEnteredFormat": {
                        "textFormat": {"bold": True, "fontSize": 16, "foregroundColor": COLOR_ACCENT}
                    }
                },
                "fields": "userEnteredFormat.textFormat",
            }
        },
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 1,
                    "endRowIndex": len(GUIDE_ROWS),
                    "startColumnIndex": 1,
                    "endColumnIndex": 2,
                },
                "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP", "verticalAlignment": "TOP"}},
                "fields": "userEnteredFormat(wrapStrategy,verticalAlignment)",
            }
        },
    ]

    for i, (label, _) in enumerate(GUIDE_ROWS):
        if not label.startswith(SECTION_PREFIX):
            continue
        requests.append({
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": i, "endRowIndex": i + 1},
                "cell": {
                    "userEnteredFormat": {
                        "textFormat": {"bold": True, "fontSize": 11, "foregroundColor": COLOR_ACCENT}
                    }
                },
                "fields": "userEnteredFormat.textFormat",
            }
        })

    # Footer note
    requests.append({
        "repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": len(GUIDE_ROWS) - 1, "endRowIndex": len(GUIDE_ROWS)},
            "cell": {"userEnteredFormat": {"textFormat": {"fontSize": 9, "foregroundColor": COLOR_MUTED}}},
            "fields": "userEnteredFormat.textFormat",
        }
    })
    return requests


def write_channel_index(spreadsheet, entries: dict[str, str]):
    """Refresh the tab listing every channel and where its log lives.

    With one spreadsheet per channel, nothing otherwise tells you which files
    exist or which channel each one covers.
    """
    if not entries:
        return

    rows = [INDEX_HEADER]
    rows += [[f"#{name}", url] for name, url in sorted(entries.items())]

    try:
        worksheet = spreadsheet.worksheet(INDEX_SHEET_TITLE)
    except Exception:
        worksheet = spreadsheet.add_worksheet(
            title=INDEX_SHEET_TITLE, rows=max(len(rows) + 20, 50), cols=2, index=1
        )

    worksheet.clear()
    worksheet.update(rows, "A1", value_input_option="USER_ENTERED")

    try:
        spreadsheet.batch_update({"requests": [
            {
                "updateSheetProperties": {
                    "properties": {
                        "sheetId": worksheet.id,
                        "gridProperties": {"frozenRowCount": 1},
                    },
                    "fields": "gridProperties.frozenRowCount",
                }
            },
            {
                "repeatCell": {
                    "range": {"sheetId": worksheet.id, "startRowIndex": 0, "endRowIndex": 1},
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": COLOR_ACCENT,
                            "textFormat": {
                                "bold": True,
                                "foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0},
                            },
                        }
                    },
                    "fields": "userEnteredFormat(backgroundColor,textFormat)",
                }
            },
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": worksheet.id, "dimension": "COLUMNS",
                        "startIndex": 0, "endIndex": 1,
                    },
                    "properties": {"pixelSize": 260},
                    "fields": "pixelSize",
                }
            },
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": worksheet.id, "dimension": "COLUMNS",
                        "startIndex": 1, "endIndex": 2,
                    },
                    "properties": {"pixelSize": 560},
                    "fields": "pixelSize",
                }
            },
        ]})
    except Exception as e:
        logger.warning(f"Failed to format the channel index: {e}")


def _is_blank(worksheet) -> bool:
    """Whether a worksheet holds no content.

    get_all_values() returns [[]] for an empty sheet, not [], so truthiness
    alone reports every blank sheet as occupied.
    """
    return not any(cell.strip() for row in worksheet.get_all_values() for cell in row)


def ensure_guide_sheet(spreadsheet) -> bool:
    """Write the guide into the spreadsheet's default sheet.

    Returns True if it was written, False if it was already there. Never
    overwrites an existing guide, so edits made in the sheet survive.
    """
    worksheets = spreadsheet.worksheets()
    leftovers = [
        ws for ws in worksheets if ws.title in DEFAULT_SHEET_TITLES and _is_blank(ws)
    ]

    if any(ws.title == GUIDE_SHEET_TITLE for ws in worksheets):
        # A blank "シート1" alongside the guide is just clutter. Deleting it is
        # safe here: the guide itself keeps the spreadsheet from losing its
        # last sheet.
        for ws in leftovers:
            try:
                spreadsheet.del_worksheet(ws)
                logger.info(f"Removed leftover empty sheet: {ws.title}")
            except Exception as e:
                logger.warning(f"Could not remove empty sheet {ws.title}: {e}")
        return False

    rows = [list(row) for row in GUIDE_ROWS]

    # Prefer reusing the leftover default sheet, but only while it is untouched.
    target = None
    if leftovers:
        target = leftovers[0]
        target.update_title(GUIDE_SHEET_TITLE)

    if target is None:
        target = spreadsheet.add_worksheet(
            title=GUIDE_SHEET_TITLE, rows=len(rows) + 10, cols=2, index=0
        )

    target.update(rows, "A1", value_input_option="RAW")
    try:
        spreadsheet.batch_update({"requests": _format_requests(target.id)})
    except Exception as e:
        logger.warning(f"Failed to format guide sheet: {e}")

    return True
