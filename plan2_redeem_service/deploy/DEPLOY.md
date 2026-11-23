# Plan 2 单机 VPS 部署（Debian 12）

本部署包只启动 `plan2_redeem_service`，不是仓库根目录的店铺账本服务。FastAPI 使用单 worker，前端直接使用仓库内的 `frontend/dist`，Caddy 负责自动 HTTPS。Docker 以非 root 用户运行应用。**默认 dry_run，不会触发真实采购。**

## 前置条件

- 2 vCPU、4 GB RAM、40 GB SSD 起，Debian 12，公网 IPv4
- 已安装 Docker Engine + Docker Compose v2（可选预装 Docker 的系统镜像）
- 一个域名的 A 记录已指向服务器公网 IP
- 云防火墙和系统防火墙允许 TCP 80、443；SSH 22 仅授权管理 IP
- 私有 GitHub 仓库需要只读 Deploy Key 或其他安全 Git 认证，不要把 PAT 放进命令或仓库

## 启动

```bash
git clone git@github.com:wanghaibing07/multistore-ai-ops.git
cd multistore-ai-ops
git switch deploy/plan2-vps  # PR 合并到 master 后可省略
cd plan2_redeem_service
cp .env.example .env
```

编辑 `.env`，至少修改：

```dotenv
PLAN2_DOMAIN=redeem.yourdomain.com
PLAN2_PUBLIC_BASE_URL=https://redeem.yourdomain.com
PLAN2_ADMIN_TOKEN=请填openssl生成的强随机Token
PLAN2_EXECUTION_MODE=dry_run
```

用 `openssl rand -hex 32` 生成 Token（不要提交或发到聊天里）。`PLAN2_DOMAIN` 必须与 DNS 的实际域名一致。真实运行前还要配置 Canboso、16688、SMTP 相关密钥。

```bash
chmod 600 .env
docker compose up -d --build
docker compose ps
docker compose logs --tail=80 app
curl -fsS https://redeem.yourdomain.com/api/v1/health
```

检查返回 `status=ok` 且 `execution_mode=dry_run`，再在浏览器测试提货和后台。只有验证接口、邮件、价格上限及上游权限后，才手动改成 `PLAN2_EXECUTION_MODE=live` 并运行 `docker compose up -d`；切换 live 可能发生真实外部采购/发货。

## 更新与备份

```bash
git pull --ff-only
docker compose up -d --build
```

SQLite 使用 WAL，**不要在线直接复制数据库文件充当备份**。可在运行中的应用容器里通过 SQLite backup API 制作一致性备份：

```bash
docker compose exec -T app python -c "import sqlite3; s=sqlite3.connect('/data/redeem.sqlite3'); d=sqlite3.connect('/data/backup.sqlite3'); s.backup(d); d.close(); s.close()"
docker compose cp app:/data/backup.sqlite3 ./redeem-backup.sqlite3
docker compose cp app:/data/private ./private-backup
```

把数据库与私有截图备份加密保存到服务器外，定期测试恢复。不要执行 `docker compose down -v`：它会删除数据库和截图所在的持久化卷。备份副本含客户数据，不要公开上传 GitHub。

## 注意

- 公网只开放 Caddy 的 80/443，不要对外映射应用的 8000 端口
- 后台使用 Bearer Token；严格保管 `.env`、数据库、保修截图
- 项目中的 `/api/v1/health` 只证明进程可响应，不代表上游供货与 SMTP 均正常
- 上线前确认 GitHub 中的前端构建产物确实是计划发布的版本
