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

from dotenv import dotenv_values, load_dotenv

# Deliberately does not import config: config exits when required values are
# missing, which is exactly the situation this script exists to fix.
import google_auth

ENV_FILE = ".env"   # rewritten by apply_profile() before anything reads it
PROFILE_ROOT = "profiles"


def apply_profile(name: str):
    """Point this run's files at profiles/<name>/ instead of the root.

    One checkout, several bots: a public one and a private one want different
    Slack apps, different Google accounts and different Drive folders, and
    nothing but the code is shared between them. Keeping each set in its own
    directory means one `git pull` updates them all.

    The paths go into the environment because config.py and google_auth.py
    read them from there, and because the bot itself is pointed at a profile
    the same way — SLACK_LOG_PROFILE in its systemd unit.
    """
    global ENV_FILE
    base = os.path.dirname(os.path.abspath(__file__))
    directory = os.path.join(base, PROFILE_ROOT, name)
    os.makedirs(directory, exist_ok=True)
    os.chmod(directory, 0o700)   # a token and a bot token live here

    ENV_FILE = os.path.join(directory, ".env")
    os.environ["SLACK_LOG_PROFILE"] = name
    os.environ["GOOGLE_DRIVE_TOKEN_FILE"] = os.path.join(directory, "drive_token.json")

    # An OAuth client is not account-specific, so a shared one at the root
    # serves every profile unless this one brought its own.
    own_client = os.path.join(directory, "client_secret.json")
    os.environ["GOOGLE_OAUTH_CLIENT_FILE"] = (
        own_client if os.path.exists(own_client)
        else os.path.join(base, "client_secret.json")
    )
ENV_EXAMPLE = ".env.example"

# One folder holds everything the bot owns for a workspace: the index
# spreadsheet, the per-channel spreadsheets, and the attachments folder.
#
# The workspace name is part of both, because one Google account can run the
# bot for several Slack workspaces. Without it the second workspace would find
# the first one's folder by name and adopt it, and two channels that happen to
# share a name would then write into the same spreadsheet.
DRIVE_FOLDER_NAME = "Slack ログ - {workspace}"
SPREADSHEET_NAME = "Slack ログ 索引 - {workspace}"

FOLDER_MIME = "application/vnd.google-apps.folder"
# Must match google_drive.ATTACHMENTS_FOLDER_NAME and
# google_drive.CHANNEL_ID_PROPERTY. Declared here rather than imported: those
# modules import config, and config exits when the ids are missing — which is
# exactly the state this wizard is in while it is still creating them.
ATTACHMENTS_FOLDER_NAME = "添付ファイル"
CHANNEL_ID_PROPERTY = "slackChannelId"

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

    workspace = resp.get("team") or "workspace"
    ok(f"接続成功: {workspace} / bot={resp.get('user')}")

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

    channels = {}
    try:
        cursor = None
        while True:
            listing = WebClient(token=env["SLACK_BOT_TOKEN"]).conversations_list(
                types="public_channel,private_channel", limit=200, cursor=cursor
            )
            channels.update({c["id"]: c["name"] for c in listing["channels"]})
            cursor = listing.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
    except Exception as e:
        warn(f"チャンネル一覧を取得できませんでした: {e}")

    return env, workspace, channels


# ── Step 2: Google OAuth2 ──

def run_google_auth(reauth: bool = False, port: int = 0):
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
    if port:
        print(f"  ポート {port} で待ち受けます。SSH 越しの場合は手元の端末で:")
        print(f"    ssh -L {port}:localhost:{port} <このホスト>")
        print("  を張ってから、表示される URL をブラウザで開いてください。")
    flow = InstalledAppFlow.from_client_secrets_file(client_file, google_auth.SCOPES)
    try:
        creds = flow.run_local_server(port=port, open_browser=not port)
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


def move_into_folder(drive, file_id: str, folder_id: str) -> bool:
    """Put a file under folder_id. Returns True if it actually moved."""
    meta = drive.files().get(fileId=file_id, fields="parents").execute()
    parents = meta.get("parents", [])
    if folder_id in parents:
        return False
    drive.files().update(
        fileId=file_id,
        addParents=folder_id,
        removeParents=",".join(parents),
        fields="id",
    ).execute()
    return True


