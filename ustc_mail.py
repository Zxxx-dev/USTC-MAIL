"""USTC 邮箱 CLI：离线预览、只读检索及带防重复保护的邮件操作。"""

import argparse
from contextlib import contextmanager, redirect_stdout
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import formatdate, make_msgid, parseaddr
import getpass
import hashlib
import html
import imaplib
import io
import json
import mimetypes
import os
from pathlib import Path
import re
import smtplib
import ssl
import sys

HOST = "mail.ustc.edu.cn"
RECEIPTS = Path.home() / ".ustc-mail" / "receipts"
VERSION = "0.1.0"
DEFAULT_MAX_BODY_CHARS = 12000
DEFAULT_MAX_MESSAGE_BYTES = 10485760


class WorkflowError(ValueError):
    def __init__(self, message, code="operation_failed", state="failed", retryable=False, data=None):
        super().__init__(message)
        self.code, self.state, self.retryable, self.data = code, state, retryable, data or {}


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise WorkflowError(message, "invalid_arguments", "rejected")


def address(value):
    if not isinstance(value, str) or re.search(r"[\s<>;,\r\n]", value):
        raise ValueError("邮箱须为单个完整地址，不含显示名或分隔符")
    if parseaddr(value)[1] != value or not re.fullmatch(r"[^@]+@[^@]+\.[^@]+", value):
        raise ValueError("邮箱格式不正确")
    return value


def account_address(value):
    value = address(value)
    if not value.isascii():
        raise ValueError("邮箱账号不能包含中文、全角或其他非 ASCII 字符；请填写实际完整科大邮箱地址，勿使用示例占位文字")
    if value.rsplit("@", 1)[1].lower() not in {"ustc.edu.cn", "mail.ustc.edu.cn", "ah.edu.cn"}:
        raise ValueError("发件账号后缀须为 ustc.edu.cn、mail.ustc.edu.cn 或 ah.edu.cn")
    return value


def validate_recipients(to, cc, allow_recipients=None):
    addresses = [address(to), *(address(item) for item in cc)]
    if len(addresses) > 5:
        raise WorkflowError("收件人与抄送总人数上限为 5", "recipient_limit", "rejected")
    if allow_recipients:
        allowed = {address(item).casefold() for item in allow_recipients}
        denied = [item for item in addresses if item.casefold() not in allowed]
        if denied:
            raise WorkflowError("收件人不在 --allow-recipient 白名单中", "recipient_not_allowed", "rejected")
    return addresses


def compose(spec_path, account, allow_recipients=None):
    spec_path = Path(spec_path).resolve()
    spec = json.loads(spec_path.read_text(encoding="utf-8-sig"))
    if not isinstance(spec, dict) or set(spec) - {"to", "cc", "subject", "body_file", "html_file", "attachments"}:
        raise ValueError("邮件配置包含未知字段或不是 JSON 对象")
    recipient = address(spec["to"])
    cc = spec.get("cc", [])
    if not isinstance(cc, list):
        raise ValueError("cc 须为地址数组")
    cc = [address(item) for item in cc]
    validate_recipients(recipient, cc, allow_recipients)
    subject = spec["subject"]
    if not isinstance(subject, str) or not subject.strip() or re.search(r"[\r\n]", subject):
        raise ValueError("主题不能为空或含换行")
    body = (spec_path.parent / spec["body_file"]).read_text(encoding="utf-8-sig")
    if not body.strip():
        raise ValueError("正文不能为空")
    rich = None
    if spec.get("html_file"):
        rich = (spec_path.parent / spec["html_file"]).read_text(encoding="utf-8-sig")
        if not rich.strip():
            raise ValueError("HTML 正文不能为空")
    paths = spec.get("attachments", [])
    if not isinstance(paths, list):
        raise ValueError("attachments 须为文件路径数组")
    attachments = []
    for item in paths:
        path = spec_path.parent / item
        data = path.read_bytes()
        if not data:
            raise ValueError("附件不能为空")
        attachments.append((path.name, data))
    identity = {"from": account, "to": recipient, "cc": cc, "subject": subject,
                "body": body, "html": rich,
                "attachments": [(name, hashlib.sha256(data).hexdigest()) for name, data in attachments]}
    fingerprint = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    message = EmailMessage(policy=policy.SMTP)
    message["From"] = account
    message["To"] = recipient
    if cc:
        message["Cc"] = ", ".join(cc)
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=account.split("@")[1])
    message.set_content(body)
    if rich:
        message.add_alternative(rich, subtype="html")
    for name, data in attachments:
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        major, minor = mime.split("/", 1)
        message.add_attachment(data, maintype=major, subtype=minor, filename=name)
    return message, fingerprint


