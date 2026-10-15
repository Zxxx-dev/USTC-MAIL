"""离线自检：不会连接或发送真实邮件。"""

from email import policy
from email.parser import BytesParser
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import ustc_mail as mail

TEST_ACCOUNT = "user@mail.ustc.edu.cn"


class MailTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(mail.os.environ, {"USTC_MAIL_ACCOUNT": TEST_ACCOUNT}, clear=True))
        self.enterContext(patch("socket.create_connection", side_effect=AssertionError("测试禁止联网")))
        if self._testMethodName != "test_windows_credential_lifecycle":
            self.enterContext(patch.object(mail, "credential", side_effect=AssertionError("测试禁止读取真实凭据")))
        self.enterContext(redirect_stdout(io.StringIO()))
        self.enterContext(redirect_stderr(io.StringIO()))
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "正文.txt").write_text("您好：\n\n测试正文。", encoding="utf-8")
        (self.root / "正文.html").write_text('<p style="text-indent:2em">测试正文。</p>', encoding="utf-8")
        (self.root / "附件.pdf").write_bytes(b"%PDF-test")
        self.spec = self.root / "邮件.json"
        self.config = {"to": "recipient@example.com", "cc": ["recipient+copy@example.com"],
                       "subject": "测试主题", "body_file": "正文.txt", "html_file": "正文.html",
                       "attachments": ["附件.pdf"]}
        self.write_spec()

    def write_spec(self):
        self.spec.write_text(json.dumps(self.config, ensure_ascii=False), encoding="utf-8")

    def compose(self):
        return mail.compose(self.spec, TEST_ACCOUNT)

    def test_missing_account_fails_before_credentials_or_network(self):
        with patch.dict(mail.os.environ, {}, clear=True), patch.object(mail, "password") as password, patch.object(mail, "mailbox") as network:
            self.assertEqual(mail.main(["list"]), 1)
            password.assert_not_called()
            network.assert_not_called()

    def test_account_environment_and_argument_override(self):
        with patch.object(mail, "auth") as auth:
            self.assertEqual(mail.main(["auth", "status"]), 0)
            auth.assert_called_once_with(TEST_ACCOUNT, "status")
            auth.reset_mock()
            self.assertEqual(mail.main(["--account", "user+other@mail.ustc.edu.cn", "auth", "status"]), 0)
            auth.assert_called_once_with("user+other@mail.ustc.edu.cn", "status")

    def test_mime_roundtrip_and_stable_fingerprint(self):
        message, fingerprint = self.compose()
        saved = BytesParser(policy=policy.default).parsebytes(message.as_bytes())
        self.assertEqual(saved["Subject"], "测试主题")
        attachment = next(saved.iter_attachments())
        self.assertEqual(attachment.get_filename(), "附件.pdf")
        self.assertEqual(attachment.get_payload(decode=True), b"%PDF-test")
        self.assertIn("测试正文", saved.get_body(preferencelist=("plain",)).get_content())
        self.assertIn("text-indent", saved.get_body(preferencelist=("html",)).get_content())
        self.assertEqual(fingerprint, self.compose()[1])

    def test_content_changes_invalidate_confirmation(self):
        previous = self.compose()[1]
        for field, value in [("subject", "更改主题"), ("to", "recipient+other@example.com"), ("cc", [])]:
            self.config[field] = value
            self.write_spec()
            current = self.compose()[1]
            self.assertNotEqual(previous, current)
            previous = current

        for name in ["正文.txt", "正文.html", "附件.pdf"]:
            with (self.root / name).open("ab") as output:
                output.write(b"changed")
            current = self.compose()[1]
            self.assertNotEqual(previous, current)
            previous = current

    def test_plain_text_only_preview(self):
        del self.config["html_file"]
        self.write_spec()
        output = self.root / "plain.html"
        self.assertEqual(mail.main(["preview", str(self.spec), "--output", str(output)]), 0)
        self.assertIn("测试正文", output.read_text(encoding="utf-8"))

    def test_reject_header_injection_and_multiple_to(self):
        for recipient in ["recipient@example.com\r\nBcc:recipient+other@example.com", "recipient@example.com,recipient+other@example.com", "recipient@example.com recipient+other@example.com"]:
            self.config["to"] = recipient
            self.write_spec()
            with self.assertRaises(ValueError):
                self.compose()
        self.config["to"] = "recipient@example.com"
        self.config["subject"] = "标题\nBcc:recipient@example.com"
        self.write_spec()
        with self.assertRaises(ValueError):
            self.compose()

    def test_missing_attachment_and_unknown_fields_fail(self):
        (self.root / "附件.pdf").unlink()
        with self.assertRaises(FileNotFoundError):
            self.compose()
        self.config["bcc"] = ["recipient@example.com"]
        self.write_spec()
        with self.assertRaises(ValueError):
            self.compose()

    def test_preview_offline_and_no_overwrite(self):
        output = self.root / "预览.html"
        with patch.object(mail, "mailbox") as network:
            self.assertEqual(mail.main(["preview", str(self.spec), "--output", str(output)]), 0)
            network.assert_not_called()
        self.assertIn("测试正文", output.read_text(encoding="utf-8"))
        original = output.read_bytes()
        self.assertEqual(mail.main(["preview", str(self.spec), "--output", str(output)]), 1)
        self.assertEqual(output.read_bytes(), original)

    def test_read_uses_peek(self):
        message, _ = self.compose()
        client = MagicMock()
        client.uid.return_value = ("OK", [(b"1", message.as_bytes())])
        client.select.return_value = ("OK", [b"1"])
        mail.fetch(client, 123)
        client.uid.assert_called_once_with("fetch", "123", "(BODY.PEEK[])")
        mail.select(client, "INBOX")
        client.select.assert_called_once_with('"INBOX"', readonly=True)

    def test_draft_roundtrip(self):
        message, _ = self.compose()
        client = MagicMock()
        client.append.return_value = ("OK", [b"done"])
        client.select.return_value = ("OK", [b"1"])
        client.uid.side_effect = [("OK", [b"42"]), ("OK", [(b"42", message.as_bytes())])]
        mail.draft(client, message, "Drafts")
        self.assertEqual(client.append.call_args.args[1], r"\Draft")
        self.assertEqual(client.uid.call_args.args[2], "(BODY.PEEK[])")

    def test_draft_mismatch_fails(self):
        message, _ = self.compose()
        client = MagicMock()
        client.append.return_value = ("OK", [b"done"])
        client.select.return_value = ("OK", [b"1"])
        client.uid.side_effect = [("OK", [b"42"]), ("OK", [(b"42", b"Subject: wrong\r\n\r\nwrong")])]
        with self.assertRaisesRegex(ValueError, "回读内容不一致"):
            mail.draft(client, message, "Drafts")

    def test_send_requires_confirmation_before_network(self):
        message, fingerprint = self.compose()
        with patch.object(mail, "password") as password:
            with self.assertRaises(ValueError):
                mail.send(message, fingerprint, TEST_ACCOUNT, "wrong")
            password.assert_not_called()

    def test_auth_error_diagnostic_does_not_print_server_secret(self):
        message, fingerprint = self.compose()
        with patch.object(mail, "compose", return_value=(message, fingerprint)), patch.object(mail, "send", side_effect=mail.smtplib.SMTPAuthenticationError(535, b"sensitive-server-text")), redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(mail.main(["send", str(self.spec), "--confirm-sha256", fingerprint]), 1)
            self.assertIn("535", errors.getvalue())
            self.assertNotIn("sensitive-server-text", errors.getvalue())

    def test_saved_password_and_account_isolation(self):
        with patch.dict(mail.os.environ, {}, clear=True), patch.object(mail, "credential", return_value="saved-secret") as store, patch.object(mail, "input_password") as prompt:
            self.assertEqual(mail.password("user+other@mail.ustc.edu.cn"), "saved-secret")
            store.assert_called_once_with("user+other@mail.ustc.edu.cn")
            prompt.assert_not_called()

    def test_environment_password_has_priority(self):
        with patch.dict(mail.os.environ, {"USTC_MAIL_PASSWORD": "env-secret"}), patch.object(mail, "credential") as store:
            self.assertEqual(mail.password(TEST_ACCOUNT), "env-secret")
            store.assert_not_called()

    def test_missing_password_prompts_once(self):
        with patch.dict(mail.os.environ, {}, clear=True), patch.object(mail, "credential", return_value=None), patch.object(mail, "input_password", return_value="input-secret") as prompt:
            self.assertEqual(mail.password(TEST_ACCOUNT), "input-secret")
            prompt.assert_called_once()

    def test_auth_login_validates_before_saving(self):
        with patch.dict(mail.os.environ, {"USTC_MAIL_PASSWORD": "new-secret"}), patch.object(mail, "credential") as store, patch.object(mail.smtplib, "SMTP_SSL") as smtp:
            client = smtp.return_value.__enter__.return_value
            client.login.side_effect = mail.smtplib.SMTPAuthenticationError(535, b"bad")
            with self.assertRaises(mail.smtplib.SMTPAuthenticationError):
                mail.auth(TEST_ACCOUNT, "login")
            store.assert_not_called()
            client.login.side_effect = None
            mail.auth(TEST_ACCOUNT, "login")
            store.assert_called_once_with(TEST_ACCOUNT, "write", "new-secret")
            client.send_message.assert_not_called()

    def test_auth_status_does_not_print_secret(self):
        with patch.object(mail, "credential", return_value="do-not-print"), redirect_stdout(io.StringIO()) as output:
            mail.auth(TEST_ACCOUNT, "status")
            self.assertTrue(json.loads(output.getvalue())["credential_saved"])
            self.assertNotIn("do-not-print", output.getvalue())

    @unittest.skipUnless(mail.os.name == "nt" and mail.os.environ.get("USTC_MAIL_TEST_CREDENTIALS") == "1",
                         "仅显式启用时测试随机账号的Windows凭据存储")
    def test_windows_credential_lifecycle(self):
        import uuid
        account = "cli-selftest-" + uuid.uuid4().hex + "@mail.ustc.edu.cn"
        try:
            self.assertIsNone(mail.credential(account))
            mail.credential(account, "write", "非真实凭据-test")
            self.assertEqual(mail.credential(account), "非真实凭据-test")
            import subprocess
            code = "import ustc_mail,sys; assert ustc_mail.password(sys.argv[1]) == '非真实凭据-test'"
            child_env = mail.os.environ.copy()
            child_env.pop("USTC_MAIL_PASSWORD", None)
            result = subprocess.run([mail.sys.executable, "-c", code, account], cwd=Path(mail.__file__).parent,
                                    env=child_env, stdin=subprocess.DEVNULL, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, "独立进程须能在无输入状态下复用凭据")
            mail.credential(account, "delete")
            self.assertIsNone(mail.credential(account))
        finally:
            mail.credential(account, "delete")

    def test_send_duplicate_block_and_unknown_result(self):
        message, fingerprint = self.compose()
        for fail in [False, True]:
            receipts = self.root / str(fail)
            with patch.object(mail, "RECEIPTS", receipts), patch.object(mail, "password", return_value="test"), patch.object(mail.smtplib, "SMTP_SSL") as smtp:
                client = smtp.return_value.__enter__.return_value
                client.send_message.return_value = {}
                if fail:
                    client.send_message.side_effect = OSError("timeout")
                    with self.assertRaises(OSError):
                        mail.send(message, fingerprint, TEST_ACCOUNT, fingerprint)
                else:
                    mail.send(message, fingerprint, TEST_ACCOUNT, fingerprint)
                with self.assertRaisesRegex(ValueError, "已有同内容发送记录"):
                    mail.send(message, fingerprint, TEST_ACCOUNT, fingerprint)
                self.assertEqual(client.send_message.call_count, 1)
                record = json.loads((receipts / (fingerprint + ".json")).read_text(encoding="utf-8"))
                self.assertEqual(record["state"], "attempting" if fail else "smtp_accepted")

    def test_partial_refusal_recorded(self):
        message, fingerprint = self.compose()
        with patch.object(mail, "RECEIPTS", self.root / "receipts"), patch.object(mail, "password", return_value="test"), patch.object(mail.smtplib, "SMTP_SSL") as smtp:
            smtp.return_value.__enter__.return_value.send_message.return_value = {"recipient+copy@example.com": (550, b"no")}
            with self.assertRaisesRegex(ValueError, "部分收件人"):
                mail.send(message, fingerprint, TEST_ACCOUNT, fingerprint)
            record = json.loads(next(mail.RECEIPTS.glob("*.json")).read_text(encoding="utf-8"))
            self.assertEqual(record["state"], "partially_refused")


if __name__ == "__main__":
    unittest.main()
