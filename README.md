# Slack Log Bot

Slack無料プランではメッセージ履歴が一定期間で消えてしまいます。
このbotは、Slackの投稿・スレッド返信・添付ファイルを自動で **Google スプレッドシート** と **Google Drive** に保存します。

## 特徴

- **週次定期収集** — 毎週自動で過去1週間のメッセージを収集（推奨）
- **リアルタイム収集** — Socket Modeで常駐し、投稿を即時記録（オプション）
- **過去履歴の一括取り込み** — 既存メッセージをバックフィル
- **スレッド対応** — 親メッセージとスレッド返信を隣接して記録。返信には `└` マークと背景色で視覚的に区別
- **添付ファイル保存** — PDF等をSlackからダウンロードしてGoogle Driveに自動アップロード。チャンネル別フォルダで整理
- **重複防止** — メッセージの一意ID（TS）で自動判定。何度実行しても同じメッセージは追加されない
- **アクセス制御** — パブリックチャンネルはリンク共有、プライベートチャンネルはメンバー限定共有
- **見やすいスプレッドシート** — ヘッダー固定・色分け・列幅調整・オートフィルターを自動適用

## プロジェクト構成

```
slack_log_bot/
├── main.py              # リアルタイム収集 (Socket Mode)
├── collect_weekly.py     # 週次定期収集 (cron/systemd timer)
├── backfill.py           # 過去履歴の一括取り込み
├── google_sheets.py      # スプレッドシート操作・書式設定
├── google_drive.py       # Drive添付ファイルアップロード
├── slack_utils.py        # Slackユーザー・チャンネル情報の解決
├── config.py             # 環境変数の読み込み
├── requirements.txt      # Python依存パッケージ
├── .env.example          # 環境変数テンプレート
└── .gitignore
```

## スプレッドシートの構成

### パブリックチャンネル

1つの共有スプレッドシートにチャンネルごとのタブが自動作成されます。
リンクを知っている人は誰でも閲覧できます。

```
📊 共有スプレッドシート
 ├── [general]  タブ
 ├── [random]   タブ
 └── [project]  タブ
```

### プライベートチャンネル

チャンネルごとに専用スプレッドシートが自動作成され、そのチャンネルのメンバーのGoogleアカウントにのみ共有されます。

```
📊 Slack Log - #secret-project  → メンバー3人のみ閲覧可
📊 Slack Log - #hr-team         → メンバー5人のみ閲覧可
```

> Google Sheetsはタブ単位で権限を分けられないため、プライベートチャンネルはスプレッドシート自体を分離しています。

### カラム

| 日時 | チャンネル | 表示名 | ユーザー名 | メッセージ | スレッド元メッセージ | 添付ファイル | パーマリンク | メッセージTS | スレッドTS |
|------|-----------|--------|-----------|-----------|-------------------|-------------|-------------|-------------|-----------|

| カラム | 説明 |
|--------|------|
| 表示名 | Slackプロフィールの表示名（例: 田中太郎） |
| ユーザー名 | @メンション名（例: @tanaka.taro） |
| メッセージ | 投稿本文。スレッド返信には先頭に `└ ` が付く |
| スレッド元メッセージ | スレッド返信の場合、親メッセージの冒頭100文字 |
| 添付ファイル | Google Driveへのリンク（複数ある場合は改行区切り） |
| メッセージTS | Slack固有のタイムスタンプID（重複判定に使用） |

### スプレッドシートの書式

| 要素 | 書式 |
|------|------|
| ヘッダー行 | 紫背景 + 白太字 + 固定（スクロールで隠れない） + オートフィルター |
| 親メッセージ | 白背景 |
| スレッド返信 | 薄い青灰背景 + 先頭に `└ ` |
| メッセージ列 | テキスト折り返し有効（長文も表示） |
| TS列 | グレー小文字（メタデータとして控えめ表示） |

### Google Drive の添付ファイル構成

```
📁 Slack添付ファイル (ルートフォルダ)
 ├── 📁 #general        ← リンクで誰でも閲覧可
 │    ├── 会議資料.pdf
 │    └── 週報.xlsx
 ├── 📁 #hr-team        ← メンバーのみ閲覧可
 │    └── 人事異動案.pdf
 └── ...
```

