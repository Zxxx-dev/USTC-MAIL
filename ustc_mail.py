"""USTC 邮箱 CLI：预览不联网，Windows凭据可复用，发送必须确认内容指纹。"""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import formatdate, make_msgid, parseaddr
import getpass
import hashlib
import html
import imaplib
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


def address(value):
    if not isinstance(value, str) or re.search(r"[\s<>;,\r\n]", value):
        raise ValueError("邮箱须为单个完整地址，不含显示名或分隔符")
    if parseaddr(value)[1] != value or not re.fullmatch(r"[^@]+@[^@]+\.[^@]+", value):
        raise ValueError("邮箱格式不正确")
    return value


def account_address(value):
    value = address(value)
    if value.rsplit("@", 1)[1].lower() not in {"ustc.edu.cn", "mail.ustc.edu.cn"}:
        raise ValueError("发件账号须为科大邮箱")
    return value


def compose(spec_path, account):
    spec_path = Path(spec_path).resolve()
    spec = json.loads(spec_path.read_text(encoding="utf-8-sig"))
    if not isinstance(spec, dict) or set(spec) - {"to", "cc", "subject", "body_file", "html_file", "attachments"}:
        raise ValueError("邮件配置包含未知字段或不是 JSON 对象")
    recipient = address(spec["to"])
    cc = spec.get("cc", [])
    if not isinstance(cc, list):
        raise ValueError("cc 须为地址数组")
    cc = [address(item) for item in cc]
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
    print(json.dumps({**summary(message), "sha256": fingerprint,
                      "attachments": [{"name": p.get_filename(), "bytes": len(p.get_payload(decode=True))}
                                      for p in message.iter_attachments()]}, ensure_ascii=False, indent=2))
    print(message.get_body(preferencelist=("plain",)).get_content())
    if message.get_body(preferencelist=("html",)) is not None:
        print("本邮件含 HTML 正文；请使用 preview --output 在浏览器核对排版。")


def save_preview(path, message):
    rich = message.get_body(preferencelist=("html", "plain"))
    body = rich.get_content()
    if rich.get_content_type() != "text/html":
        body = "<pre style='white-space:pre-wrap'>" + html.escape(body) + "</pre>"
    headers = html.escape(json.dumps(summary(message), ensure_ascii=False, indent=2))
    names = html.escape(", ".join(p.get_filename() or "" for p in message.iter_attachments()))
    # HTML 来自用户自己的邮件正文；仅在用户主动打开预览时渲染。
    document = '<!doctype html><meta charset="utf-8"><title>邮件预览</title>'
    document += "<pre>" + headers + "</pre><p>附件：" + names + "</p><hr>" + body
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


def password(account):
    value = os.environ.get("USTC_MAIL_PASSWORD")
    if not value:
        value = credential(account)
    return value or input_password()


def auth(account, action):
    if action == "status":
        print(json.dumps({"account": account, "credential_saved": bool(credential(account)),
                          "environment_override": bool(os.environ.get("USTC_MAIL_PASSWORD"))}, ensure_ascii=False))
    elif action == "forget":
        credential(account, "delete")
        print("已移除本账号的本机凭据；未撤销邮箱服务器上的专用密码。")
    else:
        secret = os.environ.get("USTC_MAIL_PASSWORD") or input_password()
        with smtplib.SMTP_SSL(HOST, 465, context=ssl.create_default_context(), timeout=30) as client:
            client.login(account, secret)
        credential(account, "write", secret)
        print("SMTP认证通过；已保存至当前Windows用户的凭据管理器，后续自动复用。未发送邮件。")


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
        raise ValueError("IMAP 操作失败；未自动重试")
    return data


def select(client, folder):
    if re.search(r'[\r\n"\\]', folder):
        raise ValueError("文件夹名称含不支持的字符")
    checked(client.select('"' + folder + '"', readonly=True))


def fetch(client, uid, headers=False):
    query = "(BODY.PEEK[HEADER])" if headers else "(BODY.PEEK[])"
    data = checked(client.uid("fetch", str(uid), query))
    raw = next((item[1] for item in data if isinstance(item, tuple)), None)
    if raw is None:
        raise ValueError("该 UID 不存在或邮件内容不可读")
    return BytesParser(policy=policy.default).parsebytes(raw)


def draft(client, message, folder):
    raw = message.as_bytes()
    checked(client.append('"' + folder + '"', r"\Draft", imaplib.Time2Internaldate(datetime.now(timezone.utc)), raw))
    select(client, folder)
    data = checked(client.uid("search", None, "HEADER", "Message-ID", '"' + message["Message-ID"] + '"'))
    uids = data[0].split()
    if len(uids) != 1:
        raise ValueError("草稿已提交，但无法唯一定位；请先查草稿箱，不要直接重试")
    saved = fetch(client, int(uids[0]))
    if saved.as_bytes(policy=policy.SMTP) != raw:
        raise ValueError("草稿已提交，但服务器回读内容不一致；请人工核验")
    print(json.dumps({"draft_uid": uids[0].decode(), "verified": True, **summary(saved)}, ensure_ascii=False))


