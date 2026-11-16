#!/usr/bin/env python3
"""
大学メール転送スクリプト（IMAP で受信 → SMTP で転送先へ送信）

- 初回起動時、その時点で各フォルダにあるメールはすべて「転送済み」として記録し、
  それ以降に届いたメールだけを転送する（過去のメールは転送しない）。
- 転送済みの位置（フォルダごとの UID）を state.json に記録するので、再起動しても二重転送しない。
- 古いメールを別フォルダへ移動しても転送しない（受信日時が転送開始より前のものは飛ばす）。
- 同じメールが複数のフォルダに現れても1回しか転送しない（Message-ID で重複を判定）。
- 元のメールは未読のまま残る（読み取り専用でフォルダを開き、BODY.PEEK で取得する）。

標準ライブラリのみ使用。Python 3.9 以上。
"""
from __future__ import annotations

import argparse
import base64
import configparser
import email
import hashlib
import html
import imaplib
import json
import logging
import mimetypes
import os
import re
import smtplib
import ssl
import sys
import time
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.policy import SMTP as SMTP_POLICY
from email.policy import default as DEFAULT_POLICY
from email.utils import formatdate, make_msgid
from pathlib import Path

log = logging.getLogger("mail-forwarder")

LOOP_HEADER = "X-Mail-Forwarder"
EXIT_AUTH_FAILED = 2  # systemd の RestartPreventExitStatus と合わせる
STATE_VERSION = 2
SINCE_MARGIN = 300  # 転送開始時刻の余裕（秒）。Pi とサーバーの時計のずれ対策
MAX_REMEMBERED_IDS = 5000

# folder = * のとき自動で除外するフォルダ
EXCLUDE_FLAGS = {"\\sent", "\\drafts", "\\trash", "\\junk", "\\all", "\\flagged"}
EXCLUDE_NAMES = {
    "sent", "sent items", "sent messages", "sent mail", "送信済み", "送信済みアイテム", "送信済みメール", "送信箱",
    "drafts", "draft", "下書き",
    "trash", "deleted items", "deleted messages", "ゴミ箱", "ごみ箱", "削除済みアイテム",
    "junk", "junk e-mail", "junk email", "spam", "迷惑メール",
    "templates", "テンプレート", "outbox",
}


class AuthError(Exception):
    """ログイン失敗。繰り返すとアカウントがロックされる恐れがあるので再試行しない。"""


class FolderError(Exception):
    """フォルダを開けない。そのフォルダだけ飛ばして続ける。"""


# ---------------------------------------------------------------- 設定

@dataclass
class Settings:
    imap_host: str
    imap_port: int
    imap_security: str
    imap_user: str
    imap_password: str
    folders: list          # ["*"] なら全フォルダ
    exclude_folders: list
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

    @property
    def all_folders(self) -> bool:
        return self.folders == ["*"]


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
    folders = _split(imap.get("folder", "INBOX")) or ["INBOX"]
    if "*" in folders and len(folders) > 1:
        raise SystemExit("[imap] folder に * を使うときは * だけを書いてください（除外は exclude_folders へ）")

    return Settings(
        imap_host=req(imap, "host"),
        imap_port=imap.getint("port", 993),
        imap_security=security(imap, "ssl"),
        imap_user=imap_user,
        imap_password=imap_password,
        folders=folders,
        exclude_folders=_split(imap.get("exclude_folders", "")),
        smtp_host=req(smtp, "host"),
        smtp_port=smtp.getint("port", 587),
        smtp_security=security(smtp, "starttls"),
        # 空なら IMAP と同じユーザー名・パスワードを使う（Thunderbird でも大抵同じ）
        smtp_user=smtp.get("user", "").strip() or imap_user,
        smtp_password=smtp.get("password", "").strip() or imap_password,
        forward_to=_split(req(fwd, "to")),
        forward_from=forward_from,
        poll_interval=max(15, fwd.getint("poll_interval", 60)),
        max_attach_bytes=int(float(fwd.get("max_attach_mb", "20")) * 1024 * 1024),
        from_contains=_split(flt.get("from_contains", "")),
        subject_contains=_split(flt.get("subject_contains", "")),
    )


