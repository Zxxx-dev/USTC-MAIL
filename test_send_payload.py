"""用假 SMTP 核对预览后实际交给发送端的 MIME 正文和附件没有变化。"""

from contextlib import redirect_stdout
from email import policy
from email.parser import BytesParser
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import ustc_mail as mail


class FakeSMTP:
    def __init__(self):
        self.message_bytes = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def login(self, account, password):
        self.account = account
        self.password = password

    def send_message(self, message):
        self.message_bytes = message.as_bytes()
        return {}


class SendPayloadTests(unittest.TestCase):
    def test_preview_does_not_change_payload_delivered_to_smtp(self):
        with tempfile.TemporaryDirectory(prefix="ustc-mail-payload-") as temp:
            root = Path(temp)
            plain = "第一行\r\n第二行，含中文。\r\n"
            rich = '<p style="color:#123456">HTML 原文。</p>\r\n'
            attachment = b"\x00\x01synthetic attachment\xff"
            (root / "body.txt").write_bytes(plain.encode("utf-8"))
            (root / "body.html").write_bytes(rich.encode("utf-8"))
            (root / "attachment.bin").write_bytes(attachment)
            spec = root / "mail.json"
            spec.write_text(json.dumps({
                "to": "recipient@example.com",
                "subject": "离线发送内容回归",
                "body_file": "body.txt",
                "html_file": "body.html",
                "attachments": ["attachment.bin"],
            }, ensure_ascii=False), encoding="utf-8")

            account = "user@mail.ustc.edu.cn"
            message, fingerprint = mail.compose(spec, account)
            original = mail.mime_semantics(message)
            preview_path = root / "preview.html"
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(mail.main([
                    "--json", "--account", account, "preview", str(spec),
                    "--output", str(preview_path),
                ]), 0)
            envelope = json.loads(output.getvalue())
            self.assertTrue(envelope["ok"], envelope)
            self.assertEqual(envelope["data"]["sha256"], fingerprint)
            self.assertTrue(preview_path.is_file())
            self.assertEqual(mail.mime_semantics(message), original)

            smtp = FakeSMTP()
            receipts = root / "receipts"
            with patch.object(mail, "password", return_value="synthetic-only"), \
                    patch.object(mail.smtplib, "SMTP_SSL", return_value=smtp), \
                    patch.object(mail, "RECEIPTS", receipts), \
                    redirect_stdout(io.StringIO()):
                mail.send(message, fingerprint, account, fingerprint)

            delivered = BytesParser(policy=policy.default).parsebytes(smtp.message_bytes)
            plain_part = delivered.get_body(preferencelist=("plain",))
            html_part = delivered.get_body(preferencelist=("html",))
            self.assertEqual(plain_part.get_content().encode("utf-8"), (root / "body.txt").read_bytes())
            self.assertEqual(html_part.get_content().encode("utf-8"), (root / "body.html").read_bytes())
            sent_attachment = next(delivered.iter_attachments())
            self.assertEqual(sent_attachment.get_filename(), "attachment.bin")
            self.assertEqual(sent_attachment.get_payload(decode=True), attachment)
            self.assertEqual(mail.mime_semantics(delivered), original)
            self.assertTrue((receipts / f"{fingerprint}.json").is_file())


if __name__ == "__main__":
    unittest.main()
