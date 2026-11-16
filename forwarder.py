#!/usr/bin/env python3
"""
大学メール転送スクリプト（IMAP で受信 → SMTP で転送先へ送信）

- 初回起動時、その時点で受信箱にあるメールはすべて「転送済み」として記録し、
  それ以降に届いたメールだけを転送する（過去のメールは転送しない）。
- 転送済みの位置（UID）を state.json に記録するので、再起動しても二重転送しない。
- 元のメールは未読のまま残る（読み取り専用でフォルダを開き、BODY.PEEK で取得する）。

標準ライブラリのみ使用。Python 3.9 以上。
"""
from __future__ import annotations

import argparse
import base64
import configparser
import email
import html
import imaplib
import json
import logging
import os
import re
import smtplib
import ssl
import sys
import time
import uuid
from dataclasses import dataclass
from email.message import EmailMessage
from email.policy import SMTP as SMTP_POLICY
from email.policy import default as DEFAULT_POLICY
from email.utils import formatdate, make_msgid
from pathlib import Path

log = logging.getLogger("mail-forwarder")

LOOP_HEADER = "X-Mail-Forwarder"
EXIT_AUTH_FAILED = 2  # systemd の RestartPreventExitStatus と合わせる


class AuthError(Exception):
    """ログイン失敗。繰り返すとアカウントがロックされる恐れがあるので再試行しない。"""


# ---------------------------------------------------------------- 設定

@dataclass
class Settings:
    imap_host: str
    imap_port: int
    imap_security: str
    imap_user: str
    imap_password: str
    folder: str
    smtp_host: str
    smtp_port: int
    smtp_security: str
    smtp_user: str
    smtp_password: str
    forward_to: list
    forward_from: str
    poll_interval: int
    max_attach_bytes: int
    from_contains: list
    subject_contains: list


def _split(value: str) -> list:
    return [x.strip() for x in value.split(",") if x.strip()]


def load_settings(path: Path) -> Settings:
    if not path.exists():
        raise SystemExit(f"設定ファイルがありません: {path}")
    if path.stat().st_mode & 0o077:
        log.warning("%s が他のユーザーからも読める状態です。chmod 600 %s を実行してください", path, path)

    cp = configparser.ConfigParser(interpolation=None)
    cp.read(path, encoding="utf-8")
    for name in ("imap", "smtp", "forward"):
        if not cp.has_section(name):
            raise SystemExit(f"設定ファイルに [{name}] セクションがありません")
    imap, smtp, fwd = cp["imap"], cp["smtp"], cp["forward"]
    flt = cp["filter"] if cp.has_section("filter") else {}

    def req(sec, key):
        value = sec.get(key, "").strip()
        if not value:
            raise SystemExit(f"設定 [{sec.name}] {key} が空です")
        return value

    def security(sec, default):
        value = sec.get("security", default).strip().lower()
        if value not in ("ssl", "starttls", "none"):
            raise SystemExit(f"設定 [{sec.name}] security は ssl / starttls / none のどれかにしてください")
        if value == "none":
            log.warning("[%s] が暗号化なし（none）です。パスワードが平文で送られます", sec.name)
        return value

    imap_user = req(imap, "user")
    imap_password = req(imap, "password")
    forward_from = fwd.get("from", "").strip() or (imap_user if "@" in imap_user else "")
    if not forward_from:
        raise SystemExit("[forward] from に自分の大学メールアドレスを書いてください")
    forward_to = _split(req(fwd, "to"))

    return Settings(
        imap_host=req(imap, "host"),
        imap_port=imap.getint("port", 993),
        imap_security=security(imap, "ssl"),
        imap_user=imap_user,
        imap_password=imap_password,
        folder=imap.get("folder", "INBOX").strip() or "INBOX",
        smtp_host=req(smtp, "host"),
        smtp_port=smtp.getint("port", 587),
        smtp_security=security(smtp, "starttls"),
        # 空なら IMAP と同じユーザー名・パスワードを使う（Thunderbird でも大抵同じ）
        smtp_user=smtp.get("user", "").strip() or imap_user,
        smtp_password=smtp.get("password", "").strip() or imap_password,
        forward_to=forward_to,
        forward_from=forward_from,
        poll_interval=max(15, fwd.getint("poll_interval", 60)),
        max_attach_bytes=int(float(fwd.get("max_attach_mb", "20")) * 1024 * 1024),
        from_contains=_split(flt.get("from_contains", "")),
        subject_contains=_split(flt.get("subject_contains", "")),
    )