def organise_attachment_folders(drive, root_folder_id: str) -> int:
    """Gather the per-channel folders under one attachments folder.

    Returns how many were moved. Folder ids are unchanged by a move, so the
    Drive links already recorded in the spreadsheets keep working.
    """
    loose = [
        f
        for f in drive.files().list(
            q=f"'{root_folder_id}' in parents and mimeType = '{FOLDER_MIME}' "
            f"and trashed = false",
            fields="files(id, name)",
            pageSize=1000,
        ).execute().get("files", [])
        if f["name"].startswith("#")
    ]
    if not loose:
        return 0

    attachments_id, _ = find_or_create(
        drive, ATTACHMENTS_FOLDER_NAME, FOLDER_MIME, parent=root_folder_id
    )
    moved = 0
    for folder in loose:
        try:
            drive.files().update(
                fileId=folder["id"],
                addParents=attachments_id,
                removeParents=root_folder_id,
                fields="id",
            ).execute()
            moved += 1
        except Exception as e:
            warn(f"{folder['name']} を移動できませんでした: {e}")
    return moved


def stamp_channel_ids(drive, env: dict[str, str], channels: dict[str, str]) -> int:
    """Record each channel's id on the files that hold its log.

    The bot stamps a file the first time it touches that channel, which can be
    a long wait for a quiet one — and until then a rename leaves the file
    unfindable, because the only handle on it is the name that just changed.
    Doing every channel up front closes that window.
    """
    stamped = 0
    wanted = {}
    for channel_id, name in channels.items():
        wanted[f"Slack Log - #{name}"] = channel_id
        wanted[f"#{name}"] = channel_id

    query = f"'{env['GOOGLE_DRIVE_FOLDER_ID']}' in parents and trashed = false"
    files = drive.files().list(
        q=query, fields="files(id, name, appProperties)", pageSize=1000
    ).execute().get("files", [])

    # Channel folders live one level down, under the attachments folder.
    attachments = next(
        (f for f in files if f["name"] == ATTACHMENTS_FOLDER_NAME), None
    )
    if attachments:
        files += drive.files().list(
            q=f"'{attachments['id']}' in parents and trashed = false",
            fields="files(id, name, appProperties)", pageSize=1000,
        ).execute().get("files", [])

    for meta in files:
        channel_id = wanted.get(meta["name"])
        if not channel_id:
            continue
        if meta.get("appProperties", {}).get(CHANNEL_ID_PROPERTY) == channel_id:
            continue
        try:
            drive.files().update(
                fileId=meta["id"],
                body={"appProperties": {CHANNEL_ID_PROPERTY: channel_id}},
                fields="id",
            ).execute()
            stamped += 1
        except Exception as e:
            warn(f"{meta['name']} にチャンネルIDを付与できませんでした: {e}")
    return stamped


def folder_id_from(raw: str) -> str:
    """The folder id out of a Drive URL, or the id if that is what was given."""
    match = re.search(r"/folders/([A-Za-z0-9_-]+)", raw)
    return match.group(1) if match else raw.split("?")[0].strip("/")


def ask_parent_folder(drive, given: str = "") -> str | None:
    """Where the bot's folder should be created, or None for My Drive.

    A shared drive is the reason to ask. Files made inside one belong to the
    organisation rather than to the account running the bot, so losing that
    account no longer loses the logs — but the bot cannot put a folder there
    unless it is told which one.
    """
    raw = given.strip()
    if not raw:
        print("      保存先の親フォルダがあれば、その URL か ID を貼ってください。")
        print("      共有ドライブに置くと、ファイルの所有者が組織になります。")
        print("      空のまま Enter を押すと、このアカウントのマイドライブに作ります。")
        raw = input("      親フォルダ (省略可): ").strip()
    if not raw:
        return None

    folder_id = folder_id_from(raw)

    try:
        meta = drive.files().get(
            fileId=folder_id, fields="id,name,mimeType,driveId"
        ).execute()
    except Exception as e:
        warn(f"そのフォルダを開けませんでした: {e}")
        warn("マイドライブに作成します。あとから移動できます。")
        return None

    if meta.get("mimeType") != "application/vnd.google-apps.folder":
        warn("フォルダではないようです。マイドライブに作成します。")
        return None

    where = "共有ドライブ内" if meta.get("driveId") else "マイドライブ内"
    ok(f"親フォルダ: {meta['name']}（{where}）")
    return meta["id"]


