# Command Tray

一个用于管理 `ssh -L` 本地端口转发和其他长时间运行命令的小型 Windows 桌面工具。每条配置都可以在界面里像开关一样启动或停止。

## 常用场景

Command Tray 适合管理那些需要长期运行、但不想一直占着终端窗口的命令。程序可以最小化到系统托盘，命令异常退出时会显示状态并保留日志。

### 访问远程 Jupyter、Web 服务或模型服务

服务器上的服务通常只监听 `127.0.0.1`，本机浏览器不能直接访问。可以把远程端口映射到本地：

```powershell
ssh -N -o ExitOnForwardFailure=yes -L 8888:127.0.0.1:8888 user@server
```

启动后在本机打开 `http://127.0.0.1:8888`。这类配置适合远程 Jupyter Notebook、TensorBoard、Gradio、FastAPI、模型推理服务等。

### 访问远程数据库或内网服务

把远程服务器或内网机器上的数据库端口映射到本地，再用本机客户端连接：

```powershell
ssh -N -o ExitOnForwardFailure=yes -L 5432:127.0.0.1:5432 user@db-server
ssh -N -o ExitOnForwardFailure=yes -L 6379:127.0.0.1:6379 user@redis-server
ssh -N -o ExitOnForwardFailure=yes -L 3306:127.0.0.1:3306 user@mysql-server
```

本机工具里连接 `127.0.0.1:5432`、`127.0.0.1:6379` 或 `127.0.0.1:3306` 即可。

### 保持反向端口转发

如果本机服务需要临时暴露给远程服务器，可以使用 `ssh -R`：

```powershell
ssh -N -o ExitOnForwardFailure=yes -R 9000:127.0.0.1:9000 user@server
```

适合调试 webhook、本地 API 回调、临时演示服务等。

### 管理本地开发服务

可以把常用开发命令放进 Command Tray，避免打开多个终端窗口：

```powershell
npm run dev
python -m http.server 8000
uvicorn app:app --host 127.0.0.1 --port 8000 --reload
streamlit run app.py
```

每个服务都可以单独开启、关闭、查看日志。

### 启动本地后台脚本或代理

适合启动一些不需要交互输入、但需要一直运行的小脚本：

```powershell
python .\sync_files.py
python .\worker.py
cloudflared tunnel run my-tunnel
ping 127.0.0.1 -t
```

如果脚本自己退出并返回非 0 退出码，界面会标记为异常退出。

### 开机自动恢复工作环境

可以在界面里勾选“开机自启动”，让 Command Tray 随 Windows 登录启动并隐藏到托盘。再给具体命令勾选“启动程序时自动开启”，就能在开机后自动恢复常用 SSH 隧道或本地服务。

## 运行

需要本机已安装 Python 3，并且 `ssh` 命令可在系统 PATH 中直接运行。

可选安装 `pywin32` 以使用更稳定的 Windows 托盘支持；未安装时程序会回退到内置 `ctypes` 实现：

```powershell
python -m pip install -r requirements.txt
```

```powershell
python .\main.pyw
```

也可以直接双击 `main.pyw`。

## 打包 EXE

需要安装 PyInstaller：

```powershell
python -m pip install pyinstaller
```

然后运行：

```powershell
.\build.ps1
```

生成的独立可执行文件位于 `dist\CommandTray.exe`，可以直接双击运行。打包版本会在 exe 所在目录读写 `config.json`。

## 单实例和自启动

同一台电脑同一用户下只会运行一个 Command Tray。再次双击程序时，不会启动第二个进程，而是显示已经运行的主窗口。

界面右上角的“开机自启动”可以写入或移除当前用户的 Windows 启动项。启用后，程序会随登录启动并隐藏到系统托盘。

## 自动重试

每条命令都可以在新增或编辑窗口里勾选“异常退出后自动重试”，适合 SSH 隧道、远程服务端口转发、本地开发服务等容易因为网络波动中断的场景。

自动重试只会在命令启动失败或异常退出时触发。用户手动点击“关闭”、“停止全部”或退出程序时，不会继续重试。

重试间隔采用递增退避：默认 3 秒、6 秒、12 秒、24 秒、48 秒，之后最多 60 秒一次。如果命令连续运行超过 5 分钟后再异常退出，失败次数会清零，从第 1 次重试重新开始计算。

