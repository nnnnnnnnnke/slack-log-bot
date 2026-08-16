import sys

# The codebase uses `X | None` annotations, which 3.9 raises a TypeError on at
# import time. macOS ships 3.9 as /usr/bin/python3, so a venv built with a bare
# `python3` lands there and fails with no hint about the cause.
if sys.version_info < (3, 10):
    raise SystemExit(
        f"[Python のバージョンエラー] Python 3.10 以上が必要です"
        f"（現在: {sys.version.split()[0]} / {sys.executable}）\n"
        f"  macOS の /usr/bin/python3 は 3.9 です。仮想環境を作り直してください:\n"
        f"    rm -rf .venv && uv venv --python 3.13 .venv\n"
        f"    uv pip install --python .venv/bin/python -r requirements.txt"
    )

import os

from dotenv import load_dotenv

load_dotenv()

SETUP_HINT = "`python setup.py` を実行するとセットアップできます。"


def _required(name: str, hint: str = "") -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(
            f"[設定エラー] {name} が .env に設定されていません。\n"
            f"  {hint or SETUP_HINT}"
        )
    return value


SLACK_BOT_TOKEN = _required("SLACK_BOT_TOKEN")
# APP_TOKEN is only required for Socket Mode (main.py), not for weekly collection
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN", "")

# Sheets and Drive share one OAuth2 user credential; see google_auth.py
GOOGLE_SPREADSHEET_ID = _required("GOOGLE_SPREADSHEET_ID")
GOOGLE_DRIVE_FOLDER_ID = _required("GOOGLE_DRIVE_FOLDER_ID")
GOOGLE_DRIVE_TOKEN_FILE = os.environ.get("GOOGLE_DRIVE_TOKEN_FILE", "drive_token.json")
GOOGLE_OAUTH_CLIENT_FILE = os.environ.get("GOOGLE_OAUTH_CLIENT_FILE", "client_secret.json")

TIMEZONE = os.environ.get("TIMEZONE", "Asia/Tokyo")

# File download size limit (50MB)
MAX_FILE_SIZE = 50 * 1024 * 1024
