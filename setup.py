"""Interactive setup wizard.

Walks through everything the bot needs and verifies each piece as it goes:
Slack tokens (with a scope check), Google OAuth2 consent, and the spreadsheet
and Drive folder, which are created for you rather than made by hand and
pasted in as IDs.

Safe to re-run: anything already configured and working is left alone.

Usage:
    python setup.py            # set up whatever is missing
    python setup.py --reauth   # force the Google consent flow to run again
"""

import argparse
import os
import re
import sys

from dotenv import dotenv_values, load_dotenv

# Deliberately does not import config: config exits when required values are
# missing, which is exactly the situation this script exists to fix.
import google_auth

ENV_FILE = ".env"
ENV_EXAMPLE = ".env.example"

SPREADSHEET_NAME = "Slack ログ"
DRIVE_FOLDER_NAME = "Slack添付ファイル"

# Scopes the bot needs; must stay in sync with slack-app-manifest.yml
REQUIRED_SLACK_SCOPES = [
    "app_mentions:read",
    "channels:history",
    "channels:read",
    "groups:history",
    "groups:read",
    "users:read",
    "users:read.email",
    "files:read",
    "chat:write",
]


# ── Terminal helpers ──

def step(n: int, title: str):
    print(f"\n\033[1m[{n}/4] {title}\033[0m")


def ok(msg: str):
    print(f"  \033[32m✓\033[0m {msg}")


def warn(msg: str):
    print(f"  \033[33m!\033[0m {msg}")


def fail(msg: str):
    print(f"  \033[31m✗\033[0m {msg}", file=sys.stderr)
    sys.exit(1)