# ---------------------------------------------------------------- 状態ファイル

def load_state(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError) as e:
        # 勝手に初期化すると転送漏れが起きるので止める
        raise SystemExit(f"状態ファイル {path} が読めません（{e}）。削除して --init し直してください")


def save_state(path: Path, uidvalidity: int, last_uid: int) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({
        "uidvalidity": uidvalidity,
        "last_uid": last_uid,
        "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
    }), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------- IMAP

def _quote(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def connect_imap(s: Settings):
    """接続してフォルダを読み取り専用で開く。(imap, uidvalidity, メール数) を返す。"""
    ctx = ssl.create_default_context()
    if s.imap_security == "ssl":
        imap = imaplib.IMAP4_SSL(s.imap_host, s.imap_port, ssl_context=ctx, timeout=60)
    else:
        imap = imaplib.IMAP4(s.imap_host, s.imap_port, timeout=60)
        if s.imap_security == "starttls":
            imap.starttls(ssl_context=ctx)
    try:
        imap.login(s.imap_user, s.imap_password)
    except imaplib.IMAP4.error as e:
        _safe_logout(imap)
        raise AuthError(f"IMAP のログインに失敗しました: {e}") from e

    typ, data = imap.select(_quote(s.folder), readonly=True)
    if typ != "OK":
        _safe_logout(imap)
        raise RuntimeError(f"フォルダ {s.folder} を開けません: {data}")
    count = int(data[0]) if data and data[0] else 0

    _, resp = imap.response("UIDVALIDITY")
    if resp and resp[0]:
        uidvalidity = int(resp[0])
    else:
        _, st = imap.status(_quote(s.folder), "(UIDVALIDITY)")
        m = re.search(rb"UIDVALIDITY (\d+)", st[0] or b"")
        if not m:
            raise RuntimeError("UIDVALIDITY を取得できません")
        uidvalidity = int(m.group(1))
    return imap, uidvalidity, count


def _safe_logout(imap) -> None:
    try:
        imap.logout()
    except Exception:
        pass


def _uid_search(imap, criterion: str) -> list:
    typ, data = imap.uid("SEARCH", None, criterion)
    if typ != "OK":
        raise RuntimeError(f"UID SEARCH {criterion} が失敗しました: {data}")
    return sorted(int(x) for x in (data[0] or b"").split())


def max_uid(imap) -> int:
    uids = _uid_search(imap, "UID *")  # 一番大きい UID だけが返る
    return uids[-1] if uids else 0


def new_uids(imap, last_uid: int) -> list:
    # "UID n:*" は n より大きい UID が無くても最大 UID を返す仕様なので絞り込む
    return [u for u in _uid_search(imap, f"UID {last_uid + 1}:*") if u > last_uid]


def fetch_raw(imap, uid: int):
    typ, data = imap.uid("FETCH", str(uid), "(BODY.PEEK[])")
    if typ != "OK":
        raise RuntimeError(f"UID {uid} の取得に失敗しました: {data}")
    for item in data or []:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
            return item[1]
    return None


# ---------------------------------------------------------------- メール組み立て

def _header(msg, name: str) -> str:
    try:
        value = str(msg.get(name, "") or "")
    except Exception:
        return ""
    return re.sub(r"[\r\n]+\s*", " ", value).strip()


def matches_filter(s: Settings, msg) -> bool:
    def hit(value: str, needles: list) -> bool:
        return not needles or any(n.lower() in value.lower() for n in needles)
    return hit(_header(msg, "From"), s.from_contains) and hit(_header(msg, "Subject"), s.subject_contains)


def extract_text(msg, limit: int = 20000) -> str:
    try:
        part = msg.get_body(preferencelist=("plain", "html"))
        if part is None:
            return ""
        text = part.get_content()
        if part.get_content_subtype() == "html":
            text = re.sub(r"(?is)<(script|style)\b.*?</\1>", "", text)
            text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>", "\n", text)
            text = html.unescape(re.sub(r"<[^>]+>", "", text))
            text = re.sub(r"[ \t]+\n", "\n", text)
            text = re.sub(r"\n{3,}", "\n\n", text)
        text = text.strip()
        if len(text) > limit:
            text = text[:limit] + "\n…（以下省略。全文は添付の元メールを見てください）"
        return text
    except Exception as e:  # 文字コード不明など
        log.debug("本文の取り出しに失敗: %s", e)
        return ""


def build_forward(s: Settings, raw: bytes, msg, attach_original: bool = True) -> bytes:
    """本文をテキストで載せ、元メールをそのまま .eml として添付した転送メールを作る。"""
    subject = _header(msg, "Subject")
    attach = attach_original and len(raw) <= s.max_attach_bytes

    lines = [
        "---------- 転送メッセージ ----------",
        f"From: {_header(msg, 'From')}",
        f"Date: {_header(msg, 'Date')}",
        f"Subject: {subject}",
        f"To: {_header(msg, 'To')}",
        "",
        extract_text(msg) or "（本文をテキストで表示できません。添付の元メールを開いてください）",
    ]
    if not attach:
        why = (f"サイズが大きい（{len(raw) / 1024 / 1024:.1f}MB）" if attach_original
               else "転送先のサーバーに受け付けられなかった")
        lines += ["", f"※元のメールは{why}ため添付していません。大学のメールで確認してください。"]
    body = "\n".join(lines)

    head = EmailMessage(policy=SMTP_POLICY)
    head["From"] = s.forward_from
    head["To"] = ", ".join(s.forward_to)
    head["Subject"] = "Fwd: " + (subject or "(件名なし)")
    head["Date"] = formatdate(localtime=True)
    head["Message-ID"] = make_msgid(domain=s.forward_from.rpartition("@")[2] or None)
    orig_id = _header(msg, "Message-ID")
    if orig_id:
        head["References"] = orig_id
    head[LOOP_HEADER] = "1"
    # 本文は自前で組むので、ヘッダー部分だけを生成する
    headers = head.as_bytes().rstrip(b"\r\n") + b"\r\n"

    boundary = "=_fwd_" + uuid.uuid4().hex
    out = [
        headers,
        b"MIME-Version: 1.0\r\n",
        f'Content-Type: multipart/mixed; boundary="{boundary}"\r\n\r\n'.encode(),
        f"--{boundary}\r\n".encode(),
        b'Content-Type: text/plain; charset="utf-8"\r\nContent-Transfer-Encoding: base64\r\n\r\n',
        base64.encodebytes(body.encode("utf-8")).replace(b"\n", b"\r\n"),
    ]
    if attach:
        original = re.sub(rb"\r?\n", b"\r\n", raw)  # 元メールは一切書き換えずに添付
        cte = b"7bit" if original.isascii() else b"8bit"
        out += [
            f"--{boundary}\r\n".encode(),
            b"Content-Type: message/rfc822\r\n"
            b'Content-Disposition: attachment; filename="original.eml"\r\n'
            b"Content-Transfer-Encoding: " + cte + b"\r\n\r\n",
            original if original.endswith(b"\r\n") else original + b"\r\n",
        ]
    out.append(f"--{boundary}--\r\n".encode())
    return b"".join(out)


# ---------------------------------------------------------------- SMTP

def _open_smtp(s: Settings):
    ctx = ssl.create_default_context()
    if s.smtp_security == "ssl":
        smtp = smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, context=ctx, timeout=60)
    else:
        smtp = smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=60)
    try:
        smtp.ehlo()
        if s.smtp_security == "starttls":
            smtp.starttls(context=ctx)
            smtp.ehlo()
        if s.smtp_user:
            try:
                smtp.login(s.smtp_user, s.smtp_password)
            except smtplib.SMTPAuthenticationError as e:
                raise AuthError(f"SMTP のログインに失敗しました: {e}") from e
    except BaseException:
        _safe_quit(smtp)
        raise
    return smtp


