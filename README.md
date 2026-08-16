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
├── google_auth.py        # Google OAuth2認証（Sheets/Drive共通）
├── config.py             # 環境変数の読み込み
├── setup.py              # セットアップウィザード（対話式）
├── slack-app-manifest.yml # Slack Appマニフェスト（貼るだけでApp設定完了）
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

### Google Drive の構成

botが作るものは、すべて1つのフォルダにまとまります。

```
📁 Slack ログ (ルートフォルダ)
 │
 ├── 📊 Slack ログ - パブリックチャンネル   ← 共有スプレッドシート（チャンネルごとのタブ）
 │
 ├── 📊 Slack Log - #hr-team               ← プライベート用（メンバーのみ閲覧可）
 ├── 📊 Slack Log - #secret-project
 │
 ├── 📁 #general        ← 添付ファイル / リンクで誰でも閲覧可
 │    ├── 会議資料.pdf
 │    └── 週報.xlsx
 ├── 📁 #hr-team        ← 添付ファイル / メンバーのみ閲覧可
 │    └── 人事異動案.pdf
 └── ...
```

> 既にフォルダ外にスプレッドシートがある場合、`python setup.py` を実行すると
> フォルダ内へ移動します（URLとIDは変わりません）。

---

## 導入手順

### 前提条件

- Python 3.10以上
- Slackワークスペースの管理者権限（アプリ作成に必要）
- Googleアカウント

---

### Step 1: Slack Appの作成

このリポジトリの [`slack-app-manifest.yml`](slack-app-manifest.yml) を貼り付けるだけで、
スコープ・イベント・Socket Mode がすべて設定済みのアプリが作られます。

#### 1-1. マニフェストからアプリを作成

