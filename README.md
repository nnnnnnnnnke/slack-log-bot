<p align="center">
  <img src="docs/banner.png" alt="Slack Log Bot" width="100%">
</p>

# Slack Log Bot

Slack無料プランではメッセージ履歴が一定期間で消えてしまいます。
このbotは、Slackの投稿・スレッド返信・添付ファイルを自動で **Google スプレッドシート** と **Google Drive** に保存します。

## 特徴

- **リアルタイム収集** — Socket Modeで常駐し、投稿を即時記録。ファイルアップロードはバックグラウンド処理のためメッセージ順序が崩れない
- **週次定期収集** — 毎週自動で過去1週間のメッセージを収集
- **過去履歴の一括取り込み** — Slackコマンドまたはスクリプトで既存メッセージをバックフィル
- **Slackコマンド** — botにメンションしてURL表示・バックフィル・リセット・キャッシュクリアを実行
- **スレッド対応** — 親メッセージとスレッド返信を隣接して記録。返信には `└` マークと背景色で視覚的に区別
- **添付ファイル保存** — PDF・画像等をSlackからダウンロードしてGoogle Driveに自動アップロード。チャンネル別フォルダで整理、Drive URLをスプレッドシートに記録
- **重複防止** — メッセージの一意ID（TS）で自動判定。何度実行しても同じメッセージは追加されない
- **アクセス制御** — パブリックチャンネルはリンク共有、プライベートチャンネルはメンバー限定共有
- **バックアップ＆リセット** — プライベートチャンネルのシートをバックアップ付きでリセット可能
- **見やすいスプレッドシート** — グリーンヘッダー・色分け・メッセージ列11ptフォント・列幅調整・オートフィルターを自動適用

## 動作イメージ

<table>
  <tr>
    <td width="50%"><strong>Slack</strong></td>
    <td width="50%"><strong>Google Sheets</strong></td>
  </tr>
  <tr>
    <td><img src="docs/screenshot_slack.png" alt="Slack側の動作イメージ"></td>
    <td><img src="docs/screenshot_sheets.png" alt="スプレッドシート側の動作イメージ"></td>
  </tr>
</table>

## プロジェクト構成

```
slack_log_bot/
├── main.py              # リアルタイム収集 (Socket Mode) + Slackコマンド
├── collect_weekly.py     # 週次定期収集 (cron/systemd timer)
├── backfill.py           # 過去履歴の一括取り込み
├── google_sheets.py      # スプレッドシート操作・書式設定
├── google_drive.py       # Drive添付ファイルアップロード
├── slack_utils.py        # Slackユーザー・チャンネル情報の解決
├── config.py             # 環境変数の読み込み
├── setup_drive_auth.py   # Google Drive OAuth2認証セットアップ
├── requirements.txt      # Python依存パッケージ
├── .env.example          # 環境変数テンプレート
└── .gitignore
```

## Slackコマンド

チャンネル内でbotにメンションすると、以下のコマンドが使えます：

| コマンド | 説明 |
|----------|------|
| `@Log Bot` / `@Log Bot help` | コマンド一覧とスプレッドシートURLを表示 |
| `@Log Bot url` | そのチャンネルのスプレッドシートURLを表示 |
| `@Log Bot backfill` | そのチャンネルの過去90日分のログを即座に収集 |
| `@Log Bot backfill 30` | 過去N日分を収集（日数指定） |
| `@Log Bot reset` | シートをバックアップ＆リセット（プライベートチャンネルのみ） |
| `@Log Bot clear cache` | メモリ内キャッシュをクリア |

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

| 日時 | チャンネル | 表示名 | ユーザー名 | メッセージ | 添付ファイル | パーマリンク | メッセージTS | スレッドTS |
|------|-----------|--------|-----------|-----------|-------------|-------------|-------------|-----------|

| カラム | 説明 |
|--------|------|
| 表示名 | Slackプロフィールの表示名（例: 田中太郎） |
| ユーザー名 | @メンション名（例: @tanaka.taro） |
| メッセージ | 投稿本文。スレッド返信には先頭に `└ ` が付く |
| 添付ファイル | Google Driveへのリンク（複数ある場合は改行区切り） |
| メッセージTS | Slack固有のタイムスタンプID（重複判定に使用） |

### スプレッドシートの書式

| 要素 | 書式 |
|------|------|
| ヘッダー行 | グリーン背景 + 白太字 + 固定（スクロールで隠れない） + オートフィルター |
| 親メッセージ | 白背景 |
| スレッド返信 | 薄いグリーン背景 + 先頭に `└ ` |
| メッセージ列 | 11ptフォント + テキスト折り返し有効（長文も表示） |
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
| `chat:write` | botのメッセージ送信（コマンド応答に必要） |

#### 1-3. Event Subscriptions

1. 左メニュー **「Event Subscriptions」** → **Enable Events** をON
2. **「Subscribe to bot events」** で以下を追加:
   - `message.channels` — パブリックチャンネルのメッセージ
   - `message.groups` — プライベートチャンネルのメッセージ
   - `app_mention` — botへのメンション（コマンド応答に必要）

#### 1-4. Socket Mode

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