def summary(message):
    return {key: str(message.get(key, "")) for key in ("From", "To", "Cc", "Subject", "Date", "Message-ID")}


def preview(message, fingerprint):
    plain = message.get_body(preferencelist=("plain",))
    rich = message.get_body(preferencelist=("html",))
    attachments = [{"name": p.get_filename(), "bytes": len(p.get_payload(decode=True))}
                   for p in message.iter_attachments()]
    print(json.dumps({**summary(message), "sha256": fingerprint,
                      "attachments": attachments}, ensure_ascii=False, indent=2))
    print(plain.get_content() if plain else "")
    if rich is not None:
        print("本邮件含 HTML 正文；请使用 preview --output 在浏览器核对排版。")
    return {"headers": summary(message), "sha256": fingerprint,
            "body": plain.get_content() if plain else "", "html": rich.get_content() if rich else None,
            "attachments": attachments}


def save_preview(path, message):
    rich = message.get_body(preferencelist=("html", "plain"))
    body = rich.get_content()
    if rich.get_content_type() != "text/html":
        body = "<pre style='white-space:pre-wrap'>" + html.escape(body) + "</pre>"
    headers = html.escape(json.dumps(summary(message), ensure_ascii=False, indent=2))
    names = html.escape(", ".join(p.get_filename() or "" for p in message.iter_attachments()))
    # srcdoc is escaped as an attribute and sandboxed without any allow-* permissions.
    body = re.sub(r"<meta\b(?=[^>]*\bhttp-equiv\s*=\s*(?:['\"]\s*refresh\s*['\"]|refresh))[^>]*>",
                  "", body, flags=re.IGNORECASE)
    inner = "<!doctype html><meta charset=utf-8><meta http-equiv=Content-Security-Policy content=\"default-src 'none'; img-src data:; style-src 'unsafe-inline' data:; font-src data:; media-src data:; connect-src 'none'; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'; navigate-to 'none'\">" + body
    document = '<!doctype html><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; frame-src \'self\'; object-src \'none\'; base-uri \'none\'; form-action \'none\'"><title>邮件预览</title>'
    document += "<pre>" + headers + "</pre><p>附件：" + names + "</p><hr><iframe sandbox style=\"display:block;width:100%;height:70vh;min-height:480px;border:0;overflow:auto\" srcdoc=\"" + html.escape(inner, quote=True) + "\"></iframe>"
    with Path(path).open("x", encoding="utf-8") as output:
        output.write(document)


def credential(account, action="read", secret=None):
    try:
        import win32cred
        import pywintypes
    except ImportError:
        if action == "read":
            return None
        raise ValueError("保存凭据需要 Windows 和 pywin32；请先运行 python -m pip install .") from None
    target = "USTCMail/" + account.lower()
    try:
        if action == "read":
            return win32cred.CredRead(target, win32cred.CRED_TYPE_GENERIC)["CredentialBlob"].decode("utf-16-le")
        if action == "write":
            win32cred.CredWrite({"Type": win32cred.CRED_TYPE_GENERIC, "TargetName": target,
                                "UserName": account, "CredentialBlob": secret,
                                "Persist": win32cred.CRED_PERSIST_LOCAL_MACHINE})
        elif action == "delete":
            win32cred.CredDelete(target, win32cred.CRED_TYPE_GENERIC)
        else:
            raise ValueError("未知凭据操作")
    except pywintypes.error as error:
        if getattr(error, "winerror", None) == 1168 and action in {"read", "delete"}:
            return None
        raise ValueError("Windows凭据管理器操作失败；未输出凭据") from None


