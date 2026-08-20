# Raspberry Pi へのデプロイ

Ubuntu Server for Raspberry Pi（**arm64**）の cloud-init 設定です。
SDカードを書いたあと、ブートパーティション（`system-boot`）にこの2ファイルを置くだけで、
起動時にリポジトリの取得・venv構築・systemd登録まで済みます。

```bash
cp user-data network-config /Volumes/system-boot/
```

## 事前に書き換えるところ

`user-data` の以下を自分の値にしてください。

| 項目 | 生成方法 |
|------|---------|
| `users[].name` | 使いたいユーザー名 |
| `passwd` | `mkpasswd -m sha-512` |
| `ssh_authorized_keys` | `cat ~/.ssh/id_ed25519.pub` |

`network-config` は既定で eth0 の DHCP です。無線を使うなら `wifis` の
SSID とパスフレーズを埋め、有線を使わないなら `ethernets` を消してください。
SSH先を固定したい場合は `dhcp4: false` にして `addresses` と `routes` を書きます。

## 起動後にやること

**認証情報はこの設定に含めていません。** ブートパーティションは FAT32 で
パーミッションを持てず、カードを差した人なら誰でも読めてしまうためです。
bot は `.env` が無いうちは起動しない（`ConditionPathExists`）ので、
送ってから起動します。

```bash
scp .env client_secret.json drive_token.json nukui@slack-log-bot.local:~/slack-log-bot/
ssh nukui@slack-log-bot.local 'chmod 600 ~/slack-log-bot/.env ~/slack-log-bot/*.json && sudo systemctl start slack-log-bot'
```

Google 認証をまだ通していない場合は、手元のPCで `python setup.py` を済ませてから
上のファイルを送るのが簡単です（Pi 上で完結させたい場合は `--auth-port` を使います）。

## 確認

```bash
ssh nukui@slack-log-bot.local
systemctl status slack-log-bot          # 常駐
journalctl -u slack-log-bot -f          # ログ
systemctl status slack-log-bot-install  # 初回のクローンとvenv構築
systemctl list-timers slack-log-bot*    # 週次収集
```

## 注意

- **arm64 のイメージを使ってください。** 32bit（armhf）だと `cryptography` の
  ホイールが無く、Rust でのソースビルドになります。
- Python 3.10 以上が必要です。Ubuntu 24.04 以降なら問題ありません。
- 初回起動は pip の取得で数分かかります。`slack-log-bot-install.service` の完了を待ってください。
