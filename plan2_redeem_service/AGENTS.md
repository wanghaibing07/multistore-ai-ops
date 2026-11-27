# Plan 2 云端默认运维约定

本目录（`plan2_redeem_service/`）对应的生产服务固定运行在云端主机，而非本机：

- **默认目标主机**：SSH 别名 `plan2-prod`（由本机 `~/.ssh/config` 解析；禁止把真实 IP 或任何密钥写入仓库）。
- **云端目录与服务**：
  - 应用目录：`/opt/plan2_redeem_service`（属主 `plan2:plan2`，非 Git 仓库）
  - Python 解释器：`/opt/plan2_redeem_service/.venv/bin/python`
  - 数据库：`/opt/plan2_redeem_service/data/redeem.sqlite3`
  - 服务进程：`plan2-redeem.service`（`caddy.service` 反向代理）
- **默认操作原则**：
  - 排查线上报错、查询真实兑换码/批次/订单、查看服务日志、重启或验证线上行为，一律默认直连 `plan2-prod`，不要检查或启动本地 `127.0.0.1:8000`（除非用户明确要求本地 `dry_run`）。
  - 标准化云端运维与部署脚本位于仓库根目录 `scripts/plan2-cloud.ps1`：
    - 查看服务状态与健康：`pwsh -File ..\scripts\plan2-cloud.ps1 -Action status`
    - 健康接口验收：`pwsh -File ..\scripts\plan2-cloud.ps1 -Action health`
    - 查看最近日志：`pwsh -File ..\scripts\plan2-cloud.ps1 -Action logs -Lines 80`
    - 在线 SQLite 备份与代码快照：`pwsh -File ..\scripts\plan2-cloud.ps1 -Action backup -KeepBackups 10`
    - 同步代码/前端 `dist` 并重启验证（自动检查 `purchasing` 排空并先备份）：`pwsh -File ..\scripts\plan2-cloud.ps1 -Action deploy -DrainWaitSeconds 20`（仅确认无风险时可加 `-Force`）
    - 快速回滚应用代码与前端 `dist`（不覆盖生产 `redeem.sqlite3`）：`pwsh -File ..\scripts\plan2-cloud.ps1 -Action rollback [-BackupName <YYYYMMDDTHHMMSSZ>]`
  - **管理后台与 16688 降级核验**：阿奇索/16688 库存补卡、SKU 映射、拉单补发与事件流统一走管理后台「渠道自动化」（`/admin/automation`）；若 16688 OpenAPI 暂停或返回 `503`/`504` 触发降级放行待补验，须在订单抽屉核实买家收件邮箱（含 `slot` 改邮箱场景）与上游实单状态，涉及 16688 争议码时按护栏先人工核验，避免重复扣款。
  - 本地改动须先通过本机 `pytest`（若改前端还需 `typecheck` + `build`），再同步云端并完成 Git 提交与推送。
