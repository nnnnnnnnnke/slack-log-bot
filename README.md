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
- **Slackコマンド** — botにメンションしてURL表示・バックフィルを実行
- **スレッド対応** — 親メッセージとスレッド返信を隣接して記録。返信は背景色で区別し、**折りたたんで隠せる**。親メッセージには返信数を表示
- **発言者の識別** — 表示名のセルを人ごとに色分けし、投稿者が変わる行に区切り線を表示
- **添付ファイル保存** — PDF・画像等をSlackからダウンロードしてGoogle Driveに自動アップロード。チャンネル別フォルダで整理、Drive URLをスプレッドシートに記録
- **重複防止** — メッセージの一意ID（TS）で自動判定。何度実行しても同じメッセージは追加されない
- **変更への追従** — Slack側で表示名を変えたりメッセージを編集すると、記録済みの行にも反映される
- **リネーム追従** — スプレッドシートはチャンネルIDで紐づくため、チャンネル名を変えてもログが分断されない
- **アクセス制御** — チャンネルごとに別スプレッドシートを作り、そのチャンネルのメンバーにのみ共有。参加・退出に応じて自動で追従
- **見やすいスプレッドシート** — グリーンヘッダー・色分け・メッセージ列11ptフォント・列幅調整・オートフィルターを自動適用

## 動作イメージ

> スクリーンショットは旧バージョンのものです（列構成・書式が現在と異なります）。


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
├── collector.py          # チャンネル履歴の収集（3つの入口で共用）
├── slack_utils.py        # Slackユーザー・チャンネル情報の解決
├── google_auth.py        # Google OAuth2認証（Sheets/Drive共通）
├── sheet_guide.py        # 説明タブ・チャンネル一覧タブの生成
├── migrate_to_per_channel.py     # 旧構成からの移行（共有シート→チャンネル別）
├── migrate_columns.py            # 旧構成からの移行（9列→7列）
├── cleanup_duplicate_files.py    # 重複した添付ファイルの掃除
├── relink_attachments.py         # 既存の添付セルをファイル名リンクに変換
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
| `@Slackログ保存bot help` | コマンド一覧とスプレッドシートURLを表示 |
| `@Slackログ保存bot url` | そのチャンネルのスプレッドシートURLを表示 |
| `@Slackログ保存bot backfill` | そのチャンネルの過去90日分のログを即座に収集 |
| `@Slackログ保存bot backfill 30` | 過去N日分を収集（日数指定） |

## スプレッドシートの構成

**チャンネルごとに専用のスプレッドシート**が自動作成され、そのチャンネルのメンバーの
Google アカウントにのみ共有されます。

```
📊 Slack ログ                      ← 索引（使い方とチャンネル一覧）
📊 Slack Log - #general            → #general のメンバーのみ閲覧可
📊 Slack Log - #hr-team            → #hr-team のメンバーのみ閲覧可
📊 Slack Log - #secret-project     → #secret-project のメンバーのみ閲覧可
```

> Google スプレッドシートは**タブ単位で権限を分けられません**。
> 1枚のファイルにタブを並べると、1チャンネル分を誰かに渡した時点で
> 同じファイル内の全チャンネルが見えてしまうため、ファイルごと分けています。

索引スプレッドシート（`.env` の `GOOGLE_SPREADSHEET_ID`）にはログ本体は入っておらず、
`📖 このシートについて` と `📇 チャンネル一覧` の2タブだけを持ちます。
チャンネル一覧には各チャンネルとそのログのURLが並び、新しいチャンネルが
記録されるたびに自動で更新されます。

> **メンバーのメールアドレスが必要です。** Slack プロフィールのメールアドレスを
> Google アカウントとして共有先に指定するため、`users:read.email` スコープが要ります。
> メールが取得できないメンバー（botなど）はスキップされます。

### 共有の自動同期

チャンネルのメンバー構成が変わると、共有設定も自動で追従します。

