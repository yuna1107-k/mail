# 大学メール転送（Raspberry Pi / Ubuntu Server 用）

大学のメールを IMAP で受信し、SMTP で転送先（Gmail など）へ送る Python スクリプトです。
Thunderbird のフィルタで行っていた転送を、画面のない Raspberry Pi で常時動かすためのものです。

- **過去のメールは転送しない**：初回起動時に、その時点で各フォルダにあるメールを全部「転送済み」として記録し、それ以降に届いたものだけを転送します。
- **二重転送しない**：どこまで転送したかを `state.json` に記録するので、再起動しても同じメールを送り直しません。
- **受信箱以外のフォルダも対象にできる**：`folder = *` で全フォルダ（送信済み・下書き・ゴミ箱・迷惑メールは自動で除外）、`folder = INBOX, 講義` のように指定もできます。
- **元メールは未読のまま**：大学側のメールの既読状態は変えません。
- **転送の形**：Thunderbird の「インラインで転送」と同じ形です。件名は `Fwd: 元の件名`。元メールに添付ファイルがあれば、それも付けて転送します。

  ```
  -------- Forwarded Message --------
  Subject:	元の件名
  Date:	Mon, 16 Nov 2026 09:57:02 +0900
  From:	差出人 <sender@example.ac.jp>
  Reply-To:	返信先（元メールにあるときだけ）
  To:	宛先 <you@example.ac.jp>


  元のメールの本文（HTML だけのメールはテキストに変換し、リンクは [文字](URL) の形で残す）
  ```
- 標準ライブラリだけで動くので、`pip install` は不要です（Python 3.9 以上）。

## ファイル

| ファイル | 内容 |
|---|---|
| `forwarder.py` | 転送スクリプト本体 |
| `config.example.ini` | 設定ファイルのひな形（コピーして `config.ini` にする） |
| `mail-forwarder.service` | systemd のサービスファイル |

## 導入手順

### 1. Pi にファイルを置く（Mac から）

```bash
ssh YOUR_USER@raspberrypi.local 'mkdir -p ~/mail-forwarder'
scp forwarder.py config.example.ini mail-forwarder.service YOUR_USER@raspberrypi.local:~/mail-forwarder/
```

### 2. 設定ファイルを作る（Pi で）

```bash
cd ~/mail-forwarder
cp config.example.ini config.ini
chmod 600 config.ini
nano config.ini
```

Thunderbird のアカウント設定を見ながら、`[imap]`・`[smtp]`・`[forward]` を埋めます。

### 3. 接続を確認する

```bash
python3 forwarder.py --check
```

IMAP と SMTP の両方が `OK` になれば設定は正しいです（メールは送りません）。

```bash
python3 forwarder.py --test-send
```

転送先にテストメールが1通届くか確認します。

### 4. Thunderbird から切り替える

**二重転送や転送漏れを防ぐため、この順番で行ってください。**

1. Pi で、今あるメールを転送済みとして記録する
   ```bash
   python3 forwarder.py --init
   ```
2. **すぐに** Thunderbird の転送フィルタを無効にする（またはフィルタを削除する）
3. 下の「5. 常時動かす」でサービスを起動する

1 と 2 の間に届いたメールは両方から転送される（重複する）ことがありますが、取りこぼしはありません。
2 と 3 の間に届いたメールは、サービス起動時に Pi が転送します。

### 5. 常時動かす（systemd）

```bash
sed -i "s/YOUR_USER/$USER/g" ~/mail-forwarder/mail-forwarder.service
sudo cp ~/mail-forwarder/mail-forwarder.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mail-forwarder
```

状態とログの確認：

```bash
systemctl status mail-forwarder
```

```bash
journalctl -u mail-forwarder -f
```

## よくある操作

| やりたいこと | コマンド |
|---|---|
| 設定を変えたあと反映する | `sudo systemctl restart mail-forwarder` |
| 一時的に止める | `sudo systemctl stop mail-forwarder` |
| 止めている間のメールを転送せずに再開する | `python3 forwarder.py --init` → `sudo systemctl start mail-forwarder` |
| 1回だけ新着を確認する（テスト用） | `python3 forwarder.py --once` |

止めている間に届いたメールは、普通に再開すれば全部転送されます。転送したくない場合だけ、再開前に `--init` してください。

## すべてのフォルダを対象にする

`config.ini` の `[imap]` を次のようにします。

```ini
folder = *
# ほかに除外したいフォルダがあれば
exclude_folders = アーカイブ, 古いメール
```

- 送信済み・下書き・ゴミ箱・迷惑メール（`Sent` `Drafts` `Trash` `Junk` など）は自動で除外します。どのフォルダが対象になるかは `python3 forwarder.py --check` で確認できます。
- 後から作ったフォルダも、次の確認のときに自動で対象に加わります。そのとき中にあるメールは転送せず、それ以降に入ったメールを転送します。
- 動かしている途中で `folder = INBOX` から `folder = *` に変えても大丈夫です。他のフォルダに今あるメールは転送されません。
- **古いメールをフォルダ間で移動しても転送しません。** IMAP ではメールを移動すると移動先で新着扱いになりますが、サーバーに届いた日時（INTERNALDATE）が転送開始より前のメールは飛ばします。
- **同じメールは1回しか転送しません。** 転送したメールを後で別フォルダへ移動したり、サーバー側の振り分けで複数のフォルダに入ったりしても、Message-ID で判定して重複を防ぎます。

## 動きの補足

- 新着は `poll_interval` 秒ごと（既定 60 秒）に確認します。
- 通信エラーのときは 30 秒〜15 分の間隔で自動的に再接続します。転送に失敗したメールは、次の確認でもう一度送ります。
- **パスワードが違ってログインに失敗したときは、自動では再試行せずに止まります**（何度も失敗して大学のアカウントがロックされるのを防ぐため）。`config.ini` を直してから `sudo systemctl restart mail-forwarder` してください。
- 添付ファイルの合計が `max_attach_mb`（既定 20MB）を超えるメールや、転送先に拒否されたメール（サイズ超過など）は、添付ファイルを外して本文だけで送ります。本文の最後に、外したファイル名を書き添えます。
- このスクリプトが送ったメールが大学のアドレスに戻ってきても、もう一度は転送しません（ループ防止）。
- 自宅など学外に Pi を置く場合、大学の IMAP/SMTP に学外から接続できる必要があります。Thunderbird を自宅で使えていたなら問題ありません。