def send(message, fingerprint, account, confirmed):
    if confirmed != fingerprint:
        raise ValueError("先 preview，再将完整 sha256 传给 --confirm-sha256；内容变化须重新预览")
    receipt = RECEIPTS / (fingerprint + ".json")
    if receipt.exists():
        raise ValueError("已有同内容发送记录，禁止直接重试；请先人工核验：" + str(receipt))
    secret = password(account)
    with smtplib.SMTP_SSL(HOST, 465, context=ssl.create_default_context(), timeout=30) as client:
        client.login(account, secret)
        RECEIPTS.mkdir(parents=True, exist_ok=True)
        record = {"sha256": fingerprint, **summary(message),
                  "time": datetime.now(timezone.utc).isoformat(), "state": "attempting"}
        # 提交前占位；断线可能发生在服务器接受之后，保留占位以防重复投递。
        with receipt.open("x", encoding="utf-8") as output:
            json.dump(record, output, ensure_ascii=False, indent=2)
        try:
            refused = client.send_message(message)
        except Exception:
            print("发送结果可能不确定。请核对邮箱及记录后处理，勿直接重试：" + str(receipt), file=sys.stderr)
            raise
        record["state"] = "partially_refused" if refused else "smtp_accepted"
        record["refused_recipients"] = list(refused)
        receipt.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(record, ensure_ascii=False, indent=2))
        print("SMTP 接受不等于对方已收/已读；请核对服务器记录后更新投递状态。")
        if refused:
            raise ValueError("部分收件人被拒绝；其余可能已接受，不可整封重发")


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("须为正整数")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", default=os.environ.get("USTC_MAIL_ACCOUNT"),
                        help="完整科大邮箱地址；也可设置 USTC_MAIL_ACCOUNT")
    commands = parser.add_subparsers(dest="command", required=True)
    authentication = commands.add_parser("auth", help="保存、检查或移除Windows凭据")
    authentication.add_argument("action", choices=("login", "status", "forget"))
    commands.add_parser("folders", help="列出 IMAP 文件夹及特殊用途标志")
    listing = commands.add_parser("list", help="只读列出最近邮件，不改变已读状态")
    listing.add_argument("--folder", default="INBOX")
    listing.add_argument("--limit", type=positive, default=10)
    reading = commands.add_parser("read", help="只读查看 UID 对应的正文及附件名")
    reading.add_argument("uid", type=positive)
    reading.add_argument("--folder", default="INBOX")
    for name in ("preview", "draft", "send"):
        command = commands.add_parser(name)
        command.add_argument("spec", help="邮件 JSON 配置；相对文件路径以配置文件所在目录为准")
        if name == "preview":
            command.add_argument("--output", help="另存 HTML 预览；已有文件不会覆盖")
        elif name == "draft":
            command.add_argument("--folder", required=True, help="先用 folders 查 \u005c\u005cDrafts 文件夹名")
        else:
            command.add_argument("--confirm-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        if not args.account:
            raise ValueError("请通过 --account 或 USTC_MAIL_ACCOUNT 提供科大邮箱账号")
        account = account_address(args.account)
        if args.command == "auth":
            auth(account, args.action)
        elif args.command in {"preview", "draft", "send"}:
            message, fingerprint = compose(args.spec, account)
            if args.command == "preview":
                preview(message, fingerprint)
                if args.output:
                    save_preview(args.output, message)
            elif args.command == "draft":
                if re.search(r'[\r\n"\\]', args.folder):
                    raise ValueError("文件夹名称含不支持的字符")
                with mailbox(account) as client:
                    draft(client, message, args.folder)
            else:
                send(message, fingerprint, account, args.confirm_sha256)
        else:
            with mailbox(account) as client:
                if args.command == "folders":
                    for item in checked(client.list()):
                        if item:
                            print(item.decode("ascii", errors="backslashreplace"))
                else:
                    select(client, args.folder)
                    if args.command == "list":
                        uids = checked(client.uid("search", None, "ALL"))[0].split()
                        for uid in reversed(uids[-args.limit:]):
                            print(json.dumps({"uid": int(uid), **summary(fetch(client, int(uid), headers=True))}, ensure_ascii=False))
                    else:
                        message = fetch(client, args.uid)
                        print(json.dumps(summary(message), ensure_ascii=False, indent=2))
                        body = message.get_body(preferencelist=("plain", "html"))
                        print(body.get_content() if body else "（无文本正文）")
                        print("附件：", [p.get_filename() for p in message.iter_attachments()])
        return 0
    except ValueError as error:
        print("操作失败：" + str(error), file=sys.stderr)
        return 1
    except smtplib.SMTPAuthenticationError as error:
        print(f"SMTP 登录失败（状态码 {error.smtp_code}）：请核对客户端专用密码、账号及权限，再运行 ustc-mail auth login 更新凭据；未提交邮件。", file=sys.stderr)
        return 1
    except (OSError, KeyError, TypeError, imaplib.IMAP4.error, smtplib.SMTPException):
        # 服务器报错可能含账号或其他敏感信息，不回显原始异常。
        print("操作失败：请检查配置、凭据、文件或服务器连接；已有输出不覆盖，发送不自动重试。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