| きっかけ | 動作 |
|---------|------|
| メンバーが**参加** | そのチャンネルのスプレッドシートと添付フォルダを自動で共有 |
| メンバーが**退出** | そのメンバーの共有を自動で解除 |
| チャンネルが**リネーム** | スプレッドシートとDriveフォルダの名前を追従（ログは分断されません） |
| メンバーが**表示名を変更** | 記録済みの行の表示名も書き換え、色もそのまま引き継ぐ |
| メッセージが**編集** | その行の本文を更新（行の高さも再計算） |
| 週次収集の実行時 | 全チャンネルで未共有のメンバーを補完（botの停止中に起きた参加を回収） |

共有する権限は**編集者**です。Google スプレッドシートのアウトライン（スレッドの
折りたたみ `+` / `−`）は閲覧権限では操作できないためで、代わりに
**編集者による再共有は禁止**に設定しています。プライベートチャンネルのログを
メンバーの1人が他所へ渡せてしまうのを防ぐためです。

付与したい権限より弱い権限しか持っていない人は自動で昇格します。オーナーや、
手でより強い権限を付けた人は下げません。

> メールアドレスが1件も解決できなかった場合、既存の共有は**解除されません**。
> 一時的な取得失敗でチャンネル全員がアクセスを失うのを避けるためです。

#### 共有ドライブに保存する場合

保存先フォルダが**共有ドライブ**の中にある場合、上の自動同期は行いません。

| | マイドライブ | 共有ドライブ |
|---|---|---|
| ファイルの所有者 | bot の Google アカウント | **共有ドライブ（組織）** |
| bot のアカウントを失ったら | ログも失う | ログは残る |
| チャンネルごとのアクセス制御 | 効く | **効かない** |
| 退出時の権限剥奪 | 効く | **効かない** |

共有ドライブは配下のフォルダやファイルで権限を絞れない（足すことしかできない）
仕様です。そのドライブのメンバーは中の全ファイルを読めます。bot が個別共有を
足しても外せなくなるだけなので、共有ドライブを検出したときは何もせず、
閲覧範囲の判断をドライブのメンバーシップに委ねます。

**その系統のログを全部見てよい人だけ**を、その共有ドライブのメンバーにしてください。
チャンネル単位で見せ分けたい場合は、共有ドライブではなくマイドライブに置いてください。

保存先は `setup.py` の Step 3 で聞かれます。共有ドライブのフォルダ URL を貼ると
その配下に作成し、空のまま Enter を押すとマイドライブに作成します。

### カラム

| 日時 | 表示名 | メッセージ | 添付ファイル | メッセージTS | スレッドTS |
|------|--------|-----------|-------------|-------------|-----------|

| カラム | 説明 |
|--------|------|
| 表示名 | Slackプロフィールの表示名（例: 温井直輝）。人ごとに背景色が付く |
| メッセージ | 投稿本文。`<@U012ABC>` のようなメンションは `@name` に、`<#C012ABC>` は `#channel` に展開される。返信がある投稿には末尾に `💬3` のように返信数が付く |
| 添付ファイル | ファイル名がGoogle Driveへのリンクになっている（複数ある場合は改行区切り） |
| メッセージTS | Slack固有のタイムスタンプID（重複判定に使用） |
| スレッドTS | スレッド親メッセージのID。返信でなければ空 |

> 外した列と理由:
> **チャンネル名** — 1チャンネル=1スプレッドシートなので全行に同じ値が並ぶだけ。
> **@ユーザー名** — 表示名と同じ人を指す二重表記。
> **パーマリンク** — 無料プランでは90日を過ぎたメッセージがSlack上で開けなくなり、
> アーカイブとして価値が出る頃にはリンクが死んでいるため
> （必要なら `https://<workspace>.slack.com/archives/<チャンネルID>/p<TSからドットを除去>` で復元できます）。

### スプレッドシートの書式

