"""在临时目录、无 Python PATH、无账号配置下验证发布版 CLI。"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import uuid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path)
    parser.add_argument("--test-credentials", action="store_true",
                        help="可选：用随机假账号验证 Windows 凭据写入、读取和删除")
    args = parser.parse_args()
    executable = args.executable.resolve()
    if not executable.is_file():
        raise SystemExit(f"EXE 不存在：{executable}")

    env = os.environ.copy()
    for name in tuple(env):
        if name.startswith("USTC_MAIL_") or name in {"PYTHONPATH", "PYTHONHOME"}:
            env.pop(name, None)
    system_root = next((value for name, value in env.items()
                        if name.casefold() == "systemroot"), None)
    if not system_root:
        raise SystemExit("环境变量 SystemRoot 缺失，无法构造无 Python PATH 的 smoke 环境")
    env["PATH"] = str(Path(system_root) / "System32")

    with tempfile.TemporaryDirectory(prefix="ustc-mail-smoke-") as directory:
        root = Path(directory)

        def run(*arguments, expected=0, json_output=False):
            result = subprocess.run(
                [str(executable), *map(str, arguments)], cwd=root, env=env,
                stdin=subprocess.DEVNULL, capture_output=True,
                encoding="utf-8", errors="replace", timeout=60)
            assert result.returncode == expected, (
                arguments, result.returncode, result.stdout, result.stderr)
            if not json_output:
                return result.stdout
            try:
                payload = json.loads(result.stdout)
            except json.JSONDecodeError as error:
                raise AssertionError((arguments, result.stdout, result.stderr)) from error
            assert isinstance(payload, dict), payload
            for key in ("ok", "command", "state", "retryable", "data"):
                assert key in payload, (key, payload)
            if expected:
                assert payload["ok"] is False, payload
                assert isinstance(payload.get("error"), dict), payload
            else:
                assert payload["ok"] is True, payload
            return payload

        help_text = run("--help")
        assert "preview" in help_text
        version_text = run("--version")
        assert "0.1.1" in version_text, version_text

        command_help = {
            command: run(command, "--help")
            for command in ("auth", "folders", "search", "read", "preview", "draft", "send")
        }
        for option in ("--from", "--subject", "--since", "--unread", "--folder", "--limit"):
            assert option in command_help["search"], (option, command_help["search"])
        for option in ("--metadata-only", "--max-body-chars", "--max-message-bytes"):
            assert option in command_help["read"], (option, command_help["read"])

        missing_account = run("--json", "auth", "status", expected=1, json_output=True)
        assert missing_account["error"]["code"] == "invalid_config", missing_account

        (root / "body.txt").write_text("离线验证正文。", encoding="utf-8")
        (root / "body.html").write_text(
            '<!doctype html><html><head><script>alert("x")</script></head>'
            '<body onload="alert(1)"><img src="https://example.invalid/x" '
            'onerror="alert(2)"><p>安全预览</p></body></html>', encoding="utf-8")
        (root / "attachment.txt").write_text("合成测试附件", encoding="utf-8")
        spec = {
            "to": "recipient@example.com",
            "subject": "离线验证",
            "body_file": "body.txt",
            "html_file": "body.html",
            "attachments": ["attachment.txt"],
        }
        (root / "mail.json").write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
        preview_args = ("--json", "--account", "user@mail.ustc.edu.cn",
                        "preview", "mail.json", "--output", "preview.html")
        preview_result = run(*preview_args, json_output=True)
        assert preview_result["command"] == "preview", preview_result
        preview_data = preview_result["data"]
        assert isinstance(preview_data, dict) and "sha256" in preview_data, preview_result
        assert "离线验证正文。" in preview_data["body"], preview_result
        digest = preview_data["sha256"]
        assert len(digest) == 64 and all(c in "0123456789abcdef" for c in digest), digest
        preview = (root / "preview.html").read_bytes()
        html = preview.decode("utf-8")
        for marker in ("安全预览", "sandbox", "Content-Security-Policy"):
            assert marker.lower() in html.lower(), marker
        assert "onload=\"alert(1)\"" not in html
        assert "onerror=\"alert(2)\"" not in html
        assert "<script>alert(\"x\")</script>" not in html

        allow_spec = {**spec, "cc": ["recipient+alias@example.com"]}
        (root / "allow.json").write_text(json.dumps(allow_spec), encoding="utf-8")
        denied_cc = {**allow_spec, "cc": ["recipient+alias@example.com", "recipient+alias2@example.com"]}
        (root / "denied-cc.json").write_text(json.dumps(denied_cc), encoding="utf-8")
        rejected = run("--json", "--allow-recipient", "recipient@example.com",
                       "--allow-recipient", "recipient+alias@example.com", "--account",
                       "user@mail.ustc.edu.cn", "send", "denied-cc.json",
                       "--confirm-sha256", "0" * 64,
                       expected=1, json_output=True)
        assert rejected["error"]["code"] == "recipient_not_allowed", rejected
        repeated = run(*preview_args, expected=1, json_output=True)
        assert repeated["ok"] is False
        assert (root / "preview.html").read_bytes() == preview

        # Read-only must reject before attempting credential access or network I/O.
        readonly_cases = (
            ("send", "mail.json", "--confirm-sha256", digest),
            ("draft", "mail.json", "--folder", "Drafts"),
            ("auth", "forget"),
            ("auth", "login"),
        )
        for command_args in readonly_cases:
            rejected = run("--json", "--read-only", "--account", "user@mail.ustc.edu.cn",
                           *command_args, expected=1, json_output=True)
            assert rejected["error"]["code"] == "read_only", rejected
            assert rejected["state"] == "rejected", rejected

        # Rejected recipients and recipient counts must fail before any mail connection.
        blocked_recipient = {**spec, "to": "recipient+alias@example.com"}
        (root / "blocked.json").write_text(json.dumps(blocked_recipient), encoding="utf-8")
        rejected = run("--json", "--allow-recipient", "recipient@example.com",
                       "--account", "user@mail.ustc.edu.cn", "send", "blocked.json",
                       "--confirm-sha256", "0" * 64, expected=1, json_output=True)
        assert not rejected["ok"]
        assert rejected["error"]["code"] == "recipient_not_allowed", rejected

        too_many = {**spec, "cc": [f"recipient+alias{i}@example.com" for i in range(5)]}
        (root / "too-many.json").write_text(json.dumps(too_many), encoding="utf-8")
        rejected = run("--json", "--account", "user@mail.ustc.edu.cn", "draft",
                       "too-many.json", "--folder", "Drafts", expected=1, json_output=True)
        assert not rejected["ok"]
        assert rejected["error"]["code"] == "recipient_limit", rejected

        if args.test_credentials:
            import ustc_mail
            account = "cli-selftest-" + uuid.uuid4().hex + "@mail.ustc.edu.cn"
            secret = "synthetic-test-only-" + uuid.uuid4().hex
            try:
                ustc_mail.credential(account, "write", secret)
                status = run("--json", "--account", account, "auth", "status",
                             json_output=True)
                assert status["data"]["credential_saved"]
                assert not status["data"].get("environment_override", False)
                ustc_mail.credential(account, "delete")
                assert ustc_mail.credential(account) is None
            finally:
                ustc_mail.credential(account, "delete")

    print("EXE 离线验证通过；未读取已有邮箱凭据、连接邮箱或发送邮件。")


if __name__ == "__main__":
    main()