def input_password():
    if not sys.stdin.isatty():
        raise ValueError("尚未保存凭据，请在本地交互终端运行 ustc-mail auth login")
    value = getpass.getpass("科大邮箱客户端专用密码：")
    if not value:
        raise ValueError("客户端专用密码不能为空")
    return value


def validate_password(value):
    if not value.isascii():
        raise ValueError("客户端专用密码不能包含中文、全角或其他非 ASCII 字符；请重新复制网页生成的密码，勿输入密码名称。未输出密码")
    return value


def password(account):
    value = os.environ.get("USTC_MAIL_PASSWORD")
    if not value:
        value = credential(account)
    return validate_password(value or input_password())


def auth(account, action):
    if action == "status":
        result = {"account": account, "credential_saved": bool(credential(account)),
                  "environment_override": bool(os.environ.get("USTC_MAIL_PASSWORD"))}
        print(json.dumps(result, ensure_ascii=False))
        return result
    elif action == "forget":
        credential(account, "delete")
        print("已移除本账号的本机凭据；未撤销邮箱服务器上的专用密码。")
        return {"account": account, "credential_removed": True}
    else:
        secret = validate_password(os.environ.get("USTC_MAIL_PASSWORD") or input_password())
        with smtplib.SMTP_SSL(HOST, 465, context=ssl.create_default_context(), timeout=30) as client:
            client.login(account, secret)
        credential(account, "write", secret)
        print("SMTP认证通过；已保存至当前Windows用户的凭据管理器，后续自动复用。未发送邮件。")
        return {"account": account, "authenticated": True, "credential_saved": True}


@contextmanager
def mailbox(account):
    secret = password(account)
    client = imaplib.IMAP4_SSL(HOST, 993, ssl_context=ssl.create_default_context(), timeout=30)
    try:
        client.login(account, secret)
        yield client
    finally:
        try:
            client.logout()
        except (OSError, imaplib.IMAP4.error):
            pass


def checked(result):
    status, data = result
    if status != "OK":
        raise WorkflowError("IMAP 操作失败；未自动重试", "network_error", "network_error")
    return data


def imap_quote(value):
    if not isinstance(value, str) or re.search(r"[\x00-\x1f\x7f]", value):
        raise ValueError("IMAP 搜索值含控制字符")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def select(client, folder):
    checked(client.select(imap_quote(folder), readonly=True))


def fetch(client, uid, headers=False, metadata_only=False, max_message_bytes=DEFAULT_MAX_MESSAGE_BYTES):
    if metadata_only:
        headers = True
    if headers:
        query = "(BODY.PEEK[HEADER])"
    else:
        # RFC822.SIZE lets the caller reject oversize messages before fetching the body.
        query = "(RFC822.SIZE)"
        size_data = checked(client.uid("fetch", str(uid), query))
        size = 0
        for item in size_data:
            header = item[0] if isinstance(item, tuple) and item else item
            if isinstance(header, bytes):
                match = re.search(rb"RFC822\.SIZE\s+(\d+)", header, re.I)
                if match:
                    size = int(match.group(1))
                    break
        if size == 0:
            raise WorkflowError("无法取得 RFC822.SIZE，未下载邮件正文", "message_size_unavailable", "rejected",
                                data={"uid": int(uid), "truncated": True})
        if size and size > max_message_bytes:
            raise WorkflowError("邮件超过 --max-message-bytes 限制，未下载正文", "message_too_large", "rejected",
                                data={"uid": int(uid), "size": size, "truncated": True})
        query = "(BODY.PEEK[])"
    data = checked(client.uid("fetch", str(uid), query))
    raw = next((item[1] for item in data if isinstance(item, tuple) and isinstance(item[1], bytes)), None)
    if raw is None:
        raise ValueError("该 UID 不存在或邮件内容不可读")
    if not headers and len(raw) > max_message_bytes:
        raise WorkflowError("邮件超过 --max-message-bytes 限制", "message_too_large", "rejected",
                            data={"uid": int(uid), "size": len(raw), "truncated": True})
    return BytesParser(policy=policy.default).parsebytes(raw)


