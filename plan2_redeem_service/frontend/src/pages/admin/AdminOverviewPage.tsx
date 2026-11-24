import { useState } from "react";
import {
  AlertTriangle,
  ArrowUpRight,
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

  const { canboso, counts, skus } = overview;

  const priorityItems: Array<{
    key: string;
    label: string;
    value: number;
    meta: string;
    status?: CodeStatus | "";
    highlight: "idle" | "purple" | "amber" | "alert";
  }> = [
    {
      key: "unused",
      label: "待提货",
      value: counts.unused,
      meta: "码已发出，买家还没领",
      status: "unused",
      highlight: counts.unused > 0 ? "purple" : "idle",
    },
    {
      key: "waiting",
      label: "排队拉人中",
      value: counts.waiting_fulfillment,
      meta: "每 20 分钟自动查进度，通常 2-6 小时完成",
      status: "waiting_fulfillment",
      highlight: counts.waiting_fulfillment > 0 ? "amber" : "idle",
    },
    {
      key: "review",
      label: "需人工处理",
      value: counts.needs_manual_review,
      meta: "超价停单、余额不够或上游出错",
      status: "needs_manual_review",
      highlight: counts.needs_manual_review > 0 ? "alert" : "idle",
    },
    {
      key: "overdue",
      label: "超 24 小时未完",
      value: counts.overdue_24h,
      meta: "点这里排查超时排队单",
      status: "waiting_fulfillment",
      highlight: counts.overdue_24h > 0 ? "amber" : "idle",
    },
  ];

  const summaryItems: Array<{
    key: string;
    label: string;
    value: number;
    meta: string;
    status?: CodeStatus | "";
    highlight: "idle" | "amber" | "success";
  }> = [
    {
      key: "total",
      label: "总发码量",
      value: counts.total,
      meta: "淘宝 + 16688 全部订单",
      status: "",
      highlight: "idle",
    },
    {
      key: "completed",
      label: "已完成",
      value: counts.completed,
      meta: "共享已拉群，独享已交保修",
      status: "completed",
      highlight: counts.completed > 0 ? "success" : "idle",
    },
    {
      key: "warranty",
      label: "待交保修 / 待报备",
      value: counts.delivered_pending_warranty,
      meta: "独享已发商店邮箱，等买家交保修",
      status: "delivered_pending_warranty",
      highlight: counts.delivered_pending_warranty > 0 ? "amber" : "idle",
    },
    {
      key: "revoked",
      label: "已作废",
      value: counts.revoked,
      meta: "退款或手动作废，买家不能再领",
      status: "revoked",
      highlight: "idle",
    },
  ];

  return (
    <div className="admin-overview-flow">
      <header className="admin-header admin-header--minimal">
        <div className="admin-header-copy">
          <h1 className="admin-page-title">总览</h1>
          <p className="admin-page-sub">
            实时监控 Canboso 余额、双规格进价库存，每 20 分钟自动查排队进度。
          </p>
        </div>
        <div className="row">
          <button
            type="button"
            className={`btn btn-secondary admin-refresh-btn ${refreshing ? "is-loading" : ""}`}
            disabled={refreshing}
            aria-busy={refreshing}
            onClick={handleRefresh}
          >
            {refreshing ? <Loader2 size={15} className="spin" /> : <RefreshCw size={15} />}
            <span>刷新余额与进价</span>
          </button>
        </div>
      </header>

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
        <article className="sku-monitor-card sku-monitor-card--balance">
          <div className="row-between">
            <div className="row" style={{ gap: 8 }}>
              <span className="ops-icon-badge" aria-hidden="true">
                <Wallet size={15} />
              </span>
              <h2 className="card-title" style={{ fontSize: 15 }}>
                Canboso 账户余额
              </h2>
            </div>
            <span className={`badge ${canboso.is_low_balance ? "badge-danger" : "badge-success"}`}>
              {canboso.is_low_balance ? "余额偏低" : "余额充足"}
            </span>
          </div>

          <div
            className="kpi-value ops-balance-figure"
            style={{ color: canboso.is_low_balance ? "var(--down)" : "var(--ink-1)" }}
          >
            {formatUsd(canboso.balance_usd)}
          </div>

          <div className="ops-meta-list">
            <div className="ops-meta-row">
              <span className="ops-meta-key">低余额提醒线</span>
              <span className="mono tabular ops-meta-val">
                {formatUsd(canboso.low_balance_threshold_usd)}
              </span>
            </div>
            <div className="ops-meta-row">
              <span className="ops-meta-key">上次检查时间</span>
              <span className="mono tabular ops-meta-val">{formatDateTime(canboso.checked_at)}</span>
            </div>
            <div className="ops-meta-row">
              <span className="ops-meta-key">买家提货网址</span>
              <span
                className="mono ops-meta-val ops-meta-url"
                title={overview.public_base_url}
              >
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

              <div className="ops-price-row">
                <div>
                  <div className="ops-price-label">当前进价</div>
                  <span className="kpi-value ops-price-figure">
                    {formatUsd(sku.current_price_usd)}
                  </span>
                </div>
                <span className="ops-price-cap">
                  最高进价上限 <strong className="mono tabular">{formatUsd(sku.max_cost_usd)}</strong>
                </span>
              </div>

              <div className="price-bar-track" aria-hidden="true">
                <div
                  className={`price-bar-fill ${sku.price_exceeded ? "is-danger" : ""}`}
                  style={{ width: `${ratio}%` }}
                />
              </div>

              <div className="ops-meta-list">
                <div className="ops-meta-row">
                  <span className="ops-meta-key">上游库存</span>
                  <span className="mono tabular ops-meta-val">
                    {sku.available === null ? "充足（不限量）" : `${sku.available} 件`}
                  </span>
                </div>
                <div className="ops-meta-row">
                  <span className="ops-meta-key">未提货码余量</span>
                  <span className="mono tabular ops-meta-val">
                    淘宝 {sku.unused_codes_taobao} · 16688 {sku.unused_codes_16688}
                  </span>
                </div>
                <div className="ops-meta-row">
                  <span className="ops-meta-key">16688 商品编号</span>
                  <button
                    type="button"
                    className="ops-inline-link"
                    onClick={onJumpToSettings}
                  >
                    <span className="mono tabular">{sku.goods_no || "还没填 · 去设置"}</span>
                  </button>
                </div>
              </div>
            </article>
          );
        })}
      </section>

      <section className="ops-orders-section" aria-label="订单状态统计">
        <div className="admin-section-bar">
          <h2 className="admin-section-title">订单处理进度</h2>
          <span className="admin-section-hint">
            <Clock size={13} aria-hidden="true" />
            <span>每 20 分钟自动查进度 · 点卡片看明细</span>
          </span>
        </div>

        <div className="kpi-priority-grid" aria-label="重点待办订单">
          {priorityItems.map((item) => (
            <button
              key={item.key}
              type="button"
              className={`kpi-tile kpi-priority-card ${
                item.highlight === "alert"
                  ? "kpi-tile-alert"
                  : item.highlight === "amber"
                  ? "kpi-tile-warn"
                  : item.highlight === "purple"
                  ? "kpi-tile-accent"
                  : ""
              }`}
              onClick={() => onJumpToOrders(item.status)}
            >
              <div className="kpi-card-top">
                <span className="kpi-label">{item.label}</span>
                <ArrowUpRight size={14} className="kpi-arrow" aria-hidden="true" />
              </div>
              <span className="kpi-value tabular">{item.value}</span>
              <span className="kpi-meta">{item.meta}</span>
            </button>
          ))}
        </div>

        <div className="kpi-summary-strip" aria-label="订单汇总胶囊">
          {summaryItems.map((item) => (
            <button
              key={item.key}
              type="button"
              title={item.meta}
              className={`kpi-summary-pill ${
                item.highlight === "amber"
                  ? "is-amber"
                  : item.highlight === "success"
                  ? "is-success"
                  : ""
              }`}
              onClick={() => onJumpToOrders(item.status)}
            >
              <span className="kpi-summary-label">{item.label}</span>
              <span className="kpi-summary-value tabular">{item.value}</span>
            </button>
          ))}
        </div>
      </section>
    </div>
  );
}
