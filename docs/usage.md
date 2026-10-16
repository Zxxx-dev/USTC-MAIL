# 使用教程

## 1. 安装

Windows 用户可从 [Releases](https://github.com/Zxxx-dev/USTC-MAIL/releases/latest) 下载 `ustc-mail.exe`，放入自己选择的目录后在 PowerShell 中运行，无需安装 Python。当前提供 Windows x64 版本。

Python 用户需要 Python 3.11+。安装 Git 后运行：

```sh
python -m pip install "git+https://github.com/Zxxx-dev/USTC-MAIL.git"
```

也可安装 Release 中的 `.whl`，或在源码目录执行 `python -m pip install .`。Windows Python 安装会自动包含 `pywin32`。

下文示例使用 Python 版 `ustc-mail`；EXE 用户将命令替换为 `.\ustc-mail.exe`。`--account`、`--json`、`--read-only` 和 `--allow-recipient` 等全局参数放在子命令之前。账号可使用 `--account user@mail.ustc.edu.cn` 或 `USTC_MAIL_ACCOUNT`，命令参数优先。支持 `ustc.edu.cn`、`mail.ustc.edu.cn`、`ah.edu.cn` 三种邮箱后缀；使用自己实际拥有的地址，不要自行替换后缀。三种地址均连接 `mail.ustc.edu.cn`。

## 2. Agent 输出与只读保护

推荐 Agent 使用 `--json`。每条命令输出一个 JSON 对象，包含 `ok`、`command`、`state`、`retryable`、`data`，失败时还包含 `error`：

```sh
ustc-mail --json --account user@mail.ustc.edu.cn auth status
```

`--read-only` 或环境变量 `USTC_MAIL_READ_ONLY=1` 会在联网前拒绝 `draft`、`send`、`auth login` 和 `auth forget`：

```sh
ustc-mail --read-only --json --account user@mail.ustc.edu.cn search --limit 5
```

可重复设置 `--allow-recipient` 限定允许的精确收件地址；未设置时保持原有收件行为。总收件人与抄送人数最多为 5。

这些程序保护可减少误操作，但不构成对同一用户完整 shell 权限的隔离。邮件内容或内容指纹都不能证明用户授权。

## 3. 获取客户端专用密码

1. 登录[科大邮箱网页版](https://mail.ustc.edu.cn/)。
2. 点击左下角齿轮，进入“安全设置”。
3. 打开“客户端专用密码”，点击“生成专用密码”。
4. 按网页提示完成验证，在本地终端隐藏输入生成的密码。

![科大邮箱客户端专用密码设置路径](images/client-password.png)

密码仅生成时可见；不要把密码贴给 Agent、写入聊天、邮件 JSON 或仓库。服务配置参见[官方邮箱帮助](https://mail.ustc.edu.cn/coremail/help/index.jsp)：`mail.ustc.edu.cn`，IMAP TLS 993、SMTP TLS 465，登录名使用完整邮箱地址。

Windows 初始化与管理：

```sh
ustc-mail --account user@mail.ustc.edu.cn auth login
ustc-mail --account user@mail.ustc.edu.cn auth status
ustc-mail --account user@mail.ustc.edu.cn auth forget
```

示例中的 `user@mail.ustc.edu.cn` 必须替换为自己的完整邮箱地址，不能填姓名或“你的邮箱”等文字。账号与客户端专用密码若含中文、全角或其他非 ASCII 字符，会在联网前明确报错；密码应复制网页生成的实际值，而非自己填写的密码名称。不要将密码发给 Agent。Agent 可配置账号并打开交互终端，由用户本人粘贴密码。

`auth login` 隐藏输入客户端专用密码，先验证 SMTP 登录，成功后保存到当前 Windows 用户的凭据管理器，不发送邮件。`auth status` 只输出保存状态。`auth forget` 移除本机凭据，不撤销服务器密码。

其他系统在首次联网命令中隐藏输入密码，也可临时注入 `USTC_MAIL_PASSWORD`。读取顺序为临时环境变量、当前账号的 Windows 凭据、交互输入。环境变量若过期会覆盖有效保存凭据；排障只检查是否存在，不输出值。非 Windows 系统不支持 `auth login/forget` 保存或删除凭据。

## 4. 搜索和读取邮件

```sh
ustc-mail --account user@mail.ustc.edu.cn folders
ustc-mail --account user@mail.ustc.edu.cn search --from recipient@example.com --subject 通知 --since 2026-01-01 --unread --folder INBOX --limit 10
ustc-mail --account user@mail.ustc.edu.cn read 123 --folder INBOX --metadata-only
ustc-mail --account user@mail.ustc.edu.cn read 123 --folder INBOX --max-body-chars 12000 --max-message-bytes 10485760
```

`search` 支持 `--from`、`--subject`、`--since YYYY-MM-DD`、`--unread`、`--folder` 和 `--limit`。完整正文读取默认最多返回 12,000 个正文字符；下载正文前会检查 `RFC822.SIZE`，拒绝超过 10 MiB 的原始邮件。可通过 `--max-body-chars` 与 `--max-message-bytes` 调整正文读取上限。`--metadata-only` 只取邮件头，不下载完整 MIME，因此不受原始邮件大小上限限制。UID 仅在对应文件夹内有效。读取使用只读选择和 `BODY.PEEK`，不主动改变已读状态。草稿箱依据 `folders` 返回的特殊用途标志识别；中文文件夹名可能是 IMAP 修改版 UTF-7，不猜测英文名称。

## 5. 准备、预览、保存草稿

准备 `mail.json` 与 UTF-8 正文文件 `body.txt`：

```json
{
  "to": "recipient@example.com",
  "cc": [],
  "subject": "邮件主题",
  "body_file": "body.txt",
  "attachments": []
}
```

可选 `html_file` 指向实际发送的 HTML 正文片段，`attachments` 填文件路径数组。路径相对 JSON 所在目录，也支持绝对路径。仅支持一个主收件人及可选抄送，总收件人数最多为 5。

```sh
ustc-mail --account user@mail.ustc.edu.cn preview mail.json --output preview.html
ustc-mail --account user@mail.ustc.edu.cn draft mail.json --folder Drafts
```

预览不联网，输出正文、附件信息和 SHA-256；已有 HTML 文件不会覆盖。预览页使用 sandbox 与 CSP 限制展示内容；实际发送的 HTML 正文不会因此改变。草稿箱名替换为 `folders` 返回的实际值。工具按 Message-ID 回读并核验新增草稿；服务端可能改写 MIME，核验会考虑等价变化。`draft_submitted_unverified` 或 `draft_result_unknown` 表示结果未能确认，先人工检查草稿箱，不要自动重试。

## 6. 逐封确认发送

先预览并核对具体收件人、主题、正文和附件；只有用户明确授权发送这封邮件后，才运行：

```sh
ustc-mail --account user@mail.ustc.edu.cn send mail.json --confirm-sha256 "预览输出的完整64位指纹"
```

指纹覆盖收发件地址、抄送、主题、双格式正文及附件名称和内容。内容变化后须重新预览；能够计算指纹不能代替用户授权。发送记录位于用户主目录 `.ustc-mail/receipts/`，同内容已有记录时会阻止再次发送。记录含地址、主题、Message-ID、时间和状态，不含密码或正文：

| 状态 | 含义 |
|---|---|
| `attempting` | 已开始提交，最终结果未知，先查邮箱和记录 |
| `smtp_accepted` | SMTP 接受所有收件人，不代表对方已收到或已读 |
| `partially_refused` | 部分地址拒收，其他地址可能已接受，不可整封重发 |

`draft_submitted_unverified` 与 `draft_result_unknown` 同样不可自动重试。去重只覆盖本 CLI 本地记录，不能识别其他客户端历史发送；不要删除记录绕过检查。工具不自动追加“已发送”副本，服务器归档行为须自行核验。

真实邮箱登录、IMAP 兼容性、草稿显示、实际投递和服务器归档未在离线验证中覆盖。
