import os
import sys

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
