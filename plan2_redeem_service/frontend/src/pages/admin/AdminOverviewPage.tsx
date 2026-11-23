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
import { formatDateTime, formatUsd, getSkuShortLabel } from "../../utils/format";

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
      meta: "淘宝 + 16688 全部订单",
      status: "",
    },
    {
      key: "unused",
      label: "待提货",
      value: counts.unused,
      meta: "码已发出，买家还没领",
      status: "unused",
    },
    {
      key: "waiting",
      label: "排队拉人中",
      value: counts.waiting_fulfillment,
      meta: "每 20 分钟自动查进度，一般 1 到 6 小时完成",
      status: "waiting_fulfillment",
      variant: counts.waiting_fulfillment > 0 ? "warn" : "default",
    },
    {
      key: "warranty",
      label: "待交保修 / 待报备",
      value: counts.delivered_pending_warranty,
      meta: "独享已发商店邮箱，等买家交保修",
      status: "delivered_pending_warranty",
      variant: counts.delivered_pending_warranty > 0 ? "warn" : "default",
    },
    {
      key: "completed",
      label: "已完成",
      value: counts.completed,
      meta: "共享已拉群，独享已交保修",
      status: "completed",
    },
    {
      key: "review",
      label: "需人工处理",
      value: counts.needs_manual_review,
      meta: "超价停单、余额不够或上游出错",
      status: "needs_manual_review",
      variant: counts.needs_manual_review > 0 ? "alert" : "default",
    },
    {
      key: "overdue",
      label: "超 24 小时未完",
      value: counts.overdue_24h,
      meta: "点这里排查超时排队单",
      status: "waiting_fulfillment",
      variant: counts.overdue_24h > 0 ? "alert" : "default",
    },
    {
      key: "revoked",
      label: "已作废",
      value: counts.revoked,
      meta: "退款或手动作废，买家不能再领",
      status: "revoked",
    },
  ];

  return (
    <div className="stack-lg">
      <header className="admin-header">
        <div>
          <h1 className="admin-page-title">总览</h1>
          <p className="admin-page-sub">
            查看 Canboso 余额、共享车位和独享整组的实时进价与库存，系统每 20 分钟自动查一次排队进度。
          </p>
        </div>
        <div className="row">
          <span className={`badge ${execution_mode === "live" ? "badge-warning" : "badge-accent"}`}>
            <span className="badge-dot" aria-hidden="true" />
            <span>{execution_mode === "live" ? "生产模式" : "演练模式"}</span>
          </span>
          <button
            type="button"
            className={`btn btn-secondary ${refreshing ? "is-loading" : ""}`}
            disabled={refreshing}
            aria-busy={refreshing}
            onClick={handleRefresh}
          >
            {refreshing ? <Loader2 size={15} className="spin" /> : <RefreshCw size={15} />}
            <span>刷新余额与进价</span>
          </button>
        </div>
      </header>

      {execution_mode === "dry_run" && (
        <div className="callout callout-info">
          <CheckCircle2 size={18} className="callout-icon" aria-hidden="true" />
          <div>
            当前处于 <strong>演练模式</strong>，买家提货只会模拟走流程，不会向 Canboso 发起真实扣款下单。
          </div>
        </div>
      )}

      {canboso.is_low_balance && (
        <div className="callout callout-danger" role="alert">
          <ShieldAlert size={18} className="callout-icon" aria-hidden="true" />
          <div>
            <strong>上游余额偏低：</strong>当前余额 {formatUsd(canboso.balance_usd)}，低于提醒线{" "}
            {formatUsd(canboso.low_balance_threshold_usd)}。余额不够时新单会自动转到「需人工处理」并给你发邮件。
          </div>
        </div>
      )}

      {canboso.error && (
        <div className="callout callout-warning" role="alert">
          <AlertTriangle size={18} className="callout-icon" aria-hidden="true" />
          <div>
            <strong>查上游进价失败：</strong>
            {canboso.error}
          </div>
        </div>
      )}

      <section className="ops-strip" aria-label="上游余额与双规格进价监控">
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
              <span style={{ color: "var(--ink-3)" }}>低余额提醒线</span>
              <span className="mono">{formatUsd(canboso.low_balance_threshold_usd)}</span>
            </div>
            <div className="row-between" style={{ fontSize: 13 }}>
              <span style={{ color: "var(--ink-3)" }}>上次检查时间</span>
              <span className="mono">{formatDateTime(canboso.checked_at)}</span>
            </div>
            <div className="row-between" style={{ fontSize: 13 }}>
              <span style={{ color: "var(--ink-3)" }}>买家提货网址</span>
              <span className="mono" style={{ maxWidth: 210, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {overview.public_base_url}
              </span>
            </div>
          </div>
        </article>

        {skus.map((sku) => {
          const isExclusive = sku.sku_key === "ultra_exclusive_admin";
          const ratio =
            sku.current_price_usd !== null && sku.max_cost_usd > 0
              ? Math.min(100, Math.round((sku.current_price_usd / sku.max_cost_usd) * 100))
              : 0;
          return (
            <article key={sku.sku_key} className="sku-monitor-card">
              <div className="row-between">
                <div className="row" style={{ gap: 8 }}>
                  <span
                    className={`sku-tag ${
                      isExclusive ? "sku-tag-exclusive" : "sku-tag-shared"
                    }`}
                  >
                    {getSkuShortLabel(sku.sku_key)}
                  </span>
                  <h2 className="card-title" style={{ fontSize: 14.5 }}>
                    {isExclusive ? "秒发商店邮箱" : "排队拉进家庭组"}
                  </h2>
                </div>
                <span
                  className={`badge ${
                    sku.price_exceeded
                      ? "badge-danger"
                      : !sku.is_available
                      ? "badge-warning"
                      : "badge-success"
                  }`}
                >
                  {sku.price_exceeded ? "超价自动停单" : !sku.is_available ? "上游没货" : "正常可买"}
                </span>
              </div>

              <div className="row-between" style={{ alignItems: "baseline" }}>
                <div>
                  <div style={{ fontSize: 11.5, color: "var(--ink-3)" }}>当前进价</div>
                  <span className="kpi-value" style={{ fontSize: 22 }}>
                    {formatUsd(sku.current_price_usd)}
                  </span>
                </div>
                <span style={{ fontSize: 12.5, color: "var(--ink-3)" }}>
                  最高进价上限 <strong className="mono">{formatUsd(sku.max_cost_usd)}</strong>
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
                    {sku.available === null ? "充足（不限量）" : `${sku.available} 件`}
                  </span>
                </div>
                <div className="row-between">
                  <span>未提货码余量</span>
                  <span className="mono" style={{ color: "var(--ink-1)" }}>
                    淘宝 {sku.unused_codes_taobao} · 16688 {sku.unused_codes_16688}
                  </span>
                </div>
                <div className="row-between">
                  <span>16688 商品编号</span>
                  <button
                    type="button"
                    className="btn btn-ghost btn-sm"
                    style={{ minHeight: 28, padding: "0 8px", fontSize: 12 }}
                    onClick={onJumpToSettings}
                  >
                    <span className="mono">{sku.goods_no || "还没填 · 去设置"}</span>
                  </button>
                </div>
              </div>
            </article>
          );
        })}
      </section>

      <section className="stack-sm" aria-label="订单状态统计">
        <div className="admin-section-bar">
          <h2 className="card-title" style={{ fontSize: 16 }}>
            订单处理进度
          </h2>
          <span className="row" style={{ fontSize: 12.5, color: "var(--ink-3)", gap: 6 }}>
            <Clock size={14} aria-hidden="true" />
            <span>每 20 分钟自动查进度 · 点卡片看明细</span>
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