#### 2-3. OAuth2 クライアントID作成（Google Drive用）

サービスアカウントにはDriveストレージ容量がないため、ファイルアップロードとプライベートチャンネルのスプレッドシート作成にはOAuth2認証が必要です。

1. **「APIとサービス」→「認証情報」** に移動
2. **「認証情報を作成」→「OAuth クライアントID」** をクリック
3. アプリケーションの種類: **デスクトップアプリ**
4. 名前（例: `slack-log-bot-drive`）を入力して作成
5. JSONファイルをダウンロードし、`client_secret.json` としてプロジェクトに配置

> **OAuth同意画面の設定**: 初めての場合は先に同意画面を設定する必要があります。
> 「外部」を選び、テストユーザーに自分のGmailアドレスを追加してください。

#### 2-4. Google スプレッドシートの作成

1. [Google Sheets](https://sheets.google.com/) で新しいスプレッドシートを作成
2. 名前を付ける（例: `Slack ログ`）
3. URLからスプレッドシートIDを取得:
   ```
   https://docs.google.com/spreadsheets/d/【ここがスプレッドシートID】/edit
   ```
4. **「共有」** ボタンをクリック → サービスアカウントのメールアドレスを **「編集者」** として追加

#### 2-5. Google Drive フォルダの作成

1. [Google Drive](https://drive.google.com/) で添付ファイル保存用のフォルダを作成
2. 名前を付ける（例: `Slack添付ファイル`）
3. URLからフォルダIDを取得:
   ```
   https://drive.google.com/drive/folders/【ここがフォルダID】
   ```
4. フォルダを右クリック → **「共有」** → サービスアカウントのメールアドレスを **「編集者」** として追加

---

### Step 3: botのインストール

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

---

### Step 4: 環境変数の設定

```bash
cp .env.example .env
```

`.env` を編集して以下を設定:

```ini
# Slack（Step 1で取得したトークン）
SLACK_BOT_TOKEN=xoxb-xxxxxxxxxxxx-xxxxxxxxxxxx-xxxxxxxxxxxxxxxxxxxxxxxx
SLACK_APP_TOKEN=xapp-x-xxxxxxxxxx-xxxxxxxxxxxxx-xxxxxxxx

# Google（Step 2で作成・取得した情報）
GOOGLE_SERVICE_ACCOUNT_FILE=service_account.json
GOOGLE_SPREADSHEET_ID=your_spreadsheet_id_here
GOOGLE_DRIVE_FOLDER_ID=your_folder_id_here

# タイムゾーン
TIMEZONE=Asia/Tokyo
```

`service_account.json` と `client_secret.json` もプロジェクトディレクトリに配置:

```bash
cp /path/to/downloaded/service_account.json ./service_account.json
cp /path/to/downloaded/client_secret.json ./client_secret.json
```

---

### Step 5: Google Drive OAuth2認証

```bash
source .venv/bin/activate
python setup_drive_auth.py
```

ブラウザが開くので、Googleアカウントでログインし、Driveへのアクセスを許可してください。
成功すると `drive_token.json` が生成されます。

> この認証は初回のみ必要です。トークンは自動的にリフレッシュされます。

---

### Step 6: 動作確認

```bash
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

### A. リアルタイム収集

Socket Modeで常駐させて、投稿をリアルタイムに記録します。

```bash
source .venv/bin/activate
python main.py
```

#### systemd で常駐化 (Linux)

```bash
sudo tee /etc/systemd/system/slack-log-bot.service << 'EOF'
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
StandardOutput=append:/var/log/slack-log-bot.log
StandardError=append:/var/log/slack-log-bot.log

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now slack-log-bot.service
```

### B. 週次定期収集

毎週自動でメッセージを収集します。重複チェック付きなので何度実行しても安全です。
リアルタイム収集との併用可能（二重記録は発生しません）。

```bash
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

sudo systemctl daemon-reload
sudo systemctl enable --now slack-log-bot-weekly.timer
```

### C. 過去メッセージの取り込み（バックフィル）

初回導入時や、過去のメッセージを遡って取り込みたいときに使います。
Slackからコマンドでも実行できます: `@Log Bot backfill`

```bash
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

## トラブルシューティング

### 添付ファイルがアップロードされない / `storageQuotaExceeded`

サービスアカウントにはDriveストレージ容量がありません。
`setup_drive_auth.py` を実行してOAuth2認証を完了してください（Step 5参照）。

### `users:read.email` のエラーが出る

プライベートチャンネルのメンバー共有にはメールアドレスが必要です。
Slack AppのBot Token Scopesに `users:read.email` を追加し、アプリを再インストールしてください。

### スプレッドシートに書き込めない

- サービスアカウントのメールアドレスがスプレッドシートに **「編集者」** として共有されているか確認
- Google Sheets API が有効か確認

### botがチャンネルのメッセージを取得できない

- botがチャンネルに招待されているか確認: `/invite @Log Bot`
- プライベートチャンネルの場合、`groups:history` と `groups:read` スコープがあるか確認

### botのメンションに反応しない

- Event Subscriptionsで `app_mention` が追加されているか確認
- Socket Modeが有効になっているか確認

---

## ライセンス

MIT
