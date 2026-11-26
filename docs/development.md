# 本地开发

推荐使用 Python 3.12、Node.js 22 和 npm。后端的最低 Python 版本见 `backend/pyproject.toml`。

外部命令行工具（与 Docker 镜像一致）：
- **FFmpeg**（`ffmpeg` 加入 `PATH`）：下载 xHamster HLS 视频并进行无损 MP4 封装时必需；仅下载 PornHub MP4 时可省略。
- **rclone**（`rclone` 加入 `PATH`，或在设置中指定 `rclone_bin` 路径）：启用自动云端上传时必需；仅本地下载可省略。

## 安装依赖（Windows PowerShell）

从项目根目录执行：

```powershell
uv venv --python 3.12 backend/.venv
uv pip install --python backend/.venv/Scripts/python.exe -e "./backend[dev]"
npm.cmd --prefix frontend ci
```

不使用 `uv` 时，可以改用 `py -3.12 -m venv backend/.venv`，
再执行 `backend/.venv/Scripts/python.exe -m pip install -e "./backend[dev]"`。

## 启动服务

分别在两个终端运行：

```powershell
.\scripts\dev.ps1 backend
```

```powershell
.\scripts\dev.ps1 frontend
```

打开 `http://127.0.0.1:5173`。开发前端会把 `/api` 和 `/api/ws` 转发到本地 `8000`。
后端默认不自动重启，以免写代码时中断下载；只在调试空闲实例时可加 `-Reload`。
本地后端自动读取仓库根目录 `.env`、`backend/.env` 或终端环境变量。使用 `PHDL_ADMIN_PASSWORD` 可以覆盖本地面板密码。

macOS／Linux 从 `backend/` 创建虚拟环境并安装 `.[dev]` 后，可用
`.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000` 启动；
前端在 `frontend/` 运行 `npm ci` 和 `npm run dev`。

## 测试和构建

```powershell
Push-Location backend
.\.venv\Scripts\python.exe -m pytest -q
Pop-Location
npm.cmd --prefix frontend test
npm.cmd --prefix frontend run build
```

后端测试使用临时数据库和样例文件。构建输出写入 `frontend/dist/`。

## 本机数据

- `data/` 是运行数据目录，包含 `phdl.db`、`downloads/`、`temp/` 和服务日志。开发默认读写这个目录，也可在启动后端前通过 `PHDL_DATA_DIR` 指定隔离目录。
- `.local/` 用于本地调试产物和归档，不参与发布。`backend/.venv/`、`frontend/node_modules/` 和 `frontend/dist/` 都是可重新生成的目录。
- 同一份数据只运行一个后端。移动运行数据前应先停止服务；要备份运行中的 SQLite 数据库，请使用 SQLite 的备份功能。