def _addr_header(message, key):
    header = message.get(key)
    if header is None:
        return []
    return sorted(a.addr_spec.casefold() for a in getattr(header, "addresses", []) if a.addr_spec)


def _text_parts(message):
    result = {}
    for part in message.walk():
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        kind = part.get_content_type().lower()
        if kind in ("text/plain", "text/html"):
            try:
                content = part.get_content()
            except (LookupError, UnicodeError):
                payload = part.get_payload(decode=True) or b""
                content = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
            result.setdefault(kind, []).append(content.replace("\r\n", "\n"))
    return result


def mime_semantics(message):
    attachments = []
    for part in message.iter_attachments():
        payload = part.get_payload(decode=True) or b""
        attachments.append((part.get_filename() or "", hashlib.sha256(payload).hexdigest()))
    return {"from": _addr_header(message, "From"), "to": _addr_header(message, "To"),
            "cc": _addr_header(message, "Cc"), "subject": str(message.get("Subject", "")),
            "body": _text_parts(message), "attachments": attachments}


def draft(client, message, folder):
    raw = message.as_bytes()
    try:
        checked(client.append(imap_quote(folder), r"\Draft", imaplib.Time2Internaldate(datetime.now(timezone.utc)), raw))
    except Exception as error:
        raise WorkflowError("草稿提交结果不确定；请先查草稿箱，勿直接重试", "draft_result_unknown",
                            "draft_result_unknown", False, {"message_id": str(message.get("Message-ID", ""))}) from error
    try:
        select(client, folder)
        data = checked(client.uid("search", None, "HEADER", "Message-ID", imap_quote(str(message["Message-ID"]))))
        uids = data[0].split()
        if len(uids) != 1:
            raise ValueError("草稿无法唯一定位")
        uid = uids[0].decode("ascii")
        saved = fetch(client, int(uid))
        if mime_semantics(saved) != mime_semantics(message):
            raise ValueError("回读内容不一致")
    except Exception as error:
        detail = str(error) if isinstance(error, ValueError) else "服务器回读或搜索失败"
        raise WorkflowError("草稿已提交，但回读核验失败（" + detail + "）；请人工检查草稿箱，勿直接重试",
                            "draft_submitted_unverified", "draft_submitted_unverified", False,
                            {"uid": locals().get("uid"), "message_id": str(message.get("Message-ID", ""))}) from error
    print(json.dumps({"folder": folder, "uid": uid, "verified": True, **summary(saved)}, ensure_ascii=False))
    return {"folder": folder, "uid": int(uid), "verified": True,
            "external_data": True, **summary(saved)}