# ---------------------------------------------------------------- 状態ファイル
#
# {
#   "version": 2,
#   "since": 転送開始時刻（UNIX 時間）。これより前に受信したメールは転送しない,
#   "folders": {"INBOX": {"uidvalidity": 123, "last_uid": 456}, ...},
#   "forwarded_ids": [転送済みメールの Message-ID ...]
# }

def new_state() -> dict:
    return {"version": STATE_VERSION, "since": time.time() - SINCE_MARGIN, "folders": {}, "forwarded_ids": []}


def load_state(path: Path, s: Settings):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError) as e:
        # 勝手に初期化すると転送漏れが起きるので止める
        raise SystemExit(f"状態ファイル {path} が読めません（{e}）。削除して --init し直してください")
    if data.get("version") != STATE_VERSION:
        # 旧形式（フォルダ1つだけ）からの移行
        state = new_state()
        name = "INBOX" if s.all_folders else s.folders[0]
        state["folders"][name] = {"uidvalidity": data["uidvalidity"], "last_uid": data["last_uid"]}
        log.info("状態ファイルを新しい形式に変換しました")
        save_state(path, state)
        return state
    return data


def save_state(path: Path, state: dict) -> None:
    state["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------- IMAP

def imap_utf7_encode(name: str) -> str:
    """日本語のフォルダ名を IMAP のフォルダ名の形式（修正 UTF-7）にする。"""
    out, buf = [], []

    def flush():
        if buf:
            b64 = base64.b64encode("".join(buf).encode("utf-16-be")).decode()
            out.append("&" + b64.rstrip("=").replace("/", ",") + "-")
            buf.clear()

    for ch in name:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buf.append(ch)
    flush()
    return "".join(out)


def imap_utf7_decode(name: str) -> str:
    def repl(m):
        chunk = m.group(1)
        if not chunk:
            return "&"
        chunk = chunk.replace(",", "/")
        return base64.b64decode(chunk + "=" * (-len(chunk) % 4)).decode("utf-16-be")
    return re.sub(r"&([^-]*)-", repl, name)


def _quote(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _safe_logout(imap) -> None:
    try:
        imap.logout()
    except Exception:
        pass


def connect_imap(s: Settings):
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
    return imap


_LIST_RE = re.compile(rb'^\((?P<flags>[^)]*)\)\s+(?P<delim>NIL|"(?:[^"\\]|\\.)*")\s*(?P<name>.*)$')


def list_folders(imap) -> list:
    """サーバー上のフォルダを [(名前, 区切り文字, フラグ集合)] で返す。開けないフォルダは除く。"""
    typ, data = imap.list('""', "*")
    if typ != "OK":
        raise RuntimeError(f"フォルダ一覧を取得できません: {data}")
    result = []
    for item in data or []:
        if item is None:
            continue
        if isinstance(item, tuple):  # 名前がリテラル {n} で送られてきた場合
            head, raw_name = item[0], item[1]
        else:
            head, raw_name = item, None
        m = _LIST_RE.match(head.strip())
        if not m:
            log.debug("解釈できない LIST 応答: %r", item)
            continue
        flags = {f.lower() for f in m.group("flags").decode(errors="replace").split()}
        if flags & {"\\noselect", "\\nonexistent"}:
            continue
        delim = m.group("delim")
        delim = "" if delim == b"NIL" else delim[1:-1].replace(b"\\\\", b"\\").decode()
        if raw_name is None:
            raw_name = m.group("name").strip()
            if raw_name.startswith(b'"') and raw_name.endswith(b'"'):
                raw_name = re.sub(rb"\\(.)", rb"\1", raw_name[1:-1])
        result.append((imap_utf7_decode(raw_name.decode("utf-8", errors="replace")), delim, flags))
    return result


def _is_auto_excluded(name: str, delim: str, flags: set) -> bool:
    if flags & EXCLUDE_FLAGS:
        return True
    leaf = name.rsplit(delim, 1)[-1] if delim else name
    return leaf.strip().casefold() in EXCLUDE_NAMES or name.strip().casefold() in EXCLUDE_NAMES


def resolve_folders(imap, s: Settings) -> list:
    """転送の対象にするフォルダ名の一覧を返す。"""
    if not s.all_folders:
        return list(s.folders)
    excluded = {x.casefold() for x in s.exclude_folders}
    names = []
    for name, delim, flags in list_folders(imap):
        if name.casefold() in excluded or _is_auto_excluded(name, delim, flags):
            continue
        names.append(name)
    return names


def select_folder(imap, name: str):
    """フォルダを読み取り専用で開く。(uidvalidity, メール数) を返す。"""
    try:
        typ, data = imap.select(_quote(imap_utf7_encode(name)), readonly=True)
    except imaplib.IMAP4.error as e:
        raise FolderError(f"フォルダ「{name}」を開けません: {e}") from e
    if typ != "OK":
        raise FolderError(f"フォルダ「{name}」を開けません: {data}")
    count = int(data[0]) if data and data[0] else 0
    _, resp = imap.response("UIDVALIDITY")
    if resp and resp[0]:
        return int(resp[0]), count
    _, st = imap.status(_quote(imap_utf7_encode(name)), "(UIDVALIDITY)")
    m = re.search(rb"UIDVALIDITY (\d+)", (st or [b""])[0] or b"")
    if not m:
        raise FolderError(f"フォルダ「{name}」の UIDVALIDITY を取得できません")
    return int(m.group(1)), count


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


def fetch_internaldates(imap, last_uid: int) -> dict:
    """last_uid より後のメールの受信日時（サーバーに届いた時刻）を {UID: UNIX 時間} で返す。"""
    typ, data = imap.uid("FETCH", f"{last_uid + 1}:*", "(INTERNALDATE)")
    if typ != "OK":
        raise RuntimeError(f"受信日時の取得に失敗しました: {data}")
    dates = {}
    for item in data or []:
        line = item[0] if isinstance(item, tuple) else item
        if not isinstance(line, bytes):
            continue
        m = re.search(rb"UID (\d+)", line)
        t = imaplib.Internaldate2tuple(line)
        if m and t:
            dates[int(m.group(1))] = time.mktime(t)
    return dates


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
    """ヘッダーを表示用の文字列にする。アドレスを解釈し直さず、元の文字列をそのまま復号する
    （解釈し直すと「名前 (aizu) <addr>」のコメント部分が消えるため）。"""
    value = None
    for key, raw in msg.raw_items():
        if key.lower() == name.lower():
            value = raw
            break
    if value is None:
        return ""
    value = re.sub(r"\r?\n[ \t]+", " ", str(value))  # 折り返しを戻す
    try:
        value = str(make_header(decode_header(value)))
    except Exception:
        try:
            value = str(msg.get(name, "") or "")
        except Exception:
            pass
    return re.sub(r"[\r\n]+\s*", " ", value).strip()


def message_key(msg, raw: bytes) -> str:
    """同じメールかどうかの判定に使う値。Message-ID が無ければ中身のハッシュ。"""
    mid = _header(msg, "Message-ID")
    return mid if mid else "sha256:" + hashlib.sha256(raw).hexdigest()


def matches_filter(s: Settings, msg) -> bool:
    def hit(value: str, needles: list) -> bool:
        return not needles or any(n.lower() in value.lower() for n in needles)
    return hit(_header(msg, "From"), s.from_contains) and hit(_header(msg, "Subject"), s.subject_contains)


def html_to_text(src: str) -> str:
    """HTML メールをテキストにする。リンクは [文字](URL) の形で残す。"""
    t = re.sub(r"(?is)<(script|style|head|title)\b.*?</\1\s*>", "", src)
    t = re.sub(r"(?s)<!--.*?-->", "", t)
    t = re.sub(r"\s+", " ", t)  # HTML ソース上の改行は表示上の改行ではない

    def link(m):
        href = html.unescape(m.group(1)).strip()
        label = " ".join(html.unescape(re.sub(r"<[^>]+>", "", m.group(2))).split())
        if not href or href.startswith("#"):
            return label
        if not label or label == href:
            return href
        return f"[{label}]({href})"

    t = re.sub(r"(?is)<a\b[^>]*?\bhref\s*=\s*[\"']([^\"']*)[\"'][^>]*>(.*?)</a\s*>", link, t)
    t = re.sub(r"(?i)<br\s*/?>", "\n", t)
    t = re.sub(r"(?i)<li\b[^>]*>", "\n- ", t)
    t = re.sub(r"(?i)</?(p|div|h[1-6]|ul|ol|table|tr|blockquote|section|article|header|footer)\b[^>]*>", "\n\n", t)
    t = re.sub(r"(?i)</t[dh]\s*>", " ", t)
    t = html.unescape(re.sub(r"<[^>]+>", "", t)).replace("\xa0", " ")
    t = "\n".join(line.strip() for line in t.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def extract_text(msg, limit: int = 20000) -> str:
    """本文をテキストで取り出す。テキスト版があればそれを、無ければ HTML 版を変換して使う。"""
    try:
        part = msg.get_body(preferencelist=("plain", "html"))
        if part is None:
            return ""
        text = part.get_content()
        if part.get_content_subtype() == "html":
            text = html_to_text(text)
        else:
            text = "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").split("\n")).strip("\n")
        if len(text) > limit:
            text = text[:limit] + "\n…（以下省略。全文は大学のメールで確認してください）"
        return text
    except Exception as e:  # 文字コード不明など
        log.debug("本文の取り出しに失敗: %s", e)
        return ""


def collect_attachments(msg) -> list:
    """元メールの添付ファイルを [(ファイル名, maintype, subtype, データ)] で返す。HTML 内の埋め込み画像は除く。"""
    result = []
    for part in msg.iter_attachments():
        try:
            maintype = part.get_content_maintype()
            if (maintype == "image" and part.get("Content-ID")
                    and part.get_content_disposition() != "attachment"):
                continue  # HTML メールに埋め込まれたロゴなど
            if maintype == "multipart":
                continue
            filename = part.get_filename() or ""
            if maintype == "message":  # メールにメールが添付されている場合
                inner = part.get_payload(0) if part.is_multipart() else None
                data = inner.as_bytes() if inner is not None else (part.get_payload(decode=True) or b"")
                result.append((filename or "message.eml", "application", "octet-stream", data))
                continue
            data = part.get_payload(decode=True) or b""
            if not filename:
                filename = "attachment" + (mimetypes.guess_extension(part.get_content_type()) or "")
            result.append((filename, maintype, part.get_content_subtype(), data))
        except Exception as e:
            log.warning("添付ファイルを取り出せませんでした: %s", e)
    return result


# 転送メールの形。送れなかったときは、より小さい形にして送り直す
LEVEL_FULL, LEVEL_NO_ATTACHMENTS, LEVEL_HEADERS_ONLY = 0, 1, 2
LEVEL_LABEL = {LEVEL_FULL: "", LEVEL_NO_ATTACHMENTS: "（添付ファイルなし）", LEVEL_HEADERS_ONLY: "（ヘッダーのみ）"}


def build_forward(s: Settings, msg, level: int = LEVEL_FULL, reason: str = "") -> bytes:
    """Thunderbird の「インラインで転送」と同じ形の転送メールを作る。元メールの添付ファイルも付ける。

    level=LEVEL_NO_ATTACHMENTS なら添付ファイルを外し、LEVEL_HEADERS_ONLY なら本文も外す。
    外したときは、その理由（reason）を本文の最後に書き添える。
    """
    subject = _header(msg, "Subject")

    lines = ["-------- Forwarded Message --------"]
    for label, name in (("Subject", "Subject"), ("Date", "Date"), ("From", "From"),
                        ("Reply-To", "Reply-To"), ("To", "To"), ("CC", "Cc")):
        value = _header(msg, name)
        if value or name in ("Subject", "Date", "From", "To"):
            lines.append(f"{label}:\t{value}")
    lines += ["", ""]
    if level < LEVEL_HEADERS_ONLY:
        lines.append(extract_text(msg) or "（本文をテキストで表示できません。大学のメールで確認してください）")

    try:
        attachments = collect_attachments(msg)
    except Exception as e:
        log.warning("添付ファイルを取り出せませんでした: %s", e)
        attachments = []
    names = "、".join(a[0] for a in attachments)
    note = None
    if level == LEVEL_HEADERS_ONLY:
        what = f"本文と添付ファイル（{names}）" if attachments else "本文"
        note = f"※{what}は{reason or '転送できなかった'}ため転送していません。"
        attachments = []
    elif attachments and level == LEVEL_NO_ATTACHMENTS:
        note = f"※添付ファイル（{names}）は{reason or '転送できなかった'}ため転送していません。"
        attachments = []
    elif attachments and sum(len(a[3]) for a in attachments) > s.max_attach_bytes:
        total = sum(len(a[3]) for a in attachments)
        note = f"※添付ファイル（{names}）は大きい（合計 {total / 1024 / 1024:.1f}MB）ため転送していません。"
        attachments = []
    if note:
        lines += ["", note + "大学のメールで確認してください。"]

    fwd = EmailMessage(policy=SMTP_POLICY)
    fwd["From"] = s.forward_from
    fwd["To"] = ", ".join(s.forward_to)
    fwd["Subject"] = "Fwd: " + (subject or "(件名なし)")
    fwd["Date"] = formatdate(localtime=True)
    fwd["Message-ID"] = make_msgid(domain=s.forward_from.rpartition("@")[2] or None)
    orig_id = _header(msg, "Message-ID")
    if orig_id:
        fwd["References"] = orig_id
    fwd[LOOP_HEADER] = "1"
    fwd.set_content("\n".join(lines) + "\n", cte="base64")
    for filename, maintype, subtype, data in attachments:
        fwd.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return fwd.as_bytes()


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


class MessageTransient(RuntimeError):
    """SMTP にはつながったが、1通の送信の途中で失敗した。回数を数えて、続くなら小さい形にする・飛ばす。"""


def _size_limit(smtp) -> int:
    """送信サーバーが SIZE で知らせてくるメールの大きさの上限（バイト）。無ければ 0。"""
    value = (getattr(smtp, "esmtp_features", {}) or {}).get("size", "")
    try:
        return int(str(value).split()[0])
    except (ValueError, IndexError):
        return 0


def send_mail(s: Settings, data: bytes) -> None:
    """そのまま送る（テスト送信用）。"""
    smtp = _open_smtp(s)
    try:
        opts = ["BODY=8BITMIME"] if smtp.has_extn("8bitmime") else []
        smtp.sendmail(s.forward_from, s.forward_to, data, mail_options=opts)
    finally:
        _safe_quit(smtp)


def deliver(s: Settings, data: bytes):
    """転送メールを1通送る。結果を ("sent" | "too_big" | "rejected", 詳細) で返す。

    - 接続・ログインの失敗（ネットワーク障害など）は、そのまま例外にする（メールのせいではないので数えない）
    - 送信の途中の失敗・一時的な拒否（4xx）は MessageTransient にする（このメールの失敗として数える）
    - このメールが大きすぎる・内容で断られた（5xx）ときは "too_big" / "rejected" を返す
    """
    smtp = _open_smtp(s)
    try:
        limit = _size_limit(smtp)
        if limit and len(data) > limit:
            return "too_big", f"送信サーバーの上限 {limit / 1024 / 1024:.1f}MB を超える {len(data) / 1024 / 1024:.1f}MB"
        opts = ["BODY=8BITMIME"] if smtp.has_extn("8bitmime") else []
        try:
            smtp.sendmail(s.forward_from, s.forward_to, data, mail_options=opts)
        except smtplib.SMTPRecipientsRefused:
            raise  # 転送先アドレスの問題（設定の問題）
        except (smtplib.SMTPSenderRefused, smtplib.SMTPDataError) as e:
            # サイズ超過は MAIL FROM（SIZE=...）の時点で断られることもある
            detail = e.smtp_error.decode(errors="replace") if isinstance(e.smtp_error, bytes) else str(e.smtp_error)
            if e.smtp_code >= 500:
                return "rejected", f"{e.smtp_code} {detail}"
            raise MessageTransient(f"一時的に受け付けられませんでした（{e.smtp_code} {detail}）") from e
        except (smtplib.SMTPException, OSError) as e:
            raise MessageTransient(f"送信の途中で失敗しました（{e}）") from e
        return "sent", ""
    finally:
        _safe_quit(smtp)


# ---------------------------------------------------------------- 本体

# 送信の途中で失敗した回数（メールごと）。接続できないだけのときは数えない
_failures: dict = {}
FAIL_STEP = 3      # この回数失敗するごとに、小さい形（添付なし → ヘッダーのみ）に切り替える
FAIL_GIVE_UP = 9   # この回数失敗したら、そのメールは飛ばす


def _remember(state: dict, key: str) -> None:
    ids = state.setdefault("forwarded_ids", [])
    ids.append(key)
    del ids[:-MAX_REMEMBERED_IDS]


def forward_one(s: Settings, state: dict, folder: str, uid: int, raw: bytes) -> None:
    msg = email.message_from_bytes(raw, policy=DEFAULT_POLICY)
    subject = _header(msg, "Subject")
    where = f"[{folder}] UID {uid}"
    key = message_key(msg, raw)
    if key in state.get("forwarded_ids", []):
        log.info("%s: 転送済みのメールと同じなので転送しません: %s", where, subject)
        return
    if msg.get(LOOP_HEADER):
        log.info("%s: このスクリプトが転送したメールなので転送しません（ループ防止）: %s", where, subject)
        return
    if not matches_filter(s, msg):
        log.info("%s: 条件に合わないので転送しません: %s", where, subject)
        return

    fails = _failures.get(key, 0)
    if fails >= FAIL_GIVE_UP:
        log.error("%s: %d 回続けて送信に失敗したので、このメールは飛ばします: %s", where, fails, subject)
        _failures.pop(key, None)
        return
    # 送信の途中で何度も失敗しているメールは、最初から小さい形で送る
    level = min(fails // FAIL_STEP, LEVEL_HEADERS_ONLY)
    reason = "送信に何度も失敗した" if level else ""

    while level <= LEVEL_HEADERS_ONLY:
        try:
            data = build_forward(s, msg, level=level, reason=reason)
        except Exception:
            log.exception("%s: 転送メールを作れませんでした%s", where, LEVEL_LABEL[level])
            level, reason = level + 1, "うまく変換できなかった"
            continue
        try:
            result, detail = deliver(s, data)
        except MessageTransient as e:
            _failures[key] = fails + 1
            log.warning("%s: %s（%d 回目）。次の確認でもう一度送ります", where, e, fails + 1)
            raise
        if result == "sent":
            _failures.pop(key, None)
            _remember(state, key)
            log.info("%s: 転送しました%s: %s", where, LEVEL_LABEL[level], subject)
            return
        if level == LEVEL_HEADERS_ONLY:
            # ヘッダーだけの小さなメールまで断られた → メールではなく設定の問題の可能性が高いので、飛ばさずに止める
            raise smtplib.SMTPException(
                f"ヘッダーだけの転送メールも送信を断られました（{detail}）。[forward] の from / to の設定を確認してください")
        log.warning("%s: 送れませんでした（%s）。%sを外して送り直します",
                    where, detail, "添付ファイル" if level == LEVEL_FULL else "本文")
        level += 1
        reason = "大きすぎて送れなかった" if result == "too_big" else "転送先のサーバーに受け付けられなかった"

    log.error("%s: 転送メールを作れなかったので、このメールは飛ばします: %s", where, subject)


def init_state(imap, s: Settings, state_path: Path, reason: str) -> dict:
    """今あるメールを全部「転送済み」として記録する。"""
    state = new_state()
    for name in resolve_folders(imap, s):
        try:
            uidvalidity, _ = select_folder(imap, name)
        except FolderError as e:
            log.warning("%s", e)
            continue
        state["folders"][name] = {"uidvalidity": uidvalidity, "last_uid": max_uid(imap)}
    save_state(state_path, state)
    log.info("%s: %d 個のフォルダ（%s）にある今のメールを転送済みとして記録しました。これ以降に届くメールを転送します。",
             reason, len(state["folders"]), "、".join(state["folders"]) or "なし")
    return state


def process_folder(imap, s: Settings, state: dict, state_path: Path, name: str) -> None:
    uidvalidity, _ = select_folder(imap, name)
    fs = state["folders"].get(name)
    if fs is None:
        # 新しく対象になったフォルダ（後から作られた・設定で追加された）。今ある分は転送済みとして記録する
        top = max_uid(imap)
        state["folders"][name] = {"uidvalidity": uidvalidity, "last_uid": top}
        save_state(state_path, state)
        log.info("フォルダ「%s」を転送の対象に加えました（今ある %d 通までは転送しません）", name, top)
        return
    if fs["uidvalidity"] != uidvalidity:
        log.warning("フォルダ「%s」の UIDVALIDITY が変わりました（サーバー側の作り直しなど）。二重転送を防ぐため記録し直します", name)
        fs.update(uidvalidity=uidvalidity, last_uid=max_uid(imap))
        save_state(state_path, state)
        return

    uids = new_uids(imap, fs["last_uid"])
    if not uids:
        return
    dates = fetch_internaldates(imap, fs["last_uid"])
    skipped_old = 0
    for uid in uids:
        received = dates.get(uid)
        if received is not None and received < state["since"]:
            skipped_old += 1  # 転送開始より前に届いたメール（別フォルダから移動されたものなど）
        else:
            raw = fetch_raw(imap, uid)
            if raw is None:
                log.warning("[%s] UID %d を取得できませんでした（削除された可能性）。飛ばします", name, uid)
            else:
                try:
                    forward_one(s, state, name, uid, raw)
                except (AuthError, OSError, smtplib.SMTPException, imaplib.IMAP4.error, RuntimeError):
                    raise  # 通信や送信の失敗 → 位置を進めずに次回やり直す
                except Exception:
                    # このメール固有の想定外のエラー。ここで止まると後続のメールも転送されなくなるので飛ばす
                    log.exception("[%s] UID %d: 想定外のエラーが起きたので、このメールは飛ばします", name, uid)
        fs["last_uid"] = uid
        save_state(state_path, state)
    if skipped_old:
        log.info("[%s] 転送開始より前に届いたメール %d 通（移動されたものなど）は転送しませんでした", name, skipped_old)


def process_new_mail(imap, s: Settings, state_path: Path) -> None:
    state = load_state(state_path, s)
    if state is None:
        init_state(imap, s, state_path, "初回起動")
        return
    names = resolve_folders(imap, s)
    for name in names:
        try:
            process_folder(imap, s, state, state_path, name)
        except FolderError as e:
            log.warning("%s（このフォルダは飛ばします）", e)
    # 消えたフォルダの記録を片付ける
    gone = [n for n in state["folders"] if n not in names]
    if gone:
        for n in gone:
            del state["folders"][n]
        save_state(state_path, state)


def run_forever(s: Settings, state_path: Path) -> None:
    backoff = 30
    while True:
        imap = None
        try:
            imap = connect_imap(s)
            target = "すべてのフォルダ" if s.all_folders else "、".join(s.folders)
            log.info("IMAP に接続しました（%s / %s）。%d 秒ごとに新着を確認します",
                     s.imap_host, target, s.poll_interval)
            backoff = 30
            while True:
                process_new_mail(imap, s, state_path)
                time.sleep(s.poll_interval)
        except AuthError:
            raise
        except (imaplib.IMAP4.error, OSError, smtplib.SMTPException, RuntimeError) as e:
            log.warning("エラー: %s（%d 秒後に再接続します）", e, backoff)
        except Exception:
            # 想定外のエラーでもプロセスは終了させない（終了→再起動を繰り返して止まるのを防ぐ）
            log.exception("想定外のエラー（%d 秒後に再接続します）", backoff)
        finally:
            if imap is not None:
                _safe_logout(imap)
        time.sleep(backoff)
        backoff = min(backoff * 2, 900)


# ---------------------------------------------------------------- コマンド

def cmd_check(s: Settings, state_path: Path) -> None:
    imap = connect_imap(s)
    try:
        print(f"IMAP: OK  {s.imap_host}:{s.imap_port}")
        state = load_state(state_path, s)
        names = resolve_folders(imap, s)
        if s.all_folders:
            excluded = [n for n, _, _ in list_folders(imap) if n not in names]
            print(f"  除外するフォルダ: {'、'.join(excluded) or 'なし'}")
        print("  転送の対象にするフォルダ:")
        for name in names:
            try:
                uidvalidity, count = select_folder(imap, name)
                top = max_uid(imap)
            except FolderError as e:
                print(f"    {name}: 開けません（{e}）")
                continue
            fs = (state or {}).get("folders", {}).get(name)
            if state is None:
                note = "まだ記録なし（初回起動時に今あるメールを転送済みとして記録）"
            elif fs is None:
                note = "記録なし（次の起動時に、今ある分を転送済みとして記録）"
            elif fs["uidvalidity"] != uidvalidity:
                note = "UIDVALIDITY が記録と違う（起動時に記録し直す）"
            else:
                note = f"UID {fs['last_uid']} まで処理済み"
            print(f"    {name}: {count} 通  最新 UID {top}  {note}")
    finally:
        _safe_logout(imap)
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
    g.add_argument("--check", action="store_true", help="IMAP と SMTP にログインできるか、対象フォルダを確認する（送信しない）")
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
            imap = connect_imap(s)
            try:
                if args.init:
                    init_state(imap, s, state_path, "--init")
                else:
                    process_new_mail(imap, s, state_path)
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
