import { useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  Clock,
  Loader2,
  RefreshCw,
  ShieldAlert,
  Wallet,
} from "lucide-react";
import type { AdminOverviewResponse, CodeStatus } from "../../types/api";
import { formatDateTime, formatUsd } from "../../utils/format";

interface AdminOverviewPageProps {
  overview: AdminOverviewResponse;
  onRefreshUpstream: () => Promise<void>;
  onJumpToOrders: (statusFilter?: CodeStatus | "") => void;
  onJumpToSettings: () => void;
}

export function AdminOverviewPage({
  overview,
  onRefreshUpstream,
  onJumpToOrders,
  onJumpToSettings,
}: AdminOverviewPageProps) {
  const [refreshing, setRefreshing] = useState(false);

  const handleRefresh = async () => {
    setRefreshing(true);
    try {
      await onRefreshUpstream();
    } finally {
      setRefreshing(false);
    }
  };

  const { canboso, counts, skus, execution_mode } = overview;

  const kpiItems: Array<{
    key: string;
    label: string;
    value: number;
    meta: string;
    status?: CodeStatus | "";
    variant?: "default" | "alert" | "warn";
  }> = [
    {
      key: "total",
      label: "总发码量",
      value: counts.total,
      meta: "淘宝 + 16688 全部记录",
      status: "",
    },
    {
      key: "unused",
      label: "待核销",
      value: counts.unused,
      meta: "已发出尚未提货",
      status: "unused",
    },
    {
      key: "waiting",
      label: "排单拉人中",
      value: counts.waiting_fulfillment,
      meta: "预计 1 到 6 小时自动完成",
      status: "waiting_fulfillment",
      variant: counts.waiting_fulfillment > 0 ? "warn" : "default",
    },
    {
      key: "warranty",
      label: "待登记保修",
      value: counts.delivered_pending_warranty,
      meta: "独享整组已秒发号",
      status: "delivered_pending_warranty",
    },
    {
      key: "completed",
      label: "已完成",
      value: counts.completed,
      meta: "履约闭环完成",
      status: "completed",
    },
    {
      key: "review",
      label: "需人工处理",
      value: counts.needs_manual_review,
      meta: "熔断、余额不足或上游异常",
      status: "needs_manual_review",
      variant: counts.needs_manual_review > 0 ? "alert" : "default",
    },
    {
      key: "overdue",
      label: "超 24 小时未完",
      value: counts.overdue_24h,
      meta: "点此排查拉人超时单",
      status: "waiting_fulfillment",
      variant: counts.overdue_24h > 0 ? "alert" : "default",
    },
    {
      key: "revoked",
      label: "已作废",
      value: counts.revoked,
      meta: "退款或手动作废拦截",
      status: "revoked",
    },
  ];

  return (
    <div className="stack-lg">
      <header className="admin-header">
        <div>
          <h1 className="admin-page-title">履约总览与熔断监控</h1>
          <p className="admin-page-sub">
            实时查看 Canboso 余额、两款 Ultra 20x 成本与订单推进情况，点击任意统计卡片可跳转筛选明细。
          </p>
        </div>
        <div className="row">
          <span className={`badge ${execution_mode === "live" ? "badge-warning" : "badge-accent"}`}>
            <span className="badge-dot" aria-hidden="true" />
            <span>{execution_mode === "live" ? "实盘模式 · LIVE" : "演练保护 · DRY-RUN"}</span>
          </span>
          <button
            type="button"
            className={`btn btn-secondary ${refreshing ? "is-loading" : ""}`}
            disabled={refreshing}
            aria-busy={refreshing}
            onClick={handleRefresh}
          >
            {refreshing ? <Loader2 size={15} className="spin" /> : <RefreshCw size={15} />}
            <span>刷新上游行情</span>
          </button>
        </div>
      </header>

      {execution_mode === "dry_run" && (
        <div className="callout callout-info">
          <CheckCircle2 size={18} className="callout-icon" aria-hidden="true" />
          <div>
            当前后端运行在 <strong>DRY-RUN 演练保护模式</strong>，所有买家核销仅模拟走通状态机，不会向 Canboso 发起真实扣款下单。
          </div>
        </div>
      )}

      {canboso.is_low_balance && (
        <div className="callout callout-danger" role="alert">
          <ShieldAlert size={18} className="callout-icon" aria-hidden="true" />
          <div>
            <strong>上游余额低于预警线：</strong>当前余额 {formatUsd(canboso.balance_usd)}，低于阈值{" "}
            {formatUsd(canboso.low_balance_threshold_usd)}。余额不足时新单将自动转入「需人工处理」并邮件提醒你补款。
          </div>
        </div>
      )}

      {canboso.error && (
        <div className="callout callout-warning" role="alert">
          <AlertTriangle size={18} className="callout-icon" aria-hidden="true" />
          <div>
            <strong>上游行情检测异常：</strong>
            {canboso.error}
          </div>
        </div>
      )}

      <section className="ops-strip" aria-label="上游余额与 SKU 成本监控">
        <article className="sku-monitor-card">
          <div className="row-between">
            <div className="row">
              <Wallet size={17} style={{ color: "var(--accent)" }} aria-hidden="true" />
              <h2 className="card-title" style={{ fontSize: 15 }}>
                Canboso 账户余额
              </h2>
            </div>
            <span className={`badge ${canboso.is_low_balance ? "badge-danger" : "badge-success"}`}>
              {canboso.is_low_balance ? "余额偏低" : "余额充足"}
            </span>
          </div>

          <div
            className="kpi-value"
            style={{ color: canboso.is_low_balance ? "var(--down)" : "var(--ink-1)" }}
          >
            {formatUsd(canboso.balance_usd)}
          </div>

          <div className="stack-sm">
            <div className="row-between" style={{ fontSize: 13 }}>
              <span style={{ color: "var(--ink-3)" }}>低余额预警阈值</span>
              <span className="mono">{formatUsd(canboso.low_balance_threshold_usd)}</span>
            </div>
            <div className="row-between" style={{ fontSize: 13 }}>
              <span style={{ color: "var(--ink-3)" }}>最近巡检时间</span>
              <span className="mono">{formatDateTime(canboso.checked_at)}</span>
            </div>
            <div className="row-between" style={{ fontSize: 13 }}>
              <span style={{ color: "var(--ink-3)" }}>提货页基址</span>
              <span className="mono" style={{ maxWidth: 210, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {overview.public_base_url}
              </span>
            </div>
          </div>
        </article>

        {skus.map((sku) => {
          const ratio =
            sku.current_price_usd !== null && sku.max_cost_usd > 0
              ? Math.min(100, Math.round((sku.current_price_usd / sku.max_cost_usd) * 100))
              : 0;
          return (
            <article key={sku.sku_key} className="sku-monitor-card">
              <div className="row-between">
                <h2 className="card-title" style={{ fontSize: 15 }}>
                  {sku.product_type === "slot" ? "共享车位（Slot）" : "独享整组（Admin）"}
                </h2>
                <span
                  className={`badge ${
                    sku.price_exceeded
                      ? "badge-danger"
                      : !sku.is_available
                      ? "badge-warning"
                      : "badge-success"
                  }`}
                >
                  {sku.price_exceeded ? "已触发熔断" : !sku.is_available ? "上游缺货" : "正常可买"}
                </span>
              </div>

              <div className="row-between" style={{ alignItems: "baseline" }}>
                <span className="kpi-value" style={{ fontSize: 22 }}>
                  {formatUsd(sku.current_price_usd)}
                </span>
                <span style={{ fontSize: 12.5, color: "var(--ink-3)" }}>
                  熔断上限 <strong className="mono">{formatUsd(sku.max_cost_usd)}</strong>
                </span>
              </div>

              <div className="price-bar-track" aria-hidden="true">
                <div
                  className={`price-bar-fill ${sku.price_exceeded ? "is-danger" : ""}`}
                  style={{ width: `${ratio}%` }}
                />
              </div>

              <div className="stack-sm" style={{ fontSize: 12.5, color: "var(--ink-3)" }}>
                <div className="row-between">
                  <span>上游库存</span>
                  <span className="mono" style={{ color: "var(--ink-1)" }}>
                    {sku.available === null ? "不限量（可用）" : `${sku.available} 件`}
                  </span>
                </div>
                <div className="row-between">
                  <span>未核销存量</span>
                  <span className="mono" style={{ color: "var(--ink-1)" }}>
                    淘宝 {sku.unused_codes_taobao} · 16688 {sku.unused_codes_16688}
                  </span>
                </div>
                <div className="row-between">
                  <span>16688 商品号</span>
                  <button
                    type="button"
                    className="btn btn-ghost btn-sm"
                    style={{ minHeight: 28, padding: "0 8px", fontSize: 12 }}
                    onClick={onJumpToSettings}
                  >
                    <span className="mono">{sku.goods_no || "未绑定 · 去配置"}</span>
                  </button>
                </div>
              </div>
            </article>
          );
        })}
      </section>

      <section className="stack-sm" aria-label="订单状态统计">
        <div className="row-between">
          <h2 className="card-title" style={{ fontSize: 16 }}>
            订单履约进度看板
          </h2>
          <span className="row" style={{ fontSize: 12.5, color: "var(--ink-3)", gap: 6 }}>
            <Clock size={14} aria-hidden="true" />
            <span>点击任意指标进入明细列表</span>
          </span>
        </div>

        <div className="kpi-grid">
          {kpiItems.map((item) => (
            <button
              key={item.key}
              type="button"
              className={`kpi-tile ${
                item.variant === "alert"
                  ? "kpi-tile-alert"
                  : item.variant === "warn"
                  ? "kpi-tile-warn"
                  : ""
              }`}
              onClick={() => onJumpToOrders(item.status)}
            >
              <span className="kpi-label">{item.label}</span>
              <span className="kpi-value">{item.value}</span>
              <span className="kpi-meta">{item.meta}</span>
            </button>
          ))}
        </div>
      </section>
    </div>
  );
}
