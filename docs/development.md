# 开发与验证

Python 3.11+，在源码目录运行：

```sh
python -m pip install .
python -m unittest -v
```

默认测试模拟服务器与凭据，并阻止网络连接；覆盖 MIME、指纹、输入校验、预览、只读读取、草稿回读、发送确认、重复阻止、异常及账号配置。Windows 凭据生命周期测试默认跳过；显式设置 `USTC_MAIL_TEST_CREDENTIALS=1` 才用随机测试账号验证本机存储和跨进程复用。

## Windows 打包

GitHub Actions 的 `Windows package` 工作流在干净的 Windows x64 环境构建 EXE、wheel 和源码包，并运行离线测试。仅手动触发，不使用邮箱账号或真实密码。构建产物检查通过后发布到 Releases。

本地也可在独立虚拟环境安装构建依赖后运行：

```sh
python -m pip install . pyinstaller==6.22.3 build
python -m PyInstaller --clean --noconfirm --onefile --console --python-option "X utf8" --name ustc-mail --hidden-import win32cred --hidden-import win32timezone --distpath dist/windows --workpath build/pyinstaller --specpath build ustc_mail.py
python scripts/smoke_exe.py dist/windows/ustc-mail.exe
python -m build
```

EXE 验证在临时目录运行，移除 Python 所在目录的 PATH，只执行帮助、缺失账号报错和离线预览；不登录邮箱或发送邮件。工作流另以随机临时账号及假密码验证 EXE 的 Windows 凭据读写和移除，结束后清理。

真实邮箱登录、IMAP 兼容性、草稿显示、实际投递和服务器归档需要使用者自行验证。默认测试不证明这些行为。
