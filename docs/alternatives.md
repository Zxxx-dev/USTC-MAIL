# 类似项目

检索日期：2026-10-15。以下依据各项目公开文档整理，未安装或实测，不能据此保证科大邮箱兼容性。

| 项目 | 公开定位 | 与本项目的关系 |
|---|---|---|
| [Himalaya](https://github.com/pimalaya/himalaya) | 通用邮件 CLI，支持 IMAP/SMTP 等后端；[Hermes 提供对应技能](https://github.com/NousResearch/hermes-agent/blob/main/skills/email/himalaya/SKILL.md) | 适合通用邮箱与更多后端，需配置服务商 |
| [mail-cli](https://github.com/qy527145/mail-cli) | 面向 Agent 的 Rust 邮件 CLI，含系统凭据集成和 `SKILL.md` | 同样走 CLI 加技能说明的路线，功能范围更广 |
| [imap-agent-cli](https://github.com/pseudosavant/imap-agent-cli) | 供 Agent 查询/读取 IMAP 邮箱、处理附件和保存草稿，明确不发送邮件 | 更侧重只读和草稿，发送范围不同 |

USTC MAIL 的范围是科大邮箱的固定服务器配置、Windows 凭据复用，以及预览、内容指纹确认和本地防重复发送；提供 Python 与 Windows EXE 两种运行方式。

本次没有找到足以确认“同样专为科大邮箱、功能范围相同”的现成项目；检索不是全网穷尽，也不表示不存在。未因调研加入其他项目的功能或依赖。