def send(message, fingerprint, account, confirmed):
    if confirmed != fingerprint:
        raise ValueError("先 preview，再将完整 sha256 传给 --confirm-sha256；内容变化须重新预览")
    receipt = RECEIPTS / (fingerprint + ".json")
    if receipt.exists():
        raise ValueError("已有同内容发送记录，禁止直接重试；请先人工核验：" + str(receipt))
    secret = password(account)
    submission_started = False
    accepted = False
    record = None
    try:
        with smtplib.SMTP_SSL(HOST, 465, context=ssl.create_default_context(), timeout=30) as client:
            client.login(account, secret)
            RECEIPTS.mkdir(parents=True, exist_ok=True)
            record = {"sha256": fingerprint, **summary(message),
                      "time": datetime.now(timezone.utc).isoformat(), "state": "attempting"}
            with receipt.open("x", encoding="utf-8") as output:
                json.dump(record, output, ensure_ascii=False, indent=2)
            submission_started = True
            refused = client.send_message(message)
            accepted = True
            record["state"] = "partially_refused" if refused else "smtp_accepted"
            record["refused_recipients"] = list(refused)
            try:
                receipt.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            except OSError as error:
                state = record["state"]
                code = "partial_refusal_unrecorded" if refused else "smtp_accepted_unrecorded"
                raise WorkflowError("SMTP 已处理邮件，但发送记录更新失败；请核对邮箱，勿直接重试",
                                    code, state, False,
                                    {"smtp_accepted": True, "receipt": str(receipt),
                                     "refused_recipients": list(refused),
                                     "message_id": str(message.get("Message-ID", ""))}) from error
            print(json.dumps(record, ensure_ascii=False, indent=2))
            print("SMTP 接受不等于对方已收/已读；请核对服务器记录后更新投递状态。")
            if refused:
                raise WorkflowError("部分收件人被拒绝；其余可能已接受，不可整封重发", "partial_refusal",
                                    "partially_refused", False,
                                    {"smtp_accepted": True, "receipt": str(receipt),
                                     "refused_recipients": list(refused),
                                     "message_id": str(message.get("Message-ID", ""))})
            return record
    except WorkflowError:
        raise
    except Exception as error:
        if accepted:
            state = record.get("state", "smtp_accepted") if record else "smtp_accepted"
            code = "partial_refusal" if state == "partially_refused" else "smtp_accepted"
            raise WorkflowError("SMTP 已处理邮件，但连接关闭或后续处理失败；请核对邮箱及记录，勿直接重试",
                                code, state, False,
                                {"smtp_accepted": True, "receipt": str(receipt),
                                 "refused_recipients": record.get("refused_recipients", []) if record else [],
                                 "message_id": str(message.get("Message-ID", ""))}) from error
        if submission_started:
            raise WorkflowError("发送结果可能不确定；请核对邮箱及记录，勿直接重试：" + str(receipt),
                                "send_result_unknown", "send_result_unknown", False,
                                {"receipt": str(receipt), "message_id": str(message.get("Message-ID", ""))}) from error
        raise


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("须为正整数")
    return number


def _parser():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", dest="json_output", help="以单一 JSON 对象输出结果")
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument("--account", default=os.environ.get("USTC_MAIL_ACCOUNT"),
                        help="完整科大邮箱地址；也可设置 USTC_MAIL_ACCOUNT")
    parser.add_argument("--allow-recipient", action="append", default=[], help="收件人白名单；可重复指定")
    parser.add_argument("--read-only", action="store_true", help="拒绝草稿、发送及凭据修改")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=ArgumentParser)
    authentication = commands.add_parser("auth", help="保存、检查或移除Windows凭据")
    authentication.add_argument("action", choices=("login", "status", "forget"))
    commands.add_parser("folders", help="列出 IMAP 文件夹及特殊用途标志")
    listing = commands.add_parser("list", help="只读列出最近邮件，不改变已读状态")
    listing.add_argument("--folder", default="INBOX")
    listing.add_argument("--limit", type=positive, default=10)
    search = commands.add_parser("search", help="只读按有限条件搜索邮件元数据")
    search.add_argument("--from", dest="sender")
    search.add_argument("--subject")
    search.add_argument("--since", help="YYYY-MM-DD")
    search.add_argument("--unread", action="store_true")
    search.add_argument("--folder", default="INBOX")
    search.add_argument("--limit", type=positive, default=10)
    reading = commands.add_parser("read", help="只读查看 UID 对应的正文及附件名")
    reading.add_argument("uid", type=positive)
    reading.add_argument("--folder", default="INBOX")
    reading.add_argument("--metadata-only", action="store_true")
    reading.add_argument("--max-body-chars", type=positive, default=DEFAULT_MAX_BODY_CHARS)
    reading.add_argument("--max-message-bytes", type=positive, default=DEFAULT_MAX_MESSAGE_BYTES)
    for name in ("preview", "draft", "send"):
        command = commands.add_parser(name)
        command.add_argument("spec", help="邮件 JSON 配置；相对文件路径以配置文件所在目录为准")
        if name == "preview":
            command.add_argument("--output", help="另存 HTML 预览；已有文件不会覆盖")
        elif name == "draft":
            command.add_argument("--folder", required=True, help="先用 folders 查 \\\\Drafts 文件夹名")
        else:
            command.add_argument("--confirm-sha256", required=True)
    return parser


