# Slack Log Bot セットアップガイド

Slackの投稿・スレッド・添付ファイルを自動でGoogle Sheets/Driveに保存するbotです。

## 1. Slack App の作成

### 1-1. アプリ作成
1. https://api.slack.com/apps にアクセス
2. **「Create New App」** → **「From scratch」**
3. アプリ名（例: `Log Bot`）とワークスペースを選択

### 1-2. Socket Mode を有効化
1. 左メニュー **「Socket Mode」** → **Enable Socket Mode** をON
2. App-Level Token を生成（名前: `socket-token`、scope: `connections:write`）
3. 生成された `xapp-...` トークンを控える → `.env` の `SLACK_APP_TOKEN` に設定

### 1-3. Event Subscriptions
1. 左メニュー **「Event Subscriptions」** → **Enable Events** をON
2. **「Subscribe to bot events」** で以下を追加:
   - `message.channels` (パブリックチャンネルのメッセージ)
   - `message.groups` (プライベートチャンネルのメッセージ)
   - `file_shared` (ファイル共有)

### 1-4. OAuth & Permissions
左メニュー **「OAuth & Permissions」** → **Bot Token Scopes** に以下を追加:

| Scope | 用途 |
|-------|------|
| `channels:history` | パブリックチャンネルの履歴読み取り |
| `channels:read` | チャンネル情報の取得 |
| `groups:history` | プライベートチャンネルの履歴読み取り |
| `groups:read` | プライベートチャンネル情報の取得 |
| `users:read` | ユーザー名の取得 |
| `files:read` | ファイルのダウンロード |
| `chat:write` | パーマリンク取得に必要 |

### 1-5. インストール
1. 左メニュー **「Install App」** → **「Install to Workspace」**
2. `xoxb-...` トークンを控える → `.env` の `SLACK_BOT_TOKEN` に設定
3. 記録したいチャンネルにbotを招待: `/invite @Log Bot`

---

## 2. Google Cloud の設定

### 2-1. プロジェクト作成 & API有効化
1. https://console.cloud.google.com/ でプロジェクトを作成（または既存を使用）
2. 以下のAPIを有効化:
   - **Google Sheets API**
   - **Google Drive API**

### 2-2. サービスアカウント作成
1. **「IAM と管理」→「サービスアカウント」→「サービスアカウントを作成」**
2. 名前を入力（例: `slack-log-bot`）
3. 作成後、**「鍵」タブ →「鍵を追加」→「新しい鍵を作成」→ JSON**
4. ダウンロードした JSON を `slack_log_bot/service_account.json` に配置

### 2-3. Google スプレッドシート作成
1. Google Sheets で新しいスプレッドシートを作成
2. URLから Spreadsheet ID を取得:
   ```
   https://docs.google.com/spreadsheets/d/【この部分がID】/edit
   ```
3. スプレッドシートの **「共有」** からサービスアカウントのメールアドレスを**編集者**として追加
   - メールアドレスはJSON内の `client_email` に記載

### 2-4. Google Drive フォルダ作成
1. Google Drive で添付ファイル保存用フォルダを作成
2. URLから Folder ID を取得:
   ```
   https://drive.google.com/drive/folders/【この部分がID】
   ```
3. フォルダの **「共有」** からサービスアカウントのメールアドレスを**編集者**として追加

---

## 3. Bot の起動

```bash
cd slack_log_bot

# Python仮想環境を作成
python3 -m venv .venv
source .venv/bin/activate

# 依存関係インストール
pip install -r requirements.txt

# .env ファイル作成
cp .env.example .env
# .env を編集して各値を設定

# 起動
python main.py
```

---

## 4. 過去メッセージの取り込み（バックフィル）

無料プランで残っているメッセージを取り込むには:

```bash
# 全チャンネル（過去90日分）
python backfill.py

# 特定チャンネルのみ
python backfill.py --channel general

# 過去30日分のみ
python backfill.py --days 30
```

---

## 5. 週次定期収集（推奨）

毎週自動でメッセージを収集します。重複チェック付きなので、何度実行しても安全です。

### 手動実行

```bash
# 過去8日分を収集（7日 + 1日の重複マージン）
python collect_weekly.py

# 特定チャンネルのみ
python collect_weekly.py --channel general

# 日数を指定
python collect_weekly.py --days 14
```

### macOS (launchd) で毎週自動実行

```bash
cat > ~/Library/LaunchAgents/com.slack-log-bot-weekly.plist << 'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.slack-log-bot-weekly</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/nukui_school/claude_workspace/slack_log_bot/.venv/bin/python</string>
        <string>/Users/nukui_school/claude_workspace/slack_log_bot/collect_weekly.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/Users/nukui_school/claude_workspace/slack_log_bot</string>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Weekday</key>
        <integer>1</integer>
        <key>Hour</key>
        <integer>9</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>/tmp/slack-log-bot-weekly.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/slack-log-bot-weekly.err</string>
</dict>
</plist>
PLIST

# 登録
launchctl load ~/Library/LaunchAgents/com.slack-log-bot-weekly.plist
```

> 上記の設定: **毎週月曜 9:00** に自動実行。曜日・時刻は変更可能。
> - Weekday: 0=日, 1=月, ..., 6=土
> - Hour/Minute: 24時間表記

### Linux (cron) で毎週自動実行

```bash
# crontab -e で以下を追加（毎週月曜 9:00）
0 9 * * 1 cd /path/to/slack_log_bot && .venv/bin/python collect_weekly.py >> /tmp/slack-log-bot-weekly.log 2>&1
```

---

## 6. リアルタイム収集（オプション）

Socket Modeで常駐させて、投稿をリアルタイムに記録することもできます。
（`SLACK_APP_TOKEN` が追加で必要）

```bash
python main.py
```

### macOS (launchd) で常駐化

```bash
cat > ~/Library/LaunchAgents/com.slack-log-bot-realtime.plist << 'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.slack-log-bot-realtime</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/nukui_school/claude_workspace/slack_log_bot/.venv/bin/python</string>
        <string>/Users/nukui_school/claude_workspace/slack_log_bot/main.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/Users/nukui_school/claude_workspace/slack_log_bot</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/slack-log-bot.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/slack-log-bot.err</string>
</dict>
</plist>
PLIST

launchctl load ~/Library/LaunchAgents/com.slack-log-bot-realtime.plist
```

---

## スプレッドシートの構成

チャンネルごとにシートタブが自動作成されます:

| 日時 | チャンネル | 表示名 | ユーザー名 | メッセージ | スレッド元メッセージ | 添付ファイル | パーマリンク | メッセージTS | スレッドTS |
|------|-----------|--------|-----------|-----------|-------------------|-------------|-------------|-------------|-----------|

- **表示名**: Slackのプロフィール表示名（例: 田中太郎）
- **ユーザー名**: @メンション名（例: @tanaka.taro）
- **スレッド元メッセージ**: スレッド返信の場合、親メッセージの冒頭100文字を表示
- **添付ファイル**: Google Driveへのリンク（複数ある場合は改行区切り）
- **重複防止**: メッセージTS（Slack固有のタイムスタンプID）で重複を自動排除
