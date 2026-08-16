"""Shared Google OAuth2 credentials for both Sheets and Drive.

Service accounts used to handle the spreadsheet reads and writes, but they have
no Drive storage quota, so file uploads and new spreadsheets had to go through a
second, user-owned OAuth2 credential anyway. Running both meant creating a
service account, downloading its key, and sharing every spreadsheet and folder
with its address by hand. One user credential covers all of it.
"""

import json
import logging
import os

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

DEFAULT_TOKEN_FILE = "drive_token.json"
DEFAULT_CLIENT_FILE = "client_secret.json"


class CredentialsError(RuntimeError):
    """Raised when the stored token is missing, unusable, or under-scoped."""


def token_file() -> str:
    return os.environ.get("GOOGLE_DRIVE_TOKEN_FILE", DEFAULT_TOKEN_FILE)


def client_file() -> str:
    return os.environ.get("GOOGLE_OAUTH_CLIENT_FILE", DEFAULT_CLIENT_FILE)


def save_token(creds: Credentials, path: str | None = None) -> str:
    """Persist a credential, including its expiry so staleness is detectable."""
    path = path or token_file()
    data = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": list(creds.scopes or SCOPES),
    }
    if creds.expiry:
        data["expiry"] = creds.expiry.isoformat()
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(path, 0o600)
    return path


def load_credentials(required_scopes: list[str] | None = None) -> Credentials:
    """Load the stored OAuth2 credential, refreshing it if it has expired."""
    required_scopes = required_scopes or SCOPES
    path = token_file()

    if not os.path.exists(path):
        raise CredentialsError(
            f"Google の認証ファイル {path} がありません。\n"
            f"  python setup.py を実行してセットアップしてください。"
        )

    try:
        with open(path) as f:
            stored = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise CredentialsError(f"{path} を読み込めません: {e}\n  python setup.py で作り直してください。")

    granted = set(stored.get("scopes") or [])
    missing = [s for s in required_scopes if s not in granted]
    if missing:
        # Tokens issued before Sheets access was folded into this credential
        # only carry the Drive scope, so they have to be re-consented.
        raise CredentialsError(
            f"{path} に必要な権限がありません: {', '.join(missing)}\n"
            f"  python setup.py を実行して認証をやり直してください。"
        )

    creds = Credentials.from_authorized_user_info(stored, required_scopes)

    if not creds.valid:
        if not creds.refresh_token:
            raise CredentialsError(
                f"{path} の有効期限が切れており、更新もできません。\n"
                f"  python setup.py を実行して認証をやり直してください。"
            )
        try:
            creds.refresh(Request())
        except Exception as e:
            raise CredentialsError(
                f"Google 認証トークンの更新に失敗しました: {e}\n"
                f"  OAuth同意画面が「テスト中」のままだとトークンは7日で失効します。\n"
                f"  python setup.py で再認証するか、同意画面を「本番環境」に切り替えてください。"
            )
        save_token(creds, path)
        logger.info("Refreshed Google OAuth2 token")

    return creds