def _search(client, args):
    criteria = []
    unicode_terms = []
    if args.sender:
        imap_quote(args.sender)
        if any(ord(char) > 127 for char in args.sender):
            unicode_terms.append(("FROM", args.sender))
        else:
            criteria += ["FROM", imap_quote(args.sender)]
    if args.subject:
        imap_quote(args.subject)
        if any(ord(char) > 127 for char in args.subject):
            unicode_terms.append(("SUBJECT", args.subject))
        else:
            criteria += ["SUBJECT", imap_quote(args.subject)]
    if args.since:
        try:
            day = datetime.strptime(args.since, "%Y-%m-%d")
        except ValueError:
            raise WorkflowError("--since 须为 YYYY-MM-DD", "invalid_date", "rejected") from None
        criteria += ["SINCE", day.strftime("%d-%b-%Y")]
    if args.unread:
        criteria.append("UNSEEN")
    if not criteria and not unicode_terms:
        criteria = ["ALL"]
    if not unicode_terms:
        uids = checked(client.uid("search", None, *criteria))[0].split()
    else:
        matches = []
        for index, (field, value) in enumerate(unicode_terms):
            terms = ["CHARSET", "UTF-8"]
            if index == 0:
                terms.extend(criteria)
            terms.append(field)
            client.literal = value.encode("utf-8")
            try:
                found = checked(client.uid("search", *terms))[0].split()
            finally:
                client.literal = None
            matches.append(found)
        uids = [uid for uid in matches[0] if all(uid in others for others in matches[1:])]
    return [metadata_row(args.folder, uid, fetch(client, int(uid), headers=True)) for uid in reversed(uids[-args.limit:])]


def metadata_row(folder, uid, message):
    return {"folder": folder, "uid": int(uid), "external_data": True, **summary(message)}


def _core(args):
    if not args.account:
        raise WorkflowError("请通过 --account 或 USTC_MAIL_ACCOUNT 提供科大邮箱账号", "invalid_config", "rejected")
    try:
        account = account_address(args.account)
    except ValueError as error:
        raise WorkflowError(str(error), "invalid_config", "rejected") from error
    readonly = os.environ.get("USTC_MAIL_READ_ONLY") == "1" or getattr(args, "read_only", False)
    forbidden = (args.command in {"draft", "send"} or (args.command == "auth" and args.action in {"login", "forget"}))
    if readonly and forbidden:
        raise WorkflowError("只读模式下禁止草稿、发送、认证登录和移除凭据", "read_only", "rejected")
    if args.command == "auth":
        return auth(account, args.action)
    elif args.command in {"preview", "draft", "send"}:
        allowlist = args.allow_recipient if args.command in {"draft", "send"} else None
        try:
            message, fingerprint = compose(args.spec, account, allowlist)
        except WorkflowError:
            raise
        except (ValueError, OSError, KeyError, TypeError, json.JSONDecodeError) as error:
            raise WorkflowError(str(error), "invalid_config", "rejected") from error
        if args.command == "preview":
            result = preview(message, fingerprint)
            if args.output:
                save_preview(args.output, message)
            return result
        elif args.command == "draft":
            with mailbox(account) as client:
                return draft(client, message, args.folder)
        else:
            return send(message, fingerprint, account, args.confirm_sha256)
    else:
        with mailbox(account) as client:
            if args.command == "folders":
                folders = []
                for item in checked(client.list()):
                    if item:
                        folder = item.decode("ascii", errors="backslashreplace")
                        folders.append(folder)
                        print(folder)
                return folders
            else:
                select(client, args.folder)
                if args.command == "search":
                    rows = _search(client, args)
                    for row in rows:
                        print(json.dumps(row, ensure_ascii=False))
                    return rows
                elif args.command == "list":
                    uids = checked(client.uid("search", None, "ALL"))[0].split()
                    rows = [metadata_row(args.folder, uid, fetch(client, int(uid), headers=True))
                            for uid in reversed(uids[-args.limit:])]
                    for row in rows:
                        print(json.dumps(row, ensure_ascii=False))
                    return rows
                else:
                    message = fetch(client, args.uid, headers=args.metadata_only,
                                    metadata_only=args.metadata_only, max_message_bytes=args.max_message_bytes)
                    result = metadata_row(args.folder, args.uid, message)
                    if args.metadata_only:
                        result.update({"metadata_only": True, "truncated": True})
                        print(json.dumps(result, ensure_ascii=False, indent=2))
                        return result
                    else:
                        body = message.get_body(preferencelist=("plain", "html"))
                        content = body.get_content() if body else ""
                        truncated = len(content) > args.max_body_chars
                        content = content[:args.max_body_chars]
                        result.update({"body": content or "（无文本正文）", "attachments": [p.get_filename() for p in message.iter_attachments()], "truncated": truncated})
                        print(json.dumps(result, ensure_ascii=False, indent=2))
                        return result