def check_drive(target: str) -> bool:
    """Try, on a real folder, everything the bot will need to do there.

    A shared drive can refuse the bot for several unrelated reasons — the
    account is not a member, or is one with too weak a role, or the domain
    does not allow members from outside it. Each surfaces as a different
    failure at a different point of the setup. Doing all of it up front, on
    throwaway files, turns that into one answer before anything is built.
    """
    import io

    from googleapiclient.http import MediaIoBaseUpload

    from google_drive import FOLDER_MIME, drive_service

    print("[1m保存先の事前チェック[0m")

    try:
        creds = google_auth.load_credentials()
    except Exception as e:
        fail(f"Google の認証がまだです（{e}）\n  先に `python setup.py` の Step 2 を通してください。")
    drive = drive_service(creds)

    folder_id = folder_id_from(target)

    try:
        meta = drive.files().get(
            fileId=folder_id, fields="id,name,mimeType,driveId,capabilities"
        ).execute()
    except Exception as e:
        fail(
            f"フォルダを開けませんでした: {e}\n"
            f"  この Google アカウントがフォルダのメンバーになっているか確認してください。"
        )
    if meta.get("mimeType") != FOLDER_MIME:
        fail("フォルダではありません。フォルダの URL を指定してください。")

    drive_id = meta.get("driveId")
    drive.bind_drive(drive_id)
    where = "共有ドライブ内" if drive_id else "マイドライブ内"
    ok(f"フォルダを開けました: {meta['name']}（{where}）")
    if not drive_id:
        warn("共有ドライブではないため、ファイルの所有者はこのアカウントのままになります。")

    created: list[str] = []
    problems: list[str] = []

    def attempt(label: str, action, hint: str):
        try:
            result = action()
            ok(f"{label}")
            return result
        except Exception as e:
            print(f"  \033[31m✗\033[0m {label}")
            print(f"      {e}")
            problems.append(hint)
            return None

    try:
        folder = attempt(
            "フォルダの作成",
            lambda: drive.files().create(
                body={"name": "__slack-log-bot-check__", "mimeType": FOLDER_MIME,
                      "parents": [folder_id]},
                fields="id",
            ).execute(),
            "フォルダを作れません。権限が「閲覧者」になっていないか確認してください。",
        )
        if folder:
            created.append(folder["id"])

        parent = folder["id"] if folder else folder_id

        import gspread
        sheet = attempt(
            "スプレッドシートの作成",
            lambda: gspread.authorize(creds).create("__slack-log-bot-check__", folder_id=parent),
            "スプレッドシートを作れません。",
        )
        if sheet:
            created.append(sheet.id)

        upload = attempt(
            "ファイルのアップロード（添付ファイル用）",
            lambda: drive.files().create(
                body={"name": "__slack-log-bot-check__.txt", "parents": [parent]},
                media_body=MediaIoBaseUpload(io.BytesIO(b"check"), mimetype="text/plain"),
                fields="id",
            ).execute(),
            "ファイルをアップロードできません。",
        )
        if upload:
            created.append(upload["id"])

        if upload:
            attempt(
                "ゴミ箱への移動（削除されたメッセージの添付を片付ける）",
                lambda: drive.files().update(
                    fileId=upload["id"], body={"trashed": True}
                ).execute(),
                "ゴミ箱に移せません。権限を「コンテンツ管理者」以上にしてください。",
            )

        if folder:
            found = attempt(
                "作ったものを検索で見つけられるか",
                lambda: drive.files().list(
                    q=f"'{parent}' in parents and trashed = false",
                    fields="files(id,name)", pageSize=10,
                ).execute().get("files", []),
                "検索が効きません。既存のシートを作り直してしまう恐れがあります。",
            )
            if found is not None and not found:
                print("  \033[31m✗\033[0m 検索が空を返しました")
                problems.append("検索が空を返します。作ったばかりのファイルが見つかりません。")
    finally:
        for file_id in reversed(created):
            try:
                drive.files().delete(fileId=file_id).execute()
            except Exception:
                try:
                    drive.files().update(fileId=file_id, body={"trashed": True}).execute()
                except Exception:
                    warn(f"後片付けに失敗しました。手で削除してください: {file_id}")

    print()
    if problems:
        print("\033[1;31m足りない権限があります\033[0m")
        for hint in dict.fromkeys(problems):
            print(f"  - {hint}")
        print()
        print("  共有ドライブ側で、この Google アカウントを")
        print("  \033[1m「コンテンツ管理者」\033[0m以上として追加してください。")
        print("  組織外のアカウントを追加できない設定になっている可能性もあります。")
        return False

    print("\033[1;32mすべて通りました\033[0m")
    print("  このフォルダを保存先にするには、続けて次を実行してください。")
    print(f"    python setup.py --profile <名前> --parent {folder_id}")
    print()
    print("  （このチェックは何も書き換えていません。作ったものは全部消しました）")
    return True


