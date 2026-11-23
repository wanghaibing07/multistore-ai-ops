from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Sequence

from .config import ConfigError, load_config
from .engine import build_alert_dispatcher, open_engine
from .exporter import export_multistore_csv
from .storage import DualDatabaseStore


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _resolve_dry_run_flag(config_dry_run: bool, live: bool, confirm_spend: bool, force_dry_run: bool) -> bool:
    if force_dry_run:
        return True
    if live:
        if not confirm_spend:
            raise ConfigError("实盘模式必须同时传入 --live 和 --i-confirm-real-spend（防止误扣费）")
        return False
    if not config_dry_run:
        if not confirm_spend:
            raise ConfigError(
                "配置文件中 runtime.dry_run=false；为防止误扣费，命令行运行实盘时必须显式传入 --i-confirm-real-spend，或加 --dry-run 演练"
            )
        return False
    return True


def _print_actions(actions: Sequence[object]) -> None:
    if not actions:
        print("本轮无动作。")
        return
    for action in actions:
        mode_tag = "DRY-RUN" if getattr(action, "dry_run", True) else "LIVE"
        print(f"[{mode_tag}] [{getattr(action, 'sku_id', '-')}] {getattr(action, 'action', '')}: {getattr(action, 'detail', '')}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="plan1-restock",
        description="方案 1：16688 自营卡密秒发 + Canboso 低水位自动补库（默认 dry-run 演练）",
    )
    parser.add_argument(
        "-c",
        "--config",
        default="config.local.yaml",
        help="配置文件路径（默认 config.local.yaml）",
    )
    parser.add_argument("--env-file", default=None, help="可选 .env 文件路径（默认读取配置同目录 .env）")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("check", help="只读巡检：核对 16688 余额/商品、Canboso 余额/现货及 SKU 映射兼容性")

    cat_parser = subparsers.add_parser("catalog", help="列出 Canboso 上游成品号目录（只读），便于挑选 product_id")
    cat_parser.add_argument("--supplier", default="", help="只看指定供货商名（留空则遍历全部已配置供货商）")
    cat_parser.add_argument("--keyword", default="", help="按商品名称关键词过滤")
    cat_parser.add_argument("--all-types", action="store_true", help="同时显示非 account 类型商品（标记为方案 1 不兼容）")
    cat_parser.add_argument("--in-stock", action="store_true", help="仅显示当前有货商品")
    cat_parser.add_argument("--json", action="store_true", dest="as_json", help="以 JSON 输出")

    sync_parser = subparsers.add_parser("sync-once", help="执行单轮补库/调价/订单与投诉同步（默认 dry-run）")
    sync_parser.add_argument("--dry-run", action="store_true", help="强制演练模式（零扣费、不改店铺）")
    sync_parser.add_argument("--live", action="store_true", help="开启实盘模式（需配合 --i-confirm-real-spend）")
    sync_parser.add_argument("--i-confirm-real-spend", action="store_true", help="二次确认允许真实调用 Canboso 下单扣款")

    daemon_parser = subparsers.add_parser("daemon", help="常驻定时轮询（默认 dry-run）")
    daemon_parser.add_argument("--dry-run", action="store_true", help="强制演练模式")
    daemon_parser.add_argument("--live", action="store_true", help="开启实盘模式（需配合 --i-confirm-real-spend）")
    daemon_parser.add_argument("--i-confirm-real-spend", action="store_true", help="二次确认允许真实下单扣款")
    daemon_parser.add_argument("--max-cycles", type=int, default=0, help="最多运行轮数（0 表示持续运行直到 Ctrl+C）")

    rec_parser = subparsers.add_parser("recover", help="仅执行异常恢复（保险库待补传卡密 + 网络超时幂等重放）")
    rec_parser.add_argument("--dry-run", action="store_true", help="强制演练模式")
    rec_parser.add_argument("--live", action="store_true", help="开启实盘恢复（需配合 --i-confirm-real-spend）")
    rec_parser.add_argument("--i-confirm-real-spend", action="store_true", help="二次确认允许真实写入/重放")

    subparsers.add_parser("status", help="查看本地主账本与卡密保险库汇总状态（不输出任何明文卡密）")

    alert_parser = subparsers.add_parser("test-alert", help="立即发送一条测试告警（验证 QQ 邮箱 SMTP / Webhook，零扣费）")
    alert_parser.add_argument(
        "--message",
        default="这是一条来自 16688 自动补库系统（方案 1）的测试告警邮件，收到说明邮箱告警已跑通。",
        help="测试告警正文内容",
    )
    alert_parser.add_argument(
        "--level",
        default="WARNING",
        choices=("INFO", "WARNING", "ERROR", "CRITICAL"),
        help="测试告警级别（默认 WARNING）",
    )

    exp_parser = subparsers.add_parser("export-csv", help="导出兼容 multistore-ai-ops 的采购对账 CSV（零卡密明文）")
    exp_parser.add_argument(
        "-o",
        "--output",
        default="exports/canboso_restock_ledger.csv",
        help="输出 CSV 路径",
    )

    boot_parser = subparsers.add_parser("bootstrap-goods", help="在 16688 预览或新建自营卡密商品（默认仅预览）")
    boot_parser.add_argument("--name", required=True, help="商品标题（最长 255 字符）")
    boot_parser.add_argument("--price", required=True, help="人民币售价（>= 0.02）")
    boot_parser.add_argument("--cost-price", default="", help="可选成本价（人民币）")
    boot_parser.add_argument("--category-no", default="", help="可选 16688 店内分类编号（如 C365194）")
    boot_parser.add_argument("--description", default="", help="可选商品说明")
    boot_parser.add_argument("--instruction", default="", help="可选卡密使用说明")
    boot_parser.add_argument("--image", default="", help="可选封面 URL（留空则自动调用 /openApi/goods/cover 生成）")
    boot_parser.add_argument("--cover-text", default="", help="自动生成封面时的展示文案（默认取标题）")
    boot_parser.add_argument("--warning-stock", type=int, default=5, help="库存预警数量（默认 5）")
    boot_parser.add_argument("--execute", action="store_true", help="确认真实调用 /openApi/goods/create 创建商品")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    _configure_logging(args.verbose)

    try:
        config = load_config(args.config, env_file=args.env_file)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2

    if args.command == "status":
        with DualDatabaseStore(config.runtime.ledger_db_path, config.runtime.secrets_vault_db_path) as store:
            print(json.dumps(store.summary(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "test-alert":
        with DualDatabaseStore(config.runtime.ledger_db_path, config.runtime.secrets_vault_db_path) as store:
            alerts = build_alert_dispatcher(config, store)
            try:
                result = alerts.send_test_alert(
                    message=args.message,
                    level=args.level,
                    detail={"source": "cli_test_alert", "config": str(Path(args.config).name)},
                )
            except Exception as exc:
                print(f"告警发送失败：{exc}", file=sys.stderr)
                return 1
            finally:
                alerts.close()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.command == "export-csv":
        output_path = Path(args.output)
        if not output_path.is_absolute():
            output_path = (config.base_dir / output_path).resolve()
        with DualDatabaseStore(config.runtime.ledger_db_path, config.runtime.secrets_vault_db_path) as store:
            count = export_multistore_csv(store=store, suppliers=config.canboso.suppliers, output_path=output_path)
        print(f"已导出 {count} 条采购对账记录至：{output_path}")
        return 0

    if args.command == "catalog":
        with open_engine(config, require_16688=False) as engine:
            target_suppliers = [args.supplier] if args.supplier else list(config.canboso.suppliers.keys())
            rows = []
            keyword_lower = args.keyword.strip().lower()
            for supplier in target_suppliers:
                if supplier not in config.canboso.suppliers:
                    print(f"未知供货商：{supplier}", file=sys.stderr)
                    return 2
                products = engine.client_canboso.list_products(supplier)
                for product in products:
                    if not args.all_types and not product.is_plan1_compatible:
                        continue
                    if args.in_stock and not product.is_available:
                        continue
                    if keyword_lower and keyword_lower not in product.name.lower() and keyword_lower not in product.product_id.lower():
                        continue
                    rows.append(
                        {
                            "supplier": supplier,
                            "product_id": product.product_id,
                            "product_type": product.product_type,
                            "plan1_compatible": product.is_plan1_compatible,
                            "price_text": product.price_text,
                            "price_amount": str(product.price_amount),
                            "price_currency": product.price_currency,
                            "available": product.available,
                            "name": product.name,
                        }
                    )
            if args.as_json:
                print(json.dumps(rows, ensure_ascii=False, indent=2))
            else:
                if not rows:
                    print("未匹配到符合条件的 Canboso 商品。")
                for row in rows:
                    compat = "OK" if row["plan1_compatible"] else "NO-PLAN1"
                    avail_text = "不限/人工" if row["available"] is None else str(row["available"])
                    print(
                        f"[{row['supplier']}] [{compat}] id={row['product_id']} type={row['product_type']} "
                        f"price={row['price_text']} avail={avail_text} | {row['name']}"
                    )
        return 0

    if args.command == "check":
        with open_engine(config, require_16688=True) as engine:
            report = engine.health_check()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    if args.command == "bootstrap-goods":
        price_cny = Decimal(str(args.price))
        cost_cny = Decimal(str(args.cost_price)) if args.cost_price else None
        with open_engine(config, require_16688=bool(args.execute)) as engine:
            result = engine.bootstrap_goods(
                name=args.name,
                price_cny=price_cny,
                goods_category_no=args.category_no,
                description=args.description,
                instruction=args.instruction,
                image=args.image,
                cover_text=args.cover_text,
                cost_price_cny=cost_cny,
                warning_stock_quantity=args.warning_stock,
                dry_run=not args.execute,
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.command in ("sync-once", "daemon", "recover"):
        try:
            effective_dry_run = _resolve_dry_run_flag(
                config_dry_run=config.runtime.dry_run,
                live=args.live,
                confirm_spend=args.i_confirm_real_spend,
                force_dry_run=args.dry_run,
            )
        except ConfigError as exc:
            print(f"安全拦截：{exc}", file=sys.stderr)
            return 2

        with open_engine(config, require_16688=True) as engine:
            if args.command == "recover":
                actions = engine.recover_pending(dry_run=effective_dry_run)
                _print_actions(actions)
                return 0
            if args.command == "sync-once":
                actions = engine.run_cycle(dry_run_override=effective_dry_run)
                _print_actions(actions)
                return 0

            cycles = 0
            interval = config.runtime.poll_interval_seconds
            print(f"启动方案 1 轮询守护（dry_run={effective_dry_run}, interval={interval}s），按 Ctrl+C 停止。")
            try:
                while True:
                    cycles += 1
                    print(f"--- Cycle #{cycles} ---")
                    actions = engine.run_cycle(dry_run_override=effective_dry_run)
                    _print_actions(actions)
                    if args.max_cycles > 0 and cycles >= args.max_cycles:
                        break
                    time.sleep(interval)
            except KeyboardInterrupt:
                print("\n收到中断信号，已安全退出。")
            return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
