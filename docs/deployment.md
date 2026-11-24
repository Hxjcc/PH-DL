# Docker Compose 部署

## 首次从 GitHub 部署

在 VPS 上准备 Git、Docker Engine 和支持 `--wait` 的 Docker Compose 插件。把示例地址改成你的仓库地址。

```sh
git clone https://github.com/YOUR_ACCOUNT/PH-DL.git
cd PH-DL
install -m 600 .env.example .env
```

私有仓库可在 VPS 配好 GitHub SSH 访问后，使用 `git@github.com:YOUR_ACCOUNT/PH-DL.git` 拉取。

在 `.env` 填写强密码 `PHDL_ADMIN_PASSWORD`。通过 HTTPS 域名访问时，把 `PHDL_COOKIE_SECURE` 设为 `true`。
前端默认监听宿主机的 `127.0.0.1:8080`，地址和端口可分别通过 `PHDL_BIND_IP` 和 `PHDL_PORT` 调整。
FastAPI 的 `/docs` 默认关闭，调试时可通过 `PHDL_ENABLE_DOCS=true` 临时启用。

使用已有的 rclone 远端时，把 `RCLONE_CONFIG_PATH` 设为宿主机上 `rclone.conf` 所在的目录。
默认使用项目的 `data/rclone/`，容器可在该目录保存刷新的 token。

```sh
docker compose config --quiet
docker compose up --build --wait
docker compose ps
curl -fsS http://127.0.0.1:8080/api/health
```

健康接口返回 `"status":"ok"` 表示前端可以连到后端。Compose 将项目的 `data/` 挂载到后端 `/data`，用于存放数据库、下载、断点和滚动日志。`.env` 和 `data/` 均已加入 Git 忽略规则。

## 接入 OpenResty

在 OpenResty 的 `http` 块或该块加载的配置文件中定义 WebSocket 的连接头。[Nginx WebSocket 代理文档](https://nginx.org/en/docs/http/websocket.html)

```nginx
map $http_upgrade $phdl_connection_upgrade {
    default upgrade;
    '' close;
}
```

用下方配置设置 PH-DL 域名的 HTTPS `server` 块中的 `location /`。如果改了 `PHDL_PORT`，同时修改反代目标的端口。

```nginx
location / {
    proxy_pass http://127.0.0.1:8080;
    proxy_http_version 1.1;
    proxy_set_header Host $http_host;
    proxy_set_header X-Forwarded-Host $http_host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection $phdl_connection_upgrade;
    proxy_read_timeout 3600s;
    proxy_buffering off;
    client_max_body_size 20m;
}
```

上面的地址适用于 OpenResty 跑在宿主机或使用 Docker `host` 网络的情况。如果 OpenResty 使用独立的 Docker bridge 网络，在它的容器配置中加入 PH-DL 所在的网络，并将上游改为 `http://phdl-frontend:80`。

```sh
docker inspect --format '{{range $name, $network := .NetworkSettings.Networks}}{{println $name}}{{end}}' phdl-frontend
```

检查 OpenResty 配置并重载后，通过域名打开面板。也可以用 `curl -fsS https://phdl.example.com/api/health` 检查反代，记得替换示例域名。

## 升级和备份

```sh
git pull --ff-only
docker compose up --build --wait
```

如果 OpenResty 通过容器名连接前端，重建前端后重载 OpenResty 以刷新上游地址。

仅运行一个后端实例，因为定时调度和任务队列由该进程管理。
启动新版本时，数据库会按 schema 版本自动迁移。
做文件级备份时，先停后端，再备份 `.env`、整个 `data/` 以及手工指定的外部 rclone 配置目录。运行中的数据库请用 SQLite 的备份功能。