def provision_google_resources(
    env: dict[str, str], workspace: str, channels: dict[str, str] | None = None,
    parent: str = "",
) -> dict[str, str]:
    import gspread

    from google_drive import drive_service, shared_drive_id
    from sheet_guide import ensure_guide_sheet

    step(3, "スプレッドシートと Drive フォルダの準備")

    creds = google_auth.load_credentials()
    drive = drive_service(creds)

    # The folder comes first so the spreadsheet can be created inside it.
    # Private channel spreadsheets and attachment folders already live here,
    # so this keeps everything the bot owns in one place.
    if env.get("GOOGLE_DRIVE_FOLDER_ID"):
        ok(f"既存の Drive フォルダを使用: {env['GOOGLE_DRIVE_FOLDER_ID']}")
        if parent:
            # Moving an existing archive is not something to do as a side
            # effect of a flag: the files already written stay where they are.
            warn("--parent は無視しました。保存先は .env の値が優先されます。")
            warn("  移したい場合は .env の GOOGLE_DRIVE_FOLDER_ID を消して再実行してください。")
    else:
        parent_id = ask_parent_folder(drive, parent)
        folder_name = DRIVE_FOLDER_NAME.format(workspace=workspace)
        folder_id, created = find_or_create(
            drive, folder_name, "application/vnd.google-apps.folder", parent=parent_id,
        )
        env["GOOGLE_DRIVE_FOLDER_ID"] = folder_id
        # Saved as soon as it is known. Anything created here exists in Drive
        # whether or not the rest of this step succeeds, and an id left only in
        # memory is one the next run has to find again by name.
        write_env(env)
        ok(f"Drive フォルダを{'作成' if created else '検出'}: {folder_name}")
        print(f"      https://drive.google.com/drive/folders/{folder_id}")

    folder_id = env["GOOGLE_DRIVE_FOLDER_ID"]
    drive_id = shared_drive_id(drive, folder_id)
    drive.bind_drive(drive_id)
    in_shared_drive = bool(drive_id)
    if in_shared_drive:
        ok("保存先は共有ドライブです")
        print("      ファイルの所有者は共有ドライブ（組織）になります。")
        print("      閲覧できる範囲は共有ドライブのメンバーで決まり、")
        print("      チャンネルごとの個別共有は行いません。")

    if env.get("GOOGLE_SPREADSHEET_ID"):
        ok(f"既存のスプレッドシートを使用: {env['GOOGLE_SPREADSHEET_ID']}")
    else:
        ss_name = SPREADSHEET_NAME.format(workspace=workspace)
        ss_id, created = find_or_create(
            drive, ss_name, "application/vnd.google-apps.spreadsheet",
            parent=folder_id,
        )
        env["GOOGLE_SPREADSHEET_ID"] = ss_id
        write_env(env)
        ok(f"スプレッドシートを{'作成' if created else '検出'}: {ss_name}")
        print(f"      https://docs.google.com/spreadsheets/d/{ss_id}/edit")

    # Spreadsheets made before this was folded in sit at My Drive root.
    try:
        if move_into_folder(drive, env["GOOGLE_SPREADSHEET_ID"], folder_id):
            ok("スプレッドシートを Drive フォルダに移動")
    except Exception as e:
        warn(f"スプレッドシートをフォルダに移動できませんでした: {e}")

    # Google leaves an empty default sheet behind; make it the guide.
    try:
        spreadsheet = gspread.authorize(creds).open_by_key(env["GOOGLE_SPREADSHEET_ID"])
        if ensure_guide_sheet(spreadsheet, in_shared_drive):
            ok("説明タブ「📖 このシートについて」を作成")
        else:
            ok("説明タブは作成済み")
    except Exception as e:
        warn(f"説明タブを作成できませんでした: {e}")

    try:
        stamped = stamp_channel_ids(drive, env, channels or {})
        if stamped:
            ok(f"{stamped} 件にチャンネルIDを付与（リネーム追従のため）")
    except Exception as e:
        warn(f"チャンネルIDを付与できませんでした: {e}")

    try:
        moved = organise_attachment_folders(drive, folder_id)
        if moved:
            ok(f"添付フォルダ {moved} 件を「{ATTACHMENTS_FOLDER_NAME}」にまとめました")
    except Exception as e:
        warn(f"添付フォルダを整理できませんでした: {e}")

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

        from google_drive import drive_service

        creds = google_auth.load_credentials()
        gc = gspread.authorize(creds)
        ss = gc.open_by_key(config.GOOGLE_SPREADSHEET_ID)
        ok(f"スプレッドシートを開けました: {ss.title}")
        drive = drive_service(creds)
        folder = drive.files().get(
            fileId=config.GOOGLE_DRIVE_FOLDER_ID, fields="name"
        ).execute()
        ok(f"Drive フォルダにアクセスできました: {folder.get('name')}")
    except Exception as e:
        fail(f"アクセスに失敗しました: {e}")