1. [Slack API](https://api.slack.com/apps) にアクセス
2. **「Create New App」** → **「From an app manifest」** を選択
3. 対象のワークスペースを選んで **「Next」**
4. **YAML** タブを開き、[`slack-app-manifest.yml`](slack-app-manifest.yml) の中身をまるごと貼り付けて **「Next」**
5. 権限の確認画面が出るので **「Create」**

> アプリ名を変えたい場合は、貼り付ける前に YAML 内の `display_information.name` と
> `features.bot_user.display_name` の両方を書き換えてください。

#### 1-2. App-Level Token の生成

App-Level Token だけはマニフェストで作れないため、手動で生成します。

1. 左メニュー **「Basic Information」** → **「App-Level Tokens」** → **「Generate Token and Scopes」**
2. Token名: `socket-token`、Scope: **`connections:write`** を追加して **「Generate」**
3. 生成された `xapp-...` トークンを控えておく（この画面を閉じると再表示できません）

#### 1-3. アプリのインストール

1. 左メニュー **「Install App」** → **「Install to Workspace」**
2. 権限を確認して **「許可する」**
3. 表示される **Bot User OAuth Token**（`xoxb-...`）を控えておく
4. 記録したいチャンネルにbotを招待:
   ```
   /invite @Log Bot
   ```
   > プライベートチャンネルにも忘れずに招待してください

<details>
<summary><b>マニフェストを使わず手動で設定する場合（既存アプリに追加するときなど）</b></summary>

**OAuth & Permissions** → **Bot Token Scopes** に以下を追加:

| Scope | 用途 |
|-------|------|
| `app_mentions:read` | botへのメンションイベント受信（`app_mention` イベントに必須） |
| `channels:history` | パブリックチャンネルのメッセージ履歴読み取り |
| `channels:read` | チャンネル情報（名前等）の取得 |
| `groups:history` | プライベートチャンネルのメッセージ履歴読み取り |
| `groups:read` | プライベートチャンネル情報の取得 |
| `users:read` | ユーザー名・表示名の取得 |
| `users:read.email` | ユーザーのメールアドレス取得（プライベートチャンネルの共有制御に必要） |
| `files:read` | 添付ファイルのダウンロード |
| `chat:write` | botのメッセージ送信（コマンド応答に必要） |

**Event Subscriptions** → **Enable Events** をON → **Subscribe to bot events** に以下を追加:

- `message.channels` — パブリックチャンネルのメッセージ
- `message.groups` — プライベートチャンネルのメッセージ
- `app_mention` — botへのメンション（コマンド応答に必要）

**Socket Mode** → **Enable Socket Mode** をON。

その後、上記 1-2（App-Level Token 生成）と 1-3（インストール）を実施してください。

> 既存アプリのマニフェストを差し替える方法もあります:
> **「App Manifest」** 画面で YAML を貼り替えて **「Save Changes」** → その後アプリの再インストールが必要です。

</details>

---

### Step 2: Google Cloudの設定

必要なのは **OAuth クライアントID を1つ作るだけ**です。
スプレッドシートと Drive フォルダは Step 4 の `setup.py` が自動で作成するため、
手で作ってIDをコピーする作業はありません。

1. [Google Cloud Console](https://console.cloud.google.com/) でプロジェクトを新規作成（既存でも可）
2. 以下の2つのAPIを有効化:
   - **Google Sheets API** — [有効化リンク](https://console.cloud.google.com/apis/library/sheets.googleapis.com)
   - **Google Drive API** — [有効化リンク](https://console.cloud.google.com/apis/library/drive.googleapis.com)
3. **OAuth同意画面**（[Google Auth Platform](https://console.cloud.google.com/auth/overview)）を設定
   - ユーザーの種類: **外部**
   - アプリ名・ユーザーサポートメール・デベロッパー連絡先を入力
4. **[対象](https://console.cloud.google.com/auth/audience) → 「アプリを公開」** を実行し、公開ステータスを **「本番環境」** にする
5. **[認証情報](https://console.cloud.google.com/apis/credentials) → 「認証情報を作成」→「OAuth クライアント ID」**
   - アプリケーションの種類: **デスクトップアプリ**
   - 作成後、JSONをダウンロードして `client_secret.json` としてプロジェクトに配置

> [!IMPORTANT]
> **手順4（本番環境への切り替え）を飛ばさないでください。**
>
> 「テスト中」のままだと、次の2つが起きます:
> - テストユーザーに自分を追加していないと、認証時に **`エラー 403: access_denied`** で弾かれる
> - 追加して通した場合も、Googleの仕様で **リフレッシュトークンが7日で失効**し、1週間後に突然動かなくなる
>
> 未審査のまま公開しても問題なく動作します（100ユーザーまでの制限付き）。
> 認証時に「このアプリは Google で確認されていません」と警告が出ますが、
> **「詳細」→「（アプリ名）に移動」** で進めてください。

---

### Step 3: botのインストール

```bash
git clone https://github.com/nnnnnnnnnke/slack-log-bot.git
cd slack-log-bot

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

### Step 4: セットアップウィザード

```bash
python setup.py
```

対話形式で以下をすべて実行します:

| | 内容 |
|---|---|
| 1/4 | Slackトークンの入力と接続確認 — **必要なスコープが揃っているか事前に検証**し、足りなければ名前を挙げて中断 |
| 2/4 | Googleの認証（ブラウザが開きます）→ `drive_token.json` を生成 |
| 3/4 | **スプレッドシートとDriveフォルダを自動作成**し、IDを `.env` に書き込み |
| 4/4 | 実際に開けるかを確認する書き込みテスト |

何度でも再実行できます。設定済みかつ正常な項目はスキップされるので、
途中で失敗しても最初からやり直す必要はありません。

```bash
python setup.py --reauth   # Googleの認証だけやり直す
```

<details>
<summary><b>手動で .env を設定する場合</b></summary>

既存のスプレッドシート・フォルダを使いたいときは、`.env` に直接IDを書けば
`setup.py` はそれを尊重して自動作成をスキップします。

```bash
cp .env.example .env
```

```ini
SLACK_BOT_TOKEN=xoxb-xxxxxxxxxxxx-xxxxxxxxxxxx-xxxxxxxxxxxxxxxxxxxxxxxx
SLACK_APP_TOKEN=xapp-x-xxxxxxxxxx-xxxxxxxxxxxxx-xxxxxxxx

# URLの /d/ と /edit の間 / folders/ の後ろ
GOOGLE_SPREADSHEET_ID=your_spreadsheet_id_here
GOOGLE_DRIVE_FOLDER_ID=your_folder_id_here

TIMEZONE=Asia/Tokyo
```

その後 `python setup.py` を実行すれば、認証と疎通確認だけが行われます。

</details>

---

### Step 5: 動作確認

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

### `エラー 403: access_denied` /「Google の審査プロセスを完了していません」

OAuth同意画面が **「テスト中」** で、自分がテストユーザーに入っていない状態です。
[対象](https://console.cloud.google.com/auth/audience) の画面で **「アプリを公開」** を実行してください
（テストユーザーに自分を追加しても通りますが、その場合トークンが7日で失効します）。

### 1週間ほど動いた後に突然 Google 認証が失敗する

OAuth同意画面が **「テスト中」** のままだと、Googleの仕様でリフレッシュトークンが
7日で失効します。同意画面を **「本番環境」** に切り替えてから、再認証してください。

```bash
python setup.py --reauth
```

### `Google の認証ファイル drive_token.json がありません`

`python setup.py` を実行してください。

### `drive_token.json に必要な権限がありません`

Sheets と Drive の認証が1本化される前のトークンです（Drive権限しか持っていません）。
`python setup.py --reauth` で認証をやり直してください。

### `[設定エラー] GOOGLE_SPREADSHEET_ID が .env に設定されていません`

`python setup.py` を実行すると、スプレッドシートを自動作成してIDを `.env` に書き込みます。

### スコープ不足のエラーが出る

`python setup.py` の 1/4 が必要なスコープをすべて検証します。不足していれば名前を挙げるので、
`slack-app-manifest.yml` を App Manifest 画面に貼り直し、アプリを再インストールしてください。

### botがチャンネルのメッセージを取得できない

- botがチャンネルに招待されているか確認: `/invite @Log Bot`
- プライベートチャンネルの場合、`groups:history` と `groups:read` スコープがあるか確認

### botのメンションに反応しない

- Event Subscriptionsで `app_mention` が追加されているか確認
- Socket Modeが有効になっているか確認

---

## ライセンス

MIT
