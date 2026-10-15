"""在无 Python PATH 的临时目录验证打包入口；可选测试随机假凭据。"""

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
    parser.add_argument("--test-credentials", action="store_true")
    args = parser.parse_args()
    executable = args.executable.resolve()
    env = os.environ.copy()
    for name in ("USTC_MAIL_ACCOUNT", "USTC_MAIL_PASSWORD", "PYTHONPATH", "PYTHONHOME"):
        env.pop(name, None)
    env["PATH"] = str(Path(env.get("SystemRoot", "")) / "System32")

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)

        def run(*arguments, expected=0):
            result = subprocess.run([str(executable), *arguments], cwd=root, env=env,
                                    stdin=subprocess.DEVNULL, capture_output=True,
                                    encoding="utf-8", timeout=60)
            assert result.returncode == expected, (arguments, result.stdout, result.stderr)
            return result.stdout

        assert "preview" in run("--help")
        for command in ("auth", "folders", "list", "read", "preview", "draft", "send"):
            run(command, "--help")
        run("list", expected=1)
        (root / "body.txt").write_text("离线验证正文。", encoding="utf-8")
        (root / "attachment.txt").write_text("合成测试附件", encoding="utf-8")
        (root / "mail.json").write_text(json.dumps({"to": "recipient@example.com",
            "subject": "离线验证", "body_file": "body.txt", "attachments": ["attachment.txt"]}), encoding="utf-8")
        arguments = ("--account", "user@mail.ustc.edu.cn", "preview", "mail.json", "--output", "preview.html")
        output = run(*arguments)
        assert "sha256" in output
        preview = (root / "preview.html").read_bytes()
        assert "离线验证正文。" in preview.decode("utf-8")
        run(*arguments, expected=1)
        assert (root / "preview.html").read_bytes() == preview

        if args.test_credentials:
            import ustc_mail
            account = "cli-selftest-" + uuid.uuid4().hex + "@mail.ustc.edu.cn"
            try:
                ustc_mail.credential(account, "write", "synthetic-test-only")
                status = json.loads(run("--account", account, "auth", "status"))
                assert status["credential_saved"] and not status["environment_override"]
                run("--account", account, "auth", "forget")
                assert ustc_mail.credential(account) is None
            finally:
                ustc_mail.credential(account, "delete")
    print("EXE 离线验证通过；未连接邮箱或发送邮件。")


if __name__ == "__main__":
    main()