---

## 導入手順

### 前提条件

- Python 3.10以上
- Slackワークスペースの管理者権限（アプリ作成に必要）
- Googleアカウント

---

### Step 1: Slack Appの作成

#### 1-1. アプリ作成

1. [Slack API](https://api.slack.com/apps) にアクセス
2. **「Create New App」** → **「From scratch」** を選択
3. アプリ名（例: `Log Bot`）を入力し、対象のワークスペースを選択

#### 1-2. OAuth & Permissions（権限設定）

左メニュー **「OAuth & Permissions」** → **「Bot Token Scopes」** に以下を追加:

| Scope | 用途 |
|-------|------|
| `channels:history` | パブリックチャンネルのメッセージ履歴読み取り |
| `channels:read` | チャンネル情報（名前等）の取得 |
| `groups:history` | プライベートチャンネルのメッセージ履歴読み取り |
| `groups:read` | プライベートチャンネル情報の取得 |
| `users:read` | ユーザー名・表示名の取得 |
| `users:read.email` | ユーザーのメールアドレス取得（プライベートチャンネルの共有制御に必要） |
| `files:read` | 添付ファイルのダウンロード |
| `chat:write` | メッセージのパーマリンク取得に必要 |

#### 1-3. Event Subscriptions（リアルタイム収集を使う場合のみ）

> 週次収集のみ使う場合はこのステップは不要です。

1. 左メニュー **「Event Subscriptions」** → **Enable Events** をON
2. **「Subscribe to bot events」** で以下を追加:
   - `message.channels` — パブリックチャンネルのメッセージ
   - `message.groups` — プライベートチャンネルのメッセージ
   - `file_shared` — ファイル共有

#### 1-4. Socket Mode（リアルタイム収集を使う場合のみ）

> 週次収集のみ使う場合はこのステップは不要です。

1. 左メニュー **「Socket Mode」** → **Enable Socket Mode** をON
2. App-Level Token を生成:
   - Token名: `socket-token`
   - Scope: `connections:write`
3. 生成された `xapp-...` トークンを控えておく

#### 1-5. アプリのインストール

1. 左メニュー **「Install App」** → **「Install to Workspace」**
2. 権限を確認して **「許可する」**
3. 表示される **Bot User OAuth Token**（`xoxb-...`）を控えておく
4. 記録したいチャンネルにbotを招待:
   ```
   /invite @Log Bot
   ```
   > プライベートチャンネルにも忘れずに招待してください

---

### Step 2: Google Cloudの設定

#### 2-1. プロジェクト作成 & API有効化

1. [Google Cloud Console](https://console.cloud.google.com/) にアクセス
2. プロジェクトを新規作成（または既存プロジェクトを使用）
3. 以下の2つのAPIを有効化:
   - **Google Sheets API** — [有効化リンク](https://console.cloud.google.com/apis/library/sheets.googleapis.com)
   - **Google Drive API** — [有効化リンク](https://console.cloud.google.com/apis/library/drive.googleapis.com)

#### 2-2. サービスアカウント作成

1. **「IAM と管理」→「サービスアカウント」** に移動
2. **「サービスアカウントを作成」** をクリック
3. 名前を入力（例: `slack-log-bot`）して作成
4. 作成したアカウントをクリック → **「鍵」タブ**
5. **「鍵を追加」→「新しい鍵を作成」→「JSON」** を選択
6. ダウンロードされたJSONファイルを `service_account.json` としてプロジェクトに配置

> サービスアカウントのメールアドレス（例: `slack-log-bot@project-id.iam.gserviceaccount.com`）は
> JSONファイル内の `client_email` フィールドに記載されています。次のステップで使います。

#### 2-3. Google スプレッドシートの作成

1. [Google Sheets](https://sheets.google.com/) で新しいスプレッドシートを作成
2. 名前を付ける（例: `Slack ログ`）
3. URLからスプレッドシートIDを取得:
   ```
   https://docs.google.com/spreadsheets/d/【ここがスプレッドシートID】/edit
   ```
4. **「共有」** ボタンをクリック → サービスアカウントのメールアドレスを **「編集者」** として追加

#### 2-4. Google Drive フォルダの作成

1. [Google Drive](https://drive.google.com/) で添付ファイル保存用のフォルダを作成
2. 名前を付ける（例: `Slack添付ファイル`）
3. URLからフォルダIDを取得:
   ```
   https://drive.google.com/drive/folders/【ここがフォルダID】
   ```
4. フォルダを右クリック → **「共有」** → サービスアカウントのメールアドレスを **「編集者」** として追加

---

### Step 3: botのインストール

#### ローカルマシンの場合

```bash
# リポジトリをクローン
git clone https://github.com/nnnnnnnnnke/slack-log-bot.git
cd slack-log-bot

# Python仮想環境を作成 & 有効化
python3 -m venv .venv
source .venv/bin/activate

# 依存パッケージをインストール
pip install -r requirements.txt
```

#### サーバー (Ubuntu) の場合

```bash
sudo apt-get update
sudo apt-get install -y python3-venv python3-pip

git clone https://github.com/nnnnnnnnnke/slack-log-bot.git
cd slack-log-bot

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

### Step 4: 環境変数の設定

```bash
cp .env.example .env
```

`.env` を編集して以下を設定:

```ini
# Slack（Step 1で取得したトークン）
SLACK_BOT_TOKEN=xoxb-xxxxxxxxxxxx-xxxxxxxxxxxx-xxxxxxxxxxxxxxxxxxxxxxxx
SLACK_APP_TOKEN=xapp-x-xxxxxxxxxx-xxxxxxxxxxxxx-xxxxxxxx  # リアルタイム収集を使う場合のみ

# Google（Step 2で作成・取得した情報）
GOOGLE_SERVICE_ACCOUNT_FILE=service_account.json
GOOGLE_SPREADSHEET_ID=1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms
GOOGLE_DRIVE_FOLDER_ID=0BwwA4oUTeiV1TGRPeTVjaWRDY1E

# タイムゾーン
TIMEZONE=Asia/Tokyo
```

`service_account.json` もプロジェクトディレクトリに配置:

```bash
cp /path/to/downloaded/service_account.json ./service_account.json
```

---

### Step 5: 動作確認

```bash
# 仮想環境を有効化
source .venv/bin/activate

# まず過去メッセージの取り込みテスト（特定チャンネル・過去7日）
python backfill.py --channel general --days 7
```

成功すると以下のようなログが出力されます:

```
2026-03-31 09:00:01 [INFO] Backfilling #general [public]...
2026-03-31 09:00:03 [INFO] Wrote 15 messages (grouped) to #general
2026-03-31 09:00:03 [INFO]   #general: 15 new, 0 duplicates skipped
2026-03-31 09:00:03 [INFO] Backfill complete. New: 15, Skipped (duplicate): 0
```

Google スプレッドシートを開いて、メッセージが記録されていることを確認してください。

---

## 運用方法

### A. 週次定期収集（推奨）

毎週自動でメッセージを収集します。重複チェック付きなので何度実行しても安全です。

#### 手動実行

```bash
source .venv/bin/activate

# 過去8日分を収集（7日 + 1日の重複マージン）
python collect_weekly.py

# 特定チャンネルのみ
python collect_weekly.py --channel general

# 日数を指定
python collect_weekly.py --days 14
```

#### systemd timer で自動化 (Linux)

```bash
# サービスファイル
sudo tee /etc/systemd/system/slack-log-bot-weekly.service << 'EOF'
[Unit]
Description=Slack Log Bot - Weekly Collection
After=network-online.target

[Service]
Type=oneshot
User=ubuntu
WorkingDirectory=/home/ubuntu/slack_log_bot
ExecStart=/home/ubuntu/slack_log_bot/.venv/bin/python collect_weekly.py
StandardOutput=append:/var/log/slack-log-bot-weekly.log
StandardError=append:/var/log/slack-log-bot-weekly.log
EOF

# タイマーファイル（毎週月曜 9:00）
sudo tee /etc/systemd/system/slack-log-bot-weekly.timer << 'EOF'
[Unit]
Description=Run Slack Log Bot weekly

[Timer]
OnCalendar=Mon *-*-* 09:00:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

# 有効化 & 起動
sudo systemctl daemon-reload
sudo systemctl enable --now slack-log-bot-weekly.timer

# 状態確認
systemctl list-timers slack-log-bot-weekly.timer
```

#### cron で自動化 (Linux)

```bash
# crontab -e で以下を追加（毎週月曜 9:00）
0 9 * * 1 cd /home/ubuntu/slack_log_bot && .venv/bin/python collect_weekly.py >> /var/log/slack-log-bot-weekly.log 2>&1
```

#### launchd で自動化 (macOS)

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
        <string>/path/to/slack_log_bot/.venv/bin/python</string>
        <string>/path/to/slack_log_bot/collect_weekly.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/path/to/slack_log_bot</string>
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

launchctl load ~/Library/LaunchAgents/com.slack-log-bot-weekly.plist
```

---

### B. リアルタイム収集（オプション）

Socket Modeで常駐させて、投稿をリアルタイムに記録します。
**Step 1-3, 1-4 の設定が追加で必要**です（Event Subscriptions + Socket Mode）。

```bash
source .venv/bin/activate
python main.py
```

#### systemd で常駐化 (Linux)

```bash
sudo tee /etc/systemd/system/slack-log-bot-realtime.service << 'EOF'
[Unit]
Description=Slack Log Bot - Realtime (Socket Mode)
After=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/slack_log_bot
ExecStart=/home/ubuntu/slack_log_bot/.venv/bin/python main.py
Restart=on-failure
RestartSec=10
StandardOutput=append:/var/log/slack-log-bot-realtime.log
StandardError=append:/var/log/slack-log-bot-realtime.log

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now slack-log-bot-realtime.service
```

> 週次収集とリアルタイム収集は併用可能です。重複防止機能により二重記録は発生しません。

---

### C. 過去メッセージの取り込み（バックフィル）

初回導入時や、過去のメッセージを遡って取り込みたいときに使います。

```bash
source .venv/bin/activate

# 全チャンネル・過去90日分（デフォルト）
python backfill.py

# 特定チャンネルのみ
python backfill.py --channel general

# 過去30日分のみ
python backfill.py --days 30
```

> Slackの無料プランでは古いメッセージは既に削除されている場合があります。
> 導入は早ければ早いほどデータを救える量が増えます。

---

## 管理コマンド

```bash
# --- ログ確認 ---
cat /var/log/slack-log-bot-weekly.log       # 週次収集ログ
cat /var/log/slack-log-bot-realtime.log     # リアルタイムログ

# --- 週次収集を今すぐ手動実行 ---
sudo systemctl start slack-log-bot-weekly.service

# --- タイマー状態確認 ---
systemctl list-timers slack-log-bot-weekly.timer

# --- リアルタイムbot再起動 ---
sudo systemctl restart slack-log-bot-realtime.service
```

---

## トラブルシューティング

### `users:read.email` のエラーが出る

プライベートチャンネルのメンバー共有にはメールアドレスが必要です。
Slack AppのBot Token Scopesに `users:read.email` を追加し、アプリを再インストールしてください。

### スプレッドシートに書き込めない

- サービスアカウントのメールアドレスがスプレッドシートに **「編集者」** として共有されているか確認
- Google Sheets API が有効か確認

### 添付ファイルがアップロードされない

- サービスアカウントのメールアドレスがDriveフォルダに **「編集者」** として共有されているか確認
- Google Drive API が有効か確認
- ファイルサイズが50MBを超えていないか確認（`config.py` の `MAX_FILE_SIZE` で変更可能）

### botがチャンネルのメッセージを取得できない

- botがチャンネルに招待されているか確認: `/invite @Log Bot`
- プライベートチャンネルの場合、`groups:history` と `groups:read` スコープがあるか確認

### 週次収集が動かない（systemd timer）

```bash
# タイマーが有効か確認
systemctl is-enabled slack-log-bot-weekly.timer

# 手動でサービスを実行してエラーを確認
sudo systemctl start slack-log-bot-weekly.service
journalctl -u slack-log-bot-weekly.service -n 50
```

---

## ライセンス

MIT