def _safe_quit(smtp) -> None:
    try:
        smtp.quit()
    except Exception:
        pass


def send_mail(s: Settings, data: bytes) -> None:
    smtp = _open_smtp(s)
    try:
        opts = ["BODY=8BITMIME"] if smtp.has_extn("8bitmime") else []
        smtp.sendmail(s.forward_from, s.forward_to, data, mail_options=opts)
    finally:
        _safe_quit(smtp)


# ---------------------------------------------------------------- 本体

def baseline(imap, uidvalidity: int, state_path: Path, reason: str) -> int:
    top = max_uid(imap)
    save_state(state_path, uidvalidity, top)
    log.info("%s: 今あるメール（UID %d まで）を転送済みとして記録しました。これ以降に届くメールを転送します。",
             reason, top)
    return top


def forward_one(s: Settings, uid: int, raw: bytes) -> None:
    msg = email.message_from_bytes(raw, policy=DEFAULT_POLICY)
    subject = _header(msg, "Subject")
    if msg.get(LOOP_HEADER):
        log.info("UID %d: このスクリプトが転送したメールなので転送しません（ループ防止）: %s", uid, subject)
        return
    if not matches_filter(s, msg):
        log.info("UID %d: 条件に合わないので転送しません: %s", uid, subject)
        return
    try:
        send_mail(s, build_forward(s, raw, msg))
    except smtplib.SMTPDataError as e:
        if e.smtp_code < 500:
            raise  # 一時的なエラー → 次回やり直す
        # 内容やサイズが原因で拒否された → 元メールを添付せずにもう一度だけ送る
        log.warning("UID %d: 転送先に拒否されました（%s）。元メールを添付せずに送り直します", uid, e)
        try:
            send_mail(s, build_forward(s, raw, msg, attach_original=False))
        except smtplib.SMTPDataError as e2:
            if e2.smtp_code < 500:
                raise
            log.error("UID %d: 転送できませんでした（%s）。このメールは飛ばします: %s", uid, e2, subject)
            return
    log.info("UID %d: 転送しました: %s", uid, subject)


