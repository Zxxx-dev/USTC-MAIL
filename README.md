<p align="center">
  <img src="assets/logo.png" alt="USTC MAIL logo" width="160">
</p>

<h1 align="center">USTC MAIL</h1>

<p align="center">面向中国科学技术大学邮箱的命令行工具，可供用户与 Agent 查看邮件、预览内容、保存草稿，并在逐封授权后发送。</p>

## 安装

**Windows 免安装版**：从 [Releases](https://github.com/Zxxx-dev/USTC-MAIL/releases/latest) 下载 `ustc-mail.exe`，在 PowerShell 中运行：

```powershell
.\ustc-mail.exe --help
.\ustc-mail.exe --json --account user@mail.ustc.edu.cn auth status
```

**Python 版**：需要 Python 3.11+；安装 Git 后运行：

```sh
python -m pip install "git+https://github.com/Zxxx-dev/USTC-MAIL.git"
ustc-mail --help
ustc-mail --json --account user@mail.ustc.edu.cn auth status
```

## Agent 使用

推荐读取仓库内的 [USTC MAIL 技能](skills/ustc-mail/SKILL.md)，并用 `--json` 获取稳定的单 JSON 输出。`--account`、`--json`、`--read-only` 和重复的 `--allow-recipient` 等全局参数放在子命令前。账号可通过 `USTC_MAIL_ACCOUNT` 设置。

程序可阻止误操作，但不构成对运行它的同一用户的完整 shell 权限隔离。内容指纹不能证明用户授权；发送前仍须得到对具体邮件的明确授权。

[使用教程与密码设置截图](docs/usage.md) · [开发与打包](docs/development.md) · [替代项目](docs/alternatives.md) · [MIT License](LICENSE)
