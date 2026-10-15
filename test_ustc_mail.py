"""离线自检：不会连接或发送真实邮件。"""

from email import policy
from email.parser import BytesParser
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import base64
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
        client.uid.side_effect = [("OK", [(b"1 RFC822.SIZE 10", b"")]),
                                  ("OK", [(b"1", message.as_bytes())])]
        client.select.return_value = ("OK", [b"1"])
        mail.fetch(client, 123)
        self.assertEqual(client.uid.call_args_list[0].args, ("fetch", "123", "(RFC822.SIZE)"))
        self.assertEqual(client.uid.call_args_list[1].args, ("fetch", "123", "(BODY.PEEK[])"))
        mail.select(client, "INBOX")
        client.select.assert_called_once_with('"INBOX"', readonly=True)

    def test_draft_roundtrip(self):
        message, _ = self.compose()
        client = MagicMock()
        client.append.return_value = ("OK", [b"done"])
        client.select.return_value = ("OK", [b"1"])
        client.uid.side_effect = [("OK", [b"42"]), ("OK", [(b"42 RFC822.SIZE 1200", b"")]),
                                  ("OK", [(b"42", message.as_bytes())])]
        mail.draft(client, message, "Drafts")
        self.assertEqual(client.append.call_args.args[1], r"\Draft")
        self.assertEqual(client.uid.call_args_list[-1].args[2], "(BODY.PEEK[])")

    def test_draft_mismatch_fails(self):
        message, _ = self.compose()
        client = MagicMock()
        client.append.return_value = ("OK", [b"done"])
        client.select.return_value = ("OK", [b"1"])
        client.uid.side_effect = [("OK", [b"42"]), ("OK", [(b"42 RFC822.SIZE 40", b"")]),
                                  ("OK", [(b"42", b"Subject: wrong\r\n\r\nwrong")])]
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

    def test_json_output_is_one_stable_envelope_and_accepts_global_after_command(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(mail.main(["preview", str(self.spec), "--json"]), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(set(result), {"ok", "command", "state", "retryable", "data"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["command"], "preview")
        self.assertEqual(len(result["data"]["sha256"]), 64)
        self.assertIn("body", result["data"])
        self.assertIn("html", result["data"])
        self.assertIn("attachments", result["data"])
        self.assertNotIn("output", result["data"])
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(mail.main(["--json", "--version"]), 0)
        self.assertEqual(json.loads(output.getvalue())["data"]["version"], "0.1.0")
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(mail.main(["preview", "--json", "missing.json"]), 1)
        result = json.loads(output.getvalue())
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "invalid_config")
        self.assertFalse(result["retryable"])

    def test_read_only_blocks_mutations_before_any_side_effect(self):
        with patch.dict(mail.os.environ, {"USTC_MAIL_READ_ONLY": "1"}), patch.object(mail, "credential") as credential, patch.object(mail, "mailbox") as mailbox:
            self.assertEqual(mail.main(["auth", "forget", "--json"]), 1)
            result = json.loads(mail.sys.stdout.getvalue().splitlines()[-1])
            self.assertEqual(result["error"]["code"], "read_only")
            credential.assert_not_called()
            mailbox.assert_not_called()
        with patch.object(mail, "mailbox") as mailbox:
            self.assertEqual(mail.main(["--read-only", "draft", str(self.spec), "--folder", "Drafts"]), 1)
            mailbox.assert_not_called()

    def test_recipient_limit_and_allowlist_are_checked_in_compose(self):
        self.assertIsNotNone(mail.compose(self.spec, TEST_ACCOUNT, ["recipient@example.com", "recipient+copy@example.com"]))
        with self.assertRaisesRegex(mail.WorkflowError, "白名单"):
            mail.compose(self.spec, TEST_ACCOUNT, ["recipient@example.com"])
        self.config["cc"] = [f"u{i}@example.com" for i in range(5)]
        self.write_spec()
        with self.assertRaisesRegex(mail.WorkflowError, "上限为 5"):
            mail.compose(self.spec, TEST_ACCOUNT)

    def test_imap_quote_rejects_control_characters_and_escapes_quotes(self):
        self.assertEqual(mail.imap_quote('A "folder"'), '"A \\"folder\\""')
        with self.assertRaises(ValueError):
            mail.imap_quote("INBOX\r\nALL")

    def test_fetch_rejects_oversize_before_body_fetch(self):
        client = MagicMock()
        client.uid.return_value = ("OK", [b"8 (UID 8 RFC822.SIZE 1000)"])
        with self.assertRaisesRegex(mail.WorkflowError, "未下载正文"):
            mail.fetch(client, 8, max_message_bytes=100)
        self.assertEqual(client.uid.call_count, 1)
        self.assertEqual(client.uid.call_args.args[2], "(RFC822.SIZE)")

    def test_fetch_parses_real_imaplib_size_response_and_rejects_missing_size(self):
        message, _ = self.compose()
        client = MagicMock()
        client.uid.side_effect = [("OK", [b"8 (UID 8 RFC822.SIZE 1000)"]),
                                  ("OK", [(b"8 (UID 8 BODY[] {1000})", message.as_bytes())])]
        fetched = mail.fetch(client, 8)
        self.assertEqual(str(fetched["Subject"]), "测试主题")
        self.assertEqual(client.uid.call_args_list[0].args[2], "(RFC822.SIZE)")
        self.assertEqual(client.uid.call_args_list[1].args[2], "(BODY.PEEK[])")
        client.reset_mock()
        client.uid.side_effect = None
        client.uid.return_value = ("OK", [b"8 (UID 8 FLAGS ())"])
        with self.assertRaisesRegex(mail.WorkflowError, "未下载邮件正文"):
            mail.fetch(client, 8)
        self.assertEqual(client.uid.call_count, 1)

    def test_draft_compares_mime_semantics_and_reports_unverified_uid(self):
        message, _ = self.compose()
        wire = message.as_bytes()
        parsed = BytesParser(policy=policy.default).parsebytes(wire)
        for part in parsed.walk():
            if part.get_content_type() == "text/plain":
                part.set_param("charset", '"UTF-8"')
        equivalent = parsed.as_bytes()
        self.assertEqual(mail.mime_semantics(message), mail.mime_semantics(BytesParser(policy=policy.default).parsebytes(equivalent)))
        client = MagicMock()
        client.append.return_value = ("OK", [b"done"])
        client.select.return_value = ("OK", [b"1"])
        client.uid.side_effect = [("OK", [b"42"]), ("NO", [b"temporary search failure"])]
        with self.assertRaises(mail.WorkflowError) as raised:
            mail.draft(client, message, "Drafts")
        self.assertEqual(raised.exception.code, "draft_submitted_unverified")
        self.assertEqual(raised.exception.data["uid"], "42")

    def test_mime_comparison_ignores_transfer_encoding_and_folding(self):
        message, _ = self.compose()
        parsed = BytesParser(policy=policy.default).parsebytes(message.as_bytes())
        for part in parsed.walk():
            if part.get_content_type() in {"text/plain", "text/html"}:
                decoded = part.get_payload(decode=True)
                if "Content-Transfer-Encoding" in part:
                    del part["Content-Transfer-Encoding"]
                part.set_payload(base64.encodebytes(decoded).decode("ascii"))
                part["Content-Transfer-Encoding"] = "base64"
        self.assertEqual(mail.mime_semantics(message), mail.mime_semantics(parsed))
        parsed.replace_header("Subject", "completely different")
        self.assertNotEqual(mail.mime_semantics(message), mail.mime_semantics(parsed))

    def test_mime_comparison_detects_repeated_body_and_attachment_changes(self):
        message, _ = self.compose()
        baseline = mail.mime_semantics(message)
        parsed = BytesParser(policy=policy.default).parsebytes(message.as_bytes())
        alternative = next(part for part in parsed.walk() if part.get_content_type() == "multipart/alternative")
        extra = mail.EmailMessage(policy=policy.default)
        extra.set_content("unverified extra text body")
        alternative.attach(extra)
        self.assertNotEqual(baseline, mail.mime_semantics(parsed))

        removed = BytesParser(policy=policy.default).parsebytes(message.as_bytes())
        removed.set_payload([part for part in removed.iter_parts() if part.get_content_disposition() != "attachment"])
        self.assertNotEqual(baseline, mail.mime_semantics(removed))

        added = BytesParser(policy=policy.default).parsebytes(message.as_bytes())
        added.add_attachment(b"new attachment", maintype="application", subtype="octet-stream", filename="extra.bin")
        self.assertNotEqual(baseline, mail.mime_semantics(added))

    def test_search_uses_only_quoted_whitelisted_criteria_and_metadata_rows(self):
        message, _ = self.compose()
        client = MagicMock()
        client.uid.return_value = ("OK", [b"17"])
        args = type("Args", (), {"sender": 'sender"@example.com', "subject": "budget update",
                                  "since": "2026-01-02", "unread": True, "folder": "INBOX", "limit": 5})()
        with patch.object(mail, "fetch", return_value=message):
            rows = mail._search(client, args)
        self.assertEqual(client.uid.call_args.args,
                         ("search", None, "FROM", '"sender\\"@example.com"', "SUBJECT", '"budget update"',
                          "SINCE", "02-Jan-2026", "UNSEEN"))
        self.assertEqual(rows[0]["folder"], "INBOX")
        self.assertEqual(rows[0]["uid"], 17)

    def test_unicode_search_uses_utf8_charset_and_encoded_quoted_terms(self):
        message, _ = self.compose()
        class WireProbe(mail.imaplib.IMAP4):
            def __init__(self):
                self.state = "SELECTED"
                self.untagged_responses = {}
                self.is_readonly = False
                self._encoding = "ascii"
                self.utf8_enabled = False
                self.debug = 0
                self.literal = None
                self.wire = []

            def _new_tag(self):
                return b"A001"

            def send(self, payload):
                self.wire.append(payload)

            def _get_response(self):
                return None

            def _log(self, _message):
                pass

            def uid(self, command, *args):
                self._command("UID", command.upper(), *args)
                return "OK", [b"17"]

        client = WireProbe()
        args = type("Args", (), {"sender": None, "subject": "纳米 材料", "since": None,
                                  "unread": False, "folder": "INBOX", "limit": 5})()
        with patch.object(mail, "fetch", return_value=message):
            rows = mail._search(client, args)
        self.assertEqual(rows[0]["uid"], 17)
        self.assertEqual(client.wire[0], b"A001 UID SEARCH CHARSET UTF-8 SUBJECT {13}\r\n")
        self.assertEqual(client.wire[1], "纳米 材料".encode("utf-8"))
        self.assertEqual(client.wire[2], b"\r\n")
        self.assertIsNone(client.literal)

    def test_two_unicode_search_terms_intersect_uids(self):
        message, _ = self.compose()

        class WireProbe(mail.imaplib.IMAP4):
            def __init__(self):
                self.state = "SELECTED"
                self.untagged_responses = {}
                self.is_readonly = False
                self._encoding = "ascii"
                self.utf8_enabled = False
                self.debug = 0
                self.literal = None
                self.wire = []
                self.results = {"张三@example.cn".encode(): [b"17", b"18"],
                                "纳米材料".encode(): [b"18", b"19"]}

            def _new_tag(self):
                return b"A001"

            def send(self, payload):
                self.wire.append(payload)

            def _get_response(self):
                return None

            def _log(self, _message):
                pass

            def uid(self, command, *args):
                literal = self.literal
                self._command("UID", command.upper(), *args)
                return "OK", [b" ".join(self.results[literal])]

        client = WireProbe()
        args = type("Args", (), {"sender": "张三@example.cn", "subject": "纳米材料", "since": None,
                                  "unread": False, "folder": "INBOX", "limit": 5})()
        with patch.object(mail, "fetch", return_value=message):
            rows = mail._search(client, args)
        self.assertEqual([row["uid"] for row in rows], [18])
        self.assertEqual(len([wire for wire in client.wire if b"UID SEARCH" in wire]), 2)
        self.assertIsNone(client.literal)

    def test_json_read_and_send_return_structured_objects(self):
        message, fingerprint = self.compose()
        client = MagicMock()
        output = io.StringIO()
        with patch.object(mail, "mailbox") as mailbox, patch.object(mail, "select"), \
                patch.object(mail, "fetch", return_value=message), redirect_stdout(output):
            mailbox.return_value.__enter__.return_value = client
            self.assertEqual(mail.main(["--json", "read", "17"]), 0)
        read_result = json.loads(output.getvalue())
        self.assertIsInstance(read_result["data"], dict)
        self.assertTrue(read_result["data"]["external_data"])
        self.assertIsInstance(read_result["data"]["body"], str)

        output = io.StringIO()
        receipts = self.root / "json-receipts"
        with patch.object(mail, "RECEIPTS", receipts), patch.object(mail, "password", return_value="test"), \
                patch.object(mail, "compose", return_value=(message, fingerprint)), \
                patch.object(mail.smtplib, "SMTP_SSL") as smtp, redirect_stdout(output):
            smtp.return_value.__enter__.return_value.send_message.return_value = {}
            self.assertEqual(mail.main(["--json", "send", str(self.spec), "--confirm-sha256", fingerprint]), 0)
        send_result = json.loads(output.getvalue())
        self.assertEqual(send_result["state"], "smtp_accepted")
        self.assertIsInstance(send_result["data"], dict)
        self.assertEqual(send_result["data"]["state"], "smtp_accepted")

    def test_json_search_is_array_and_draft_has_verified_state(self):
        rows = [{"folder": "INBOX", "uid": 17, "external_data": True, "Subject": "sample"}]
        output = io.StringIO()
        with patch.object(mail, "mailbox") as mailbox, patch.object(mail, "select"), \
                patch.object(mail, "_search", return_value=rows), redirect_stdout(output):
            self.assertEqual(mail.main(["--json", "search", "--subject", "sample"]), 0)
        search_result = json.loads(output.getvalue())
        self.assertIsInstance(search_result["data"], list)
        self.assertTrue(search_result["data"][0]["external_data"])

        message, _ = self.compose()
        output = io.StringIO()
        with patch.object(mail, "mailbox") as mailbox, patch.object(mail, "draft", return_value={
                "folder": "Drafts", "uid": 42, "verified": True, "external_data": True}), \
                redirect_stdout(output):
            self.assertEqual(mail.main(["--json", "draft", str(self.spec), "--folder", "Drafts"]), 0)
        draft_result = json.loads(output.getvalue())
        self.assertEqual(draft_result["state"], "draft_verified")
        self.assertEqual(draft_result["data"]["uid"], 42)

    def test_json_missing_account_has_invalid_config_code(self):
        output = io.StringIO()
        with patch.dict(mail.os.environ, {}, clear=True), redirect_stdout(output):
            self.assertEqual(mail.main(["--json", "list"]), 1)
        result = json.loads(output.getvalue())
        self.assertEqual(result["error"]["code"], "invalid_config")

    def test_draft_append_exception_is_unknown_and_not_retryable(self):
        message, _ = self.compose()
        client = MagicMock()
        client.append.side_effect = OSError("lost connection")
        with self.assertRaises(mail.WorkflowError) as raised:
            mail.draft(client, message, "Drafts")
        self.assertEqual(raised.exception.code, "draft_result_unknown")
        self.assertFalse(raised.exception.retryable)

    def test_preview_html_is_sandboxed_and_csp_restricts_content(self):
        (self.root / "正文.html").write_text('<meta content="0;url=https://bad.example" http-equiv="refresh"><p>safe</p>', encoding="utf-8")
        message, _ = self.compose()
        output = self.root / "safe.html"
        mail.save_preview(output, message)
        rendered = output.read_text(encoding="utf-8")
        self.assertIn('<iframe sandbox style="display:block;width:100%;height:70vh;min-height:480px;border:0;overflow:auto" srcdoc=', rendered)
        self.assertIn("default-src &#x27;none&#x27;", rendered)
        self.assertNotIn("allow-scripts", rendered)
        self.assertNotIn("allow-top-navigation", rendered)
        self.assertNotIn("bad.example", rendered)

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
                    with self.assertRaisesRegex(mail.WorkflowError, "勿直接重试"):
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

    def test_smtp_accepted_then_receipt_or_quit_failure_is_not_reported_failed(self):
        message, fingerprint = self.compose()
        for failure in ("receipt", "quit"):
            receipts = self.root / failure
            with patch.object(mail, "RECEIPTS", receipts), patch.object(mail, "password", return_value="test"), patch.object(mail.smtplib, "SMTP_SSL") as smtp:
                client = smtp.return_value.__enter__.return_value
                client.send_message.return_value = {}
                if failure == "receipt":
                    with patch.object(Path, "write_text", side_effect=OSError("disk error")):
                        with self.assertRaises(mail.WorkflowError) as raised:
                            mail.send(message, fingerprint, TEST_ACCOUNT, fingerprint)
                else:
                    smtp.return_value.__exit__.side_effect = OSError("quit failed")
                    with self.assertRaises(mail.WorkflowError) as raised:
                        mail.send(message, fingerprint, TEST_ACCOUNT, fingerprint)
                self.assertEqual(raised.exception.state, "smtp_accepted")
                self.assertFalse(raised.exception.retryable)

    def test_send_auth_and_connect_failures_are_pre_submission(self):
        message, fingerprint = self.compose()
        receipts = self.root / "pre-submit"
        for failure in ("auth", "connect"):
            output = io.StringIO()
            with patch.object(mail, "RECEIPTS", receipts), patch.object(mail, "password", return_value="test"), \
                    patch.object(mail, "compose", return_value=(message, fingerprint)), \
                    patch.object(mail.smtplib, "SMTP_SSL") as smtp, redirect_stdout(output):
                if failure == "auth":
                    smtp.return_value.__enter__.return_value.login.side_effect = mail.smtplib.SMTPAuthenticationError(535, b"bad")
                else:
                    smtp.side_effect = OSError("connection refused")
                result = mail.main(["--json", "send", str(self.spec), "--confirm-sha256", fingerprint])
            self.assertEqual(result, 1)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["error"]["code"], "authentication_failed" if failure == "auth" else "network_error")
            self.assertFalse(receipts.exists())
            if failure == "auth":
                smtp.return_value.__enter__.return_value.send_message.assert_not_called()

    def test_partial_refusal_state_survives_receipt_and_quit_failures(self):
        message, fingerprint = self.compose()
        refused = {"recipient+copy@example.com": (550, b"no")}
        for failure in ("receipt", "quit"):
            receipts = self.root / ("partial-" + failure)
            with patch.object(mail, "RECEIPTS", receipts), patch.object(mail, "password", return_value="test"), \
                    patch.object(mail.smtplib, "SMTP_SSL") as smtp:
                smtp.return_value.__enter__.return_value.send_message.return_value = refused
                if failure == "receipt":
                    with patch.object(Path, "write_text", side_effect=OSError("disk error")):
                        with self.assertRaises(mail.WorkflowError) as raised:
                            mail.send(message, fingerprint, TEST_ACCOUNT, fingerprint)
                    self.assertEqual(raised.exception.code, "partial_refusal_unrecorded")
                else:
                    smtp.return_value.__exit__.side_effect = OSError("quit failed")
                    with self.assertRaises(mail.WorkflowError) as raised:
                        mail.send(message, fingerprint, TEST_ACCOUNT, fingerprint)
                    self.assertEqual(raised.exception.code, "partial_refusal")
                self.assertEqual(raised.exception.state, "partially_refused")
                self.assertEqual(raised.exception.data["refused_recipients"], list(refused))
                self.assertFalse(raised.exception.retryable)


if __name__ == "__main__":
    unittest.main()