def process_new_mail(imap, uidvalidity: int, s: Settings, state_path: Path) -> None:
    state = load_state(state_path)
    if state is None:
        baseline(imap, uidvalidity, state_path, "初回起動")
        return
    if state.get("uidvalidity") != uidvalidity:
        log.warning("フォルダの UIDVALIDITY が変わりました（サーバー側の作り直しなど）。二重転送を防ぐため記録し直します")
        baseline(imap, uidvalidity, state_path, "再記録")
        return

    last = int(state["last_uid"])
    for uid in new_uids(imap, last):
        raw = fetch_raw(imap, uid)
        if raw is None:
            log.warning("UID %d を取得できませんでした（削除された可能性）。飛ばします", uid)
        else:
            forward_one(s, uid, raw)  # 送信に失敗したら例外 → 位置を進めずに次回やり直す
        save_state(state_path, uidvalidity, uid)


def run_forever(s: Settings, state_path: Path) -> None:
    backoff = 30
    while True:
        imap = None
        try:
            imap, uidvalidity, _ = connect_imap(s)
            log.info("IMAP に接続しました（%s / %s）。%d 秒ごとに新着を確認します",
                     s.imap_host, s.folder, s.poll_interval)
            backoff = 30
            while True:
                process_new_mail(imap, uidvalidity, s, state_path)
                time.sleep(s.poll_interval)
                imap.noop()
        except AuthError:
            raise
        except (imaplib.IMAP4.error, OSError, smtplib.SMTPException, RuntimeError) as e:
            log.warning("エラー: %s（%d 秒後に再接続します）", e, backoff)
        finally:
            if imap is not None:
                _safe_logout(imap)
        time.sleep(backoff)
        backoff = min(backoff * 2, 900)


