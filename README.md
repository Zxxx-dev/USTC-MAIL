# USTCMAILCLI

中国科学技术大学邮箱命令行工具：只读列信/看信、离线预览、保存并回读草稿，以及按内容指纹确认发送。支持一个主收件人及可选抄送。

需要 Python 3.11+。邮件协议使用标准库，Windows 凭据保存依赖 `pywin32`（安装时自动包含）。

## 安装与开始

```sh
git clone https://github.com/Zxxx-dev/USTCMAILCLI.git
cd USTCMAILCLI
python -m pip install .
ustc-mail --help
ustc-mail --account user@mail.ustc.edu.cn list --limit 10
```

没有默认账号。通过全局参数 `--account` 或环境变量 `USTC_MAIL_ACCOUNT` 提供完整科大邮箱地址；参数放在子命令之前，优先于环境变量。

服务器为 `mail.ustc.edu.cn`，IMAP TLS 993、SMTP TLS 465。请在网页版邮箱开启客户端访问并生成**客户端专用密码**，参见[官方邮箱帮助](https://mail.ustc.edu.cn/coremail/help/index.jsp)。

Windows 可初始化并复用凭据：

```sh
ustc-mail --account user@mail.ustc.edu.cn auth login
ustc-mail --account user@mail.ustc.edu.cn auth status
```

`auth login` 在交互终端隐藏输入密码，先验证 SMTP 登录，成功后保存到当前 Windows 用户的凭据管理器，不发送邮件。`auth forget` 移除该账号的本机凭据，不撤销服务器密码。其他系统可在联网命令运行时隐藏输入密码，或通过临时环境变量 `USTC_MAIL_PASSWORD` 提供；密码读取顺序为环境变量、Windows 凭据、交互输入。勿把密码写入仓库。

## 查看邮件

```sh
ustc-mail --account user@mail.ustc.edu.cn folders
ustc-mail --account user@mail.ustc.edu.cn read 123 --folder INBOX
```

UID 来自 `list`，仅在对应文件夹内有效。读取使用只读选择与 `BODY.PEEK`。草稿箱使用 `folders` 返回的实际编码名和 `\Drafts` 标志，不猜测英文名称。

## 预览、草稿与发送

自行创建 `mail.json` 和 UTF-8 正文文件 `body.txt`：

```json
{
  "to": "recipient@example.com",
  "cc": [],
  "subject": "邮件主题",
  "body_file": "body.txt",
  "attachments": []
}
```

可选 `html_file` 指向 HTML 正文片段；`attachments` 填文件路径数组。文件路径相对 JSON 所在目录，也支持绝对路径。

```sh
ustc-mail --account user@mail.ustc.edu.cn preview mail.json --output preview.html
ustc-mail --account user@mail.ustc.edu.cn draft mail.json --folder Drafts
ustc-mail --account user@mail.ustc.edu.cn send mail.json --confirm-sha256 "预览输出的完整64位指纹"
```

预览不联网，已有预览文件不覆盖。指纹覆盖收发件地址、主题、正文和附件；修改后须重新预览。草稿新增后回读核验，失败时先查草稿箱，避免重复新增。发送须由操作员核对具体内容并传入指纹，不自动重试。

发送记录位于用户主目录的 `.ustc-mail/receipts/`，同内容已有记录时阻止再次发送。记录包含地址、主题、Message-ID 和状态，不含密码或正文。`attempting` 表示结果未确定；`smtp_accepted` 只表示 SMTP 接受；`partially_refused` 表示部分拒收，其他地址可能已接受。结果不确定时先人工核验。去重仅覆盖本 CLI 的本地记录，不覆盖其他客户端；工具不自动归档已发送副本。

## 离线验证

```sh
python -m unittest -v
```

默认测试模拟服务器与凭据，并阻止网络连接；覆盖 MIME、内容指纹、输入校验、预览、只读读取、草稿回读、发送确认、重复阻止、发送异常及账号配置。Windows 凭据生命周期测试默认跳过；仅显式设置 `USTC_MAIL_TEST_CREDENTIALS=1` 时使用随机测试账号验证本机存储与跨进程复用。

真实邮箱登录、IMAP 兼容性、草稿显示、实际投递和服务器归档行为需使用者自行验证。MIT License，见 [LICENSE](LICENSE)。