def main():
    parser = argparse.ArgumentParser(description="Slack Log Bot セットアップ")
    parser.add_argument(
        "--parent", metavar="URL", default="",
        help="保存先の親フォルダ（共有ドライブなど）。省略すると Step 3 で聞きます",
    )
    parser.add_argument(
        "--check-drive", metavar="URL", default="",
        help="保存先フォルダに必要な権限があるか、実際に作って消して確かめる",
    )
    parser.add_argument(
        "--profile", metavar="NAME", default="",
        help="複数の bot を1つのチェックアウトで動かす場合の名前"
             "（例: --profile leaders → profiles/leaders/ 配下に設定を作る）",
    )
    parser.add_argument(
        "--reauth", action="store_true", help="Google の認証をやり直す"
    )
    parser.add_argument(
        "--auth-port", type=int, default=0, metavar="PORT",
        help="Google 認証の待ち受けポートを固定する"
             "（ヘッドレス機で SSH ポートフォワードを使う場合）",
    )
    args = parser.parse_args()

    if args.check_drive:
        if args.profile:
            apply_profile(args.profile)
        sys.exit(0 if check_drive(args.check_drive) else 1)

    if args.profile:
        apply_profile(args.profile)

    print("\033[1mSlack Log Bot セットアップ\033[0m")
    if args.profile:
        print(f"  プロファイル: \033[1m{args.profile}\033[0m  ({PROFILE_ROOT}/{args.profile}/)")

    env = read_env()
    env, workspace, channels = check_slack(env)
    write_env(env)

    run_google_auth(reauth=args.reauth, port=args.auth_port)

    env = provision_google_resources(env, workspace, channels, args.parent)
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
