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
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent

# Which set of credentials this process runs on. One checkout can drive
# several bots — a public one and a private one, say — each with its own
# Slack app, Google account and Drive folder, kept apart under profiles/.
# Unset means the checkout's own root, which is what a single-bot install
# has always been and stays.
PROFILE = os.environ.get("SLACK_LOG_PROFILE", "").strip()
PROFILE_DIR = BASE_DIR / "profiles" / PROFILE if PROFILE else BASE_DIR

if PROFILE and not PROFILE_DIR.is_dir():
    raise SystemExit(
        f"[設定エラー] プロファイル '{PROFILE}' が見つかりません（{PROFILE_DIR}）。\n"
        f"  `python setup.py --profile {PROFILE}` を実行すると作成できます。"
    )

# An explicit path so the bot does not depend on which directory it was
# started from; with a profile there is more than one .env to choose between.
load_dotenv(PROFILE_DIR / ".env")

SETUP_HINT = (
    f"`python setup.py --profile {PROFILE}` を実行するとセットアップできます。"
    if PROFILE else "`python setup.py` を実行するとセットアップできます。"
)


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
def _client_file() -> str:
    """The OAuth client for this profile, falling back to the shared one.

    A client is not tied to a Google account — one can authorise all three —
    so a client_secret.json at the checkout root serves every profile, and a
    profile needing its own just puts one beside its .env.
    """
    own = PROFILE_DIR / "client_secret.json"
    if own.exists() or not PROFILE:
        return str(own)
    return str(BASE_DIR / "client_secret.json")


# The token is the account, so it never falls back to another profile's.
GOOGLE_DRIVE_TOKEN_FILE = os.environ.get("GOOGLE_DRIVE_TOKEN_FILE") or str(
    PROFILE_DIR / "drive_token.json"
)
GOOGLE_OAUTH_CLIENT_FILE = os.environ.get("GOOGLE_OAUTH_CLIENT_FILE") or _client_file()

TIMEZONE = os.environ.get("TIMEZONE", "Asia/Tokyo")

# File download size limit (50MB)
MAX_FILE_SIZE = 50 * 1024 * 1024