def _extract_global(argv):
    argv = list(sys.argv[1:] if argv is None else argv)
    json_output = "--json" in argv
    front, rest = [], []
    i = 0
    while i < len(argv):
        value = argv[i]
        if value == "--json":
            i += 1
        elif value in {"--account", "--allow-recipient"}:
            front.extend(argv[i:i + 2])
            i += 2
        elif value == "--read-only":
            front.append(value)
            i += 1
        else:
            rest.append(value)
            i += 1
    return front + rest, json_output


def main(argv=None):
    argv, json_output = _extract_global(argv)
    stdout = io.StringIO()
    command = None
    if "--version" in argv:
        if json_output:
            print(json.dumps({"ok": True, "command": "version", "state": "completed", "retryable": False,
                              "data": {"version": VERSION}}, ensure_ascii=False))
        else:
            print(VERSION)
        return 0
    try:
        parser = _parser()
        args = parser.parse_args(argv)
        json_output = json_output or args.json_output
        command = args.command
        with redirect_stdout(stdout):
            data = _core(args)
        content = stdout.getvalue()
        if json_output:
            state = "draft_verified" if command == "draft" else (
                data.get("state", "smtp_accepted") if command == "send" and isinstance(data, dict) else "completed")
            result = {"ok": True, "command": command, "state": state, "retryable": False, "data": data}
            print(json.dumps(result, ensure_ascii=False))
        else:
            sys.stdout.write(content)
        return 0
    except Exception as error:
        if isinstance(error, WorkflowError):
            code, state, retryable, extra = error.code, error.state, error.retryable, error.data
            message = str(error)
        elif isinstance(error, smtplib.SMTPAuthenticationError):
            code, state, retryable, extra = "authentication_failed", "failed", False, {}
            message = f"SMTP 登录失败（状态码 {error.smtp_code}）：请核对客户端专用密码、账号及权限。"
        elif isinstance(error, imaplib.IMAP4.error) and any(
                marker in str(error).lower() for marker in ("auth", "login", "credential")):
            code, state, retryable, extra = "authentication_failed", "failed", False, {}
            message = "IMAP 登录失败：请核对客户端专用密码、账号及权限。"
        elif isinstance(error, (json.JSONDecodeError, KeyError, TypeError, ValueError, FileNotFoundError)):
            code, state, retryable, extra = "invalid_config", "rejected", False, {}
            message = str(error)
        elif isinstance(error, (OSError, imaplib.IMAP4.error, smtplib.SMTPException)):
            code, state, retryable, extra = "network_error", "failed", False, {}
            message = "请检查服务器连接；草稿和发送结果不确定时请先核验，勿直接重试。"
        else:
            code, state, retryable, extra = "operation_failed", "failed", False, {}
            message = str(error)
        if json_output:
            print(json.dumps({"ok": False, "command": command, "state": state, "retryable": False,
                              "data": extra, "error": {"code": code, "message": message}}, ensure_ascii=False))
        else:
            print("操作失败：" + message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