| 要素 | 書式 |
|------|------|
| ヘッダー行 | グリーン背景 + 白太字 + 固定（スクロールで隠れない） + オートフィルター |
| 表示名 | 人ごとの背景色。名前のハッシュで決まるため、**同じ人はどのチャンネルでも同じ色**（設定不要） |
| 投稿者の切り替わり | 上罫線で区切り、同じ人の連投が1つの塊に見える |
| スレッド返信 | 薄いグリーン背景。**アウトライン（行グループ）で折りたたみ可能** |
| 親メッセージ | 返信がある場合、本文末尾に `💬3` のように返信数を表示 |
| メッセージ列 | 11ptフォント + テキスト折り返し有効 |
| 行の高さ | メッセージの長さに応じて 37px（1行）〜117px（6行）で自動調整 |
| 列幅 | 合計 1030px（ノートPCの画面幅に収まる想定） |

> **スレッドの折りたたみ**: 行番号の左に出る `−` / `+` で開閉できます。左上の `1` `2` で全スレッドを一括開閉。
> 折りたたんでも Sheets は件数を表示しないため、**親メッセージ側に `💬n` を出しています**。

> 行の高さは、メッセージを列幅で折り返したときの行数から計算しています。
> 短い投稿は1行分のまま詰まり、長い投稿だけが広がります。
> ただし **6行分（117px）が上限**で、それを超える長文は表示が途中で切れます。
> **データは全文保持されており**、セルを選べば数式バーに全文が出ます。
> 上限を変えるには `google_sheets.py` の `MAX_MESSAGE_LINES` を調整してください。

### Google Drive の構成

botが作るものは、すべて1つのフォルダにまとまります。

```
📁 Slack ログ (ルートフォルダ)
 │
 ├── 📊 Slack ログ                         ← 索引（使い方 + チャンネル一覧）
 │
 ├── 📊 Slack Log - #general               ← チャンネルごとのログ
 ├── 📊 Slack Log - #hr-team                  各チャンネルのメンバーのみ閲覧可
 ├── 📊 Slack Log - #secret-project
 │
 └── 📁 添付ファイル
      ├── 📁 #general        ← #general のメンバーのみ閲覧可
      │    ├── 会議資料.pdf
      │    └── 週報.xlsx
      └── 📁 #hr-team        ← #hr-team のメンバーのみ閲覧可
           └── 人事異動案.pdf
```

> `python setup.py` は再実行するたびにこの構成へ寄せます。
> フォルダ外のスプレッドシートはフォルダ内へ、ルート直下のチャンネル別フォルダは
> 「添付ファイル」の下へ移動します。**IDとURLは変わらない**ため、
> スプレッドシートに記録済みのリンクはそのまま使えます。

---

## 導入手順

### 前提条件

- Python 3.10以上（macOS標準の3.9では動きません）
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
   /invite @Slackログ保存bot
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
| `users:read` | 表示名の取得 |
| `users:read.email` | ユーザーのメールアドレス取得（プライベートチャンネルの共有制御に必要） |
| `files:read` | 添付ファイルのダウンロード |
| `chat:write` | botのメッセージ送信（コマンド応答に必要） |

**Event Subscriptions** → **Enable Events** をON → **Subscribe to bot events** に以下を追加:

- `message.channels` — パブリックチャンネルのメッセージ
- `message.groups` — プライベートチャンネルのメッセージ
- `app_mention` — botへのメンション（コマンド応答に必要）
- `member_joined_channel` — メンバー参加時に共有を自動追加
- `member_left_channel` — メンバー退出時に共有を自動解除
- `channel_rename` / `group_rename` — チャンネル名変更に追従
- `user_change` — 表示名の変更に追従

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

### Raspberry Pi / ヘッドレス機で動かす場合

常時稼働させるなら Raspberry Pi 4 で十分動きます。Socket Mode なので**受信ポートを開ける必要はありません**。