def ask(prompt: str, validate=None) -> str:
    while True:
        try:
            value = input(f"  {prompt}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            fail("中断しました。")
        if not value:
            print("    (空欄にはできません)")
            continue
        if validate:
            problem = validate(value)
            if problem:
                print(f"    {problem}")
                continue
        return value


# ── .env handling ──

def read_env() -> dict[str, str]:
    if not os.path.exists(ENV_FILE):
        return {}
    return {k: (v or "") for k, v in dotenv_values(ENV_FILE).items()}


def write_env(values: dict[str, str]):
    """Rewrite .env, preserving comments and key order where they already exist."""
    lines: list[str] = []
    seen: set[str] = set()

    source = ENV_FILE if os.path.exists(ENV_FILE) else ENV_EXAMPLE
    if os.path.exists(source):
        for raw in open(source).read().splitlines():
            match = re.match(r"^([A-Z_][A-Z0-9_]*)=", raw)
            if match and match.group(1) in values:
                key = match.group(1)
                lines.append(f"{key}={values[key]}")
                seen.add(key)
            else:
                lines.append(raw)

    for key, value in values.items():
        if key not in seen:
            lines.append(f"{key}={value}")

    with open(ENV_FILE, "w") as f:
        f.write("\n".join(lines).rstrip() + "\n")
    os.chmod(ENV_FILE, 0o600)


# ── Step 1: Slack ──

def check_token_shape(prefix: str):
    def validate(value: str) -> str | None:
        if not value.startswith(prefix):
            return f"{prefix} で始まる必要があります"
        if len(value.split("-")) < 4:
            return "形式が違います。トークン全体をコピーできているか確認してください"
        return None

    return validate


def warn_if_truncated(token: str, usual_len: int):
    """A short trailing segment usually means the paste was cut off.

    Only a warning: auth.test is the real gate, and hard-failing on length
    would block anyone whose token Slack issues in a different shape.
    """
    secret = token.split("-")[-1]
    if len(secret) < usual_len:
        warn(
            f"末尾が {len(secret)} 文字です（通常 {usual_len} 文字）。"
            f"コピー時に切れているかもしれません。"
        )


AUTH_ERROR_HINTS = {
    "invalid_auth": [
        "トークンが途中で切れていないか（手動選択ではなく Copy ボタンを使う）",
        "アプリをワークスペースにインストール済みか（Install App → Install to Workspace）",
        "インストール後に表示された最新のトークンか（再インストールすると変わります）",
        "別のアプリ／別のワークスペースのトークンを貼っていないか",
    ],
    "account_inactive": ["アプリがワークスペースからアンインストールされています。再インストールしてください"],
    "token_revoked": ["トークンが失効しています。再インストールして新しいトークンを取得してください"],
    "not_authed": ["トークンが空です"],
}


def check_slack(env: dict[str, str]) -> dict[str, str]:
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError

    step(1, "Slack トークンの確認")

    if not env.get("SLACK_APP_TOKEN"):
        print("  Socket Mode 用の App-Level Token が必要です。")
        print("  Basic Information → App-Level Tokens → connections:write")
        env["SLACK_APP_TOKEN"] = ask("SLACK_APP_TOKEN (xapp-...)", check_token_shape("xapp-"))
        warn_if_truncated(env["SLACK_APP_TOKEN"], 64)

    # Re-prompt on failure rather than exiting: re-running the whole wizard
    # because one paste came up short is needless.
    attempts = 3
    resp = None

    for attempt in range(attempts):
        source_is_env = bool(env.get("SLACK_BOT_TOKEN"))
        if not source_is_env:
            print("\n  Slack App の Bot User OAuth Token が必要です。")
            print("  https://api.slack.com/apps → 対象アプリ → OAuth & Permissions")
            print("  （手動で選択せず Copy ボタンを使ってください）")
            env["SLACK_BOT_TOKEN"] = ask(
                "SLACK_BOT_TOKEN (xoxb-...)", check_token_shape("xoxb-")
            )
            warn_if_truncated(env["SLACK_BOT_TOKEN"], 24)

        try:
            resp = WebClient(token=env["SLACK_BOT_TOKEN"]).auth_test()
            break
        except SlackApiError as e:
            code = e.response.get("error", "unknown")
        except Exception as e:
            fail(f"Slack に接続できません: {e}")

        print()
        warn(f"Slack が認証を拒否しました: {code}")
        for hint in AUTH_ERROR_HINTS.get(code, ["トークンを確認してください"]):
            print(f"    - {hint}")
        if source_is_env:
            print("    （このトークンは .env から読み込んだものです）")

        if attempt == attempts - 1:
            fail("トークンを確認できませんでした。")

        print("\n  もう一度入力してください。")
        env["SLACK_BOT_TOKEN"] = ""

    ok(f"接続成功: {resp.get('team')} / bot={resp.get('user')}")

    # Header names are case-insensitive, so normalise before looking one up.
    headers = {k.lower(): v for k, v in (resp.headers or {}).items()}
    raw = headers.get("x-oauth-scopes", "")
    granted = {s.strip() for s in raw.split(",") if s.strip()}

    if not granted:
        warn("付与スコープを確認できませんでした（手動で確認してください）")
    else:
        missing = [s for s in REQUIRED_SLACK_SCOPES if s not in granted]
        if missing:
            print()
            warn(f"スコープが不足しています: {', '.join(missing)}")
            print("    slack-app-manifest.yml を App Manifest 画面に貼り直し、")
            print("    アプリを再インストールしてください。")
            fail("スコープ不足のため中断しました。")
        ok(f"必要なスコープ {len(REQUIRED_SLACK_SCOPES)} 個すべてを確認")

    return env


# ── Step 2: Google OAuth2 ──

def run_google_auth(reauth: bool = False):
    from google_auth_oauthlib.flow import InstalledAppFlow

    step(2, "Google の認証")

    client_file = google_auth.client_file()
    token_path = google_auth.token_file()

    if not reauth and os.path.exists(token_path):
        try:
            google_auth.load_credentials()
            ok(f"既存の認証を再利用: {token_path}")
            return
        except google_auth.CredentialsError as e:
            warn(str(e).splitlines()[0])
            print("    認証をやり直します。")

    if not os.path.exists(client_file):
        print(f"  {client_file} がありません。以下の手順で取得してください:")
        print("    1. https://console.cloud.google.com/ でプロジェクトを作成")
        print("    2. Google Sheets API と Google Drive API を有効化")
        print("    3. APIとサービス → 認証情報 → OAuth クライアントID")
        print("       アプリケーションの種類: デスクトップアプリ")
        print(f"    4. JSON をダウンロードし、{client_file} としてこのディレクトリに配置")
        fail(f"{client_file} を配置してから再実行してください。")

    print("  ブラウザが開きます。Google アカウントでアクセスを許可してください。")
    print("  「このアプリは確認されていません」の警告は 詳細 → 移動 で進めます。")
    flow = InstalledAppFlow.from_client_secrets_file(client_file, google_auth.SCOPES)
    try:
        creds = flow.run_local_server(port=0)
    except Exception as e:
        message = str(e)
        if "access_denied" in message:
            print()
            warn("Google がアクセスを拒否しました (access_denied)")
            print("    OAuth同意画面が「テスト中」のままの可能性が高いです。")
            print("    https://console.cloud.google.com/auth/audience を開き、")
            print("    「アプリを公開」で公開ステータスを「本番環境」にしてください。")
            print("    (テストユーザーに自分を追加しても通りますが、")
            print("     その場合トークンが7日で失効します)")
            fail("公開設定を変更してから再実行してください。")
        fail(f"Google の認証に失敗しました: {e}")
    google_auth.save_token(creds, token_path)
    ok(f"認証完了: {token_path}")


# ── Step 3: Spreadsheet & Drive folder ──

def find_or_create(drive, name: str, mime_type: str, parent: str | None = None) -> tuple[str, bool]:
    """Return (file_id, created). Reuses an existing item with the same name."""
    query = f"name = '{name}' and mimeType = '{mime_type}' and trashed = false"
    if parent:
        query += f" and '{parent}' in parents"
    found = drive.files().list(q=query, fields="files(id, name)", pageSize=1).execute()
    files = found.get("files", [])
    if files:
        return files[0]["id"], False

    metadata = {"name": name, "mimeType": mime_type}
    if parent:
        metadata["parents"] = [parent]
    created = drive.files().create(body=metadata, fields="id").execute()
    return created["id"], True


def provision_google_resources(env: dict[str, str]) -> dict[str, str]:
    import gspread
    from googleapiclient.discovery import build

    from sheet_guide import ensure_guide_sheet

    step(3, "スプレッドシートと Drive フォルダの準備")

    creds = google_auth.load_credentials()
    drive = build("drive", "v3", credentials=creds)

    if env.get("GOOGLE_SPREADSHEET_ID"):
        ok(f"既存のスプレッドシートを使用: {env['GOOGLE_SPREADSHEET_ID']}")
    else:
        ss_id, created = find_or_create(
            drive, SPREADSHEET_NAME, "application/vnd.google-apps.spreadsheet"
        )
        env["GOOGLE_SPREADSHEET_ID"] = ss_id
        ok(f"スプレッドシートを{'作成' if created else '検出'}: {SPREADSHEET_NAME}")
        print(f"      https://docs.google.com/spreadsheets/d/{ss_id}/edit")

    # Google leaves an empty default sheet behind; make it the guide.
    try:
        spreadsheet = gspread.authorize(creds).open_by_key(env["GOOGLE_SPREADSHEET_ID"])
        if ensure_guide_sheet(spreadsheet):
            ok("説明タブ「📖 このシートについて」を作成")
        else:
            ok("説明タブは作成済み")
    except Exception as e:
        warn(f"説明タブを作成できませんでした: {e}")

    if env.get("GOOGLE_DRIVE_FOLDER_ID"):
        ok(f"既存の Drive フォルダを使用: {env['GOOGLE_DRIVE_FOLDER_ID']}")
    else:
        folder_id, created = find_or_create(
            drive, DRIVE_FOLDER_NAME, "application/vnd.google-apps.folder"
        )
        env["GOOGLE_DRIVE_FOLDER_ID"] = folder_id
        ok(f"Drive フォルダを{'作成' if created else '検出'}: {DRIVE_FOLDER_NAME}")
        print(f"      https://drive.google.com/drive/folders/{folder_id}")

    return env


# ── Step 4: End-to-end check ──

def verify(env: dict[str, str]):
    step(4, "書き込みテスト")

    # config reads os.environ, so make the freshly written values visible to it.
    os.environ.update(env)
    load_dotenv(ENV_FILE, override=True)

    try:
        import config
        import gspread

        creds = google_auth.load_credentials()
        gc = gspread.authorize(creds)
        ss = gc.open_by_key(config.GOOGLE_SPREADSHEET_ID)
        ok(f"スプレッドシートを開けました: {ss.title}")

        from googleapiclient.discovery import build
        drive = build("drive", "v3", credentials=creds)
        folder = drive.files().get(
            fileId=config.GOOGLE_DRIVE_FOLDER_ID, fields="name"
        ).execute()
        ok(f"Drive フォルダにアクセスできました: {folder.get('name')}")
    except Exception as e:
        fail(f"アクセスに失敗しました: {e}")


def main():
    parser = argparse.ArgumentParser(description="Slack Log Bot セットアップ")
    parser.add_argument(
        "--reauth", action="store_true", help="Google の認証をやり直す"
    )
    args = parser.parse_args()

    print("\033[1mSlack Log Bot セットアップ\033[0m")

    env = read_env()
    env = check_slack(env)
    write_env(env)

    run_google_auth(reauth=args.reauth)

    env = provision_google_resources(env)
    write_env(env)

    verify(env)

    print("\n\033[1;32mセットアップ完了\033[0m\n")
    print("  過去ログの取り込み:  python backfill.py --days 7")
    print("  リアルタイム収集:    python main.py")
    print()
    print("\033[33m注意:\033[0m OAuth同意画面が「テスト中」のままだと、Google の仕様で")
    print("  リフレッシュトークンが7日で失効します。継続運用する場合は")
    print("  同意画面を「本番環境」に切り替えてください。")


if __name__ == "__main__":
    main()