# ---------------------------------------------------------------- コマンド

def cmd_check(s: Settings, state_path: Path) -> None:
    imap, uidvalidity, count = connect_imap(s)
    try:
        top = max_uid(imap)
    finally:
        _safe_logout(imap)
    print(f"IMAP: OK  {s.imap_host}:{s.imap_port} フォルダ {s.folder}  メール {count} 通  最新 UID {top}")
    state = load_state(state_path)
    if state is None:
        print("状態: まだ記録なし（初回起動時に、その時点までのメールを転送済みとして記録します）")
    elif state.get("uidvalidity") != uidvalidity:
        print("状態: UIDVALIDITY が記録と違います（起動時に記録し直します）")
    else:
        print(f"状態: UID {state['last_uid']} まで転送済み（未転送 {max(0, top - int(state['last_uid']))} 通程度）")
    _safe_quit(_open_smtp(s))
    print(f"SMTP: OK  {s.smtp_host}:{s.smtp_port}（ログインまで確認。送信はしていません）")
    print(f"転送先: {', '.join(s.forward_to)}  差出人: {s.forward_from}")


def cmd_test_send(s: Settings) -> None:
    msg = EmailMessage(policy=SMTP_POLICY)
    msg["From"] = s.forward_from
    msg["To"] = ", ".join(s.forward_to)
    msg["Subject"] = "メール転送スクリプトのテスト"
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=s.forward_from.rpartition("@")[2] or None)
    msg[LOOP_HEADER] = "1"
    msg.set_content("Raspberry Pi のメール転送スクリプトからのテスト送信です。\nこのメールが届いていれば SMTP の設定は正しいです。")
    send_mail(s, msg.as_bytes())
    print(f"テストメールを {', '.join(s.forward_to)} に送りました")


def main(argv=None) -> int:
    here = Path(__file__).resolve().parent
    p = argparse.ArgumentParser(description="大学メールを IMAP で受信して転送先へ送る")
    p.add_argument("--config", type=Path, default=here / "config.ini", help="設定ファイル（既定: スクリプトと同じ場所の config.ini）")
    p.add_argument("--state", type=Path, help="状態ファイル（既定: 設定ファイルと同じ場所の state.json）")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--check", action="store_true", help="IMAP と SMTP にログインできるか確認する（送信しない）")
    g.add_argument("--test-send", action="store_true", help="転送先にテストメールを1通送る")
    g.add_argument("--init", action="store_true", help="今あるメールを転送済みとして記録し直して終了する")
    g.add_argument("--once", action="store_true", help="新着を1回だけ確認して終了する")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    fmt = "%(levelname)s %(message)s" if os.environ.get("JOURNAL_STREAM") else "%(asctime)s %(levelname)s %(message)s"
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format=fmt)

    state_path = args.state or args.config.resolve().with_name("state.json")
    s = load_settings(args.config)
    try:
        if args.check:
            cmd_check(s, state_path)
        elif args.test_send:
            cmd_test_send(s)
        elif args.init or args.once:
            imap, uidvalidity, _ = connect_imap(s)
            try:
                if args.init:
                    baseline(imap, uidvalidity, state_path, "--init")
                else:
                    process_new_mail(imap, uidvalidity, s, state_path)
            finally:
                _safe_logout(imap)
        else:
            run_forever(s, state_path)
    except AuthError as e:
        log.error("%s。パスワードを確認してください（アカウントのロックを防ぐため自動では再試行しません）", e)
        return EXIT_AUTH_FAILED
    except KeyboardInterrupt:
        return 0
    except (imaplib.IMAP4.error, OSError, smtplib.SMTPException, RuntimeError) as e:
        log.error("%s", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