“最多重试次数”填 `0` 表示无限重试。等待重试时，状态栏会显示倒计时；该行按钮会变成：

- “重试”：立即取消等待并马上启动
- “取消”：取消等待中的自动重试

## 运行方式

每条命令都可以选择运行方式：

- `直接执行`：适合 `ssh`、`python`、`git`、`uvicorn` 这类本身就是可执行程序的命令
- `cmd`：适合依赖 `cmd` 语法的命令，例如 `dir`、`cd xxx && npm run dev`、`echo hello > a.txt`
- `PowerShell`：适合依赖 PowerShell 语法的命令，例如 `Get-ChildItem`、`$env:FOO='bar'`、管道和脚本块

如果命令只是启动一个程序，优先使用 `直接执行`。只有命令依赖 shell 语法时，才需要选择 `cmd` 或 `PowerShell`。

## 发布 Release

本项目使用 GitHub CLI 发布构建好的 exe。先安装并登录：

```powershell
winget install --id GitHub.cli
gh auth login
```

确认工作区已提交后，运行：

```powershell
.\release.ps1 -Version v0.1.0
```

脚本会重新打包、创建并推送同名 tag，然后把 `dist\CommandTray.exe` 上传到 GitHub Release。实际运行配置 `config.json` 不会被上传，避免泄露本机命令或主机信息。

## 后台运行

在 Windows 上点击窗口的关闭按钮或最小化按钮时，程序会隐藏到系统托盘，已启动的命令会继续运行。托盘图标支持：

- 双击：显示主窗口
- 右键菜单：显示窗口 / 退出

如果要真正退出程序，请从托盘右键选择“退出”。如果仍有命令运行，程序会询问是否全部停止后退出。

如果托盘初始化失败，程序会弹窗提示原因，并保持主窗口可见，不会直接退出或隐藏到找不回的位置。可以用下面的命令做托盘自检：

```powershell
python .\main.pyw --tray-smoke-test
```

## 推荐命令格式

建议隧道命令使用 `-N` 和 `ExitOnForwardFailure`：

```powershell
ssh -N -o ExitOnForwardFailure=yes -L 8080:127.0.0.1:8080 root@39.102.124.3
```

- `-N` 表示只做端口转发，不打开远程 shell。
- `-o ExitOnForwardFailure=yes` 表示端口绑定失败时让 ssh 直接退出，界面会显示异常。

## 配置

配置保存在同目录的 `config.json`：

```json
{
  "tunnels": [
    {
      "id": "example_8080",
      "name": "示例 8080",
      "command": "ssh -N -o ExitOnForwardFailure=yes -L 8080:127.0.0.1:8080 root@39.102.124.3",
      "run_mode": "direct",
      "enabled_on_start": false,
      "auto_retry": {
        "enabled": true,
        "max_attempts": 0,
        "initial_delay_seconds": 3,
        "max_delay_seconds": 60,
        "reset_after_seconds": 300
      }
    }
  ]
}
```

仓库提供了 `config.example.json` 作为示例。实际运行配置 `config.json` 会保存本机命令和主机信息，默认不会提交到 Git。

也可以直接在界面里新增、编辑、删除配置，保存后会写回这个文件。

## 其他命令

除了 `ssh`，也可以配置其他适合长期运行的命令，例如：

```powershell
python -m http.server 8000
npm run dev
ping 127.0.0.1 -t
```

界面里的“开启”会启动命令，“关闭”会终止对应进程。Windows 上如果进程没有及时退出，工具会尝试结束整个进程树。

## 异常断开提示

如果命令不是用户手动关闭，而是自己退出并返回非 0 退出码，界面状态会变成“异常退出”，日志会记录退出码。程序隐藏到托盘时，会通过托盘通知提醒。

对于 SSH 网络断开或远程主机问题，通常 `ssh` 会退出并返回非 0 退出码，因此会触发这个提示。具体原因以日志里的 `ssh` 输出为准。

## 注意

这个工具会在后台启动进程，并捕获输出作为日志。对于 SSH，建议提前配置 SSH key 或 ssh-agent；如果远程登录需要交互式输入密码，后台进程通常无法完成登录。
