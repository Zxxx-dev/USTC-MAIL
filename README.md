# USTC MAIL

给 Agent 和终端使用的中国科学技术大学邮箱工具：只读看信、离线预览、草稿、按内容指纹确认发送。

## 选择安装方式

**Windows 免安装版**：从 [Releases](https://github.com/Zxxx-dev/USTC-MAIL/releases/latest) 下载 `ustc-mail.exe`，在所在目录打开 PowerShell：

```powershell
.\ustc-mail.exe --help
.\ustc-mail.exe --account user@mail.ustc.edu.cn auth login
```

**Python 版**（Python 3.11+，需要 Git）：

```sh
python -m pip install "git+https://github.com/Zxxx-dev/USTC-MAIL.git"
ustc-mail --account user@mail.ustc.edu.cn auth login
```

Windows 的 `auth login` 会隐藏输入并保存客户端专用密码。其他系统在首次联网命令中隐藏输入密码，见[使用教程](docs/usage.md)。

## 给 Agent 使用

将仓库内的 [skills/ustc-mail](skills/ustc-mail/SKILL.md) 放入 Agent 支持的技能目录；或让 Agent 直接阅读该文件。先安装上面的 CLI，再提供账号与已安装入口，密码由用户在本地终端输入。

账号通过 `--account` 或 `USTC_MAIL_ACCOUNT` 提供。发送必须有用户对具体邮件的授权，内容指纹不能代替授权。

[完整教程与密码截图](docs/usage.md) · [开发与验证](docs/development.md) · [类似项目](docs/alternatives.md) · [MIT License](LICENSE)
