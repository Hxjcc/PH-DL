# PH-DL

用于 VPS 的视频追更与按需下载面板，使用 FastAPI、SQLite 和 Vue 3。

- **自动追更**：按计划检查订阅目标，下载新视频；启用自动上传时，通过 rclone
  上传并核验远端文件，再清理本地。关闭自动上传的文件不会被追更任务补传。
- **手动下载**：按用户名、订阅、视频链接或 TXT 创建任务，查看每条视频的进度、速度和状态。
  下载可在校验源文件一致后从断点继续。
- **历史记录**：支持筛选、重新下载和 MP4 在线播放。播放通过 PH-DL 转发，不写入下载目录。

## 目录

```text
backend/
  app/              API、任务队列、下载、上传和持久化
  tests/            后端测试和本地样例
frontend/
  src/              页面、组件和客户端逻辑
  public/           静态资源
docs/               开发和部署指南
scripts/            本地开发脚本
data/               本地运行数据（不进入版本库）
docker-compose.yml  Docker Compose 部署入口
```

## 开始使用

- [本地开发](docs/development.md)：安装依赖、启动服务、运行测试和构建前端。
- [从 GitHub 拉取、Docker Compose 部署和 OpenResty 接入](docs/deployment.md)

前后端各有自己的依赖清单。生产前端由 Nginx 托管并转发 API，视频下载和播放无需 FFmpeg。