**64bit版の Raspberry Pi OS（Bookworm 以降）を使ってください。**

- Bookworm は Python 3.11 なのでそのまま使えます（**Bullseye は 3.9 なので動きません**）
- 32bit版（armhf）は `cryptography` のビルド済みホイールが無く、Rust でのソースビルドになります

依存パッケージのうちネイティブ拡張を持つのは `cryptography` / `cffi` / `protobuf` /
`charset_normalizer` の4つだけで、arm64 ならすべてホイールが入ります。

#### Google 認証をヘッドレスで通す

`setup.py` の Google 認証はブラウザを開いて `localhost` で受け取るため、SSH 越しだとそのままでは完了できません。方法は2つあります。

**A. SSHポートフォワード**（Pi 上で完結させたい場合）

手元の端末から:

```bash
ssh -L 8080:localhost:8080 pi@raspberrypi.local
```

Pi 側で:

```bash
python setup.py --auth-port 8080
```

表示されたURLを**手元のブラウザ**で開けば、リダイレクトがフォワード経由でPiに届きます。

**B. 認証だけ手元のPCで済ませる**（簡単）

手元のPCで `python setup.py` を通したあと、生成された `client_secret.json` と
`drive_token.json` を Pi にコピーします。`setup.py` は既存の認証を再利用するので、
Pi 側ではブラウザが不要になります。

```bash
scp client_secret.json drive_token.json pi@raspberrypi.local:~/slack-log-bot/
```

常駐は後述の [systemd の手順](#systemd-で常駐化-linux)がそのまま使えます。

---

### 複数のワークスペースで使う場合

1つのワークスペースにつき **1ディレクトリ・1プロセス**です。
`setup.py` が作る Drive フォルダと索引スプレッドシートには**ワークスペース名が入る**ため、
同じGoogleアカウントでも混ざりません。

```
📁 Slack ログ - 研究室        ← ワークスペースA
📁 Slack ログ - サークル      ← ワークスペースB
```

> 名前を分けていないと、2つ目のワークスペースが1つ目のフォルダを名前で見つけて再利用し、
> 同名チャンネル（`#general` など）のログが同じスプレッドシートに混ざります。

2つ目以降は、別ディレクトリにクローンして `setup.py` を実行します。
Google の認証情報は使い回せるので、コピーすれば Step 2 とブラウザ認証を省けます。

```bash
git clone https://github.com/nnnnnnnnnke/slack-log-bot.git ~/Documents/slack-log-bot-B
cd ~/Documents/slack-log-bot-B
cp ~/Documents/slack-log-bot/client_secret.json ~/Documents/slack-log-bot/drive_token.json .
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python setup.py
```

Slack App は**ワークスペースごとに作成が必要**です（Step 1 をそのワークスペースで実施）。

---

### Step 3: botのインストール

```bash
git clone https://github.com/nnnnnnnnnke/slack-log-bot.git
cd slack-log-bot

uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -r requirements.txt
```

> [!IMPORTANT]
> **Python 3.10 以上が必要です。**
> macOS に最初から入っている `/usr/bin/python3` は 3.9 なので、
> `python3 -m venv` だとそれを拾って `TypeError: unsupported operand type(s) for |`
> で起動に失敗します。[uv](https://docs.astral.sh/uv/) を使うか、
> 3.10以上の Python を明示してください。

<details>
<summary>uv を使わない場合</summary>

```bash
python3.13 -m venv .venv     # バージョンを明示する
.venv/bin/pip install -r requirements.txt
```

</details>

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
Slackからコマンドでも実行できます: `@Slackログ保存bot backfill`

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

- botがチャンネルに招待されているか確認: `/invite @Slackログ保存bot`
- プライベートチャンネルの場合、`groups:history` と `groups:read` スコープがあるか確認

### botのメンションに反応しない

- Event Subscriptionsで `app_mention` が追加されているか確認
- Socket Modeが有効になっているか確認

---

## ライセンス

MIT
