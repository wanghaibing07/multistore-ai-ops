import { useCallback, useEffect, useState, type FormEvent, type KeyboardEvent } from "react";
import { useSearchParams } from "react-router-dom";
import { ApiError, exportAdminReconciliationCsv, listAdminCodes } from "../../api/client";
import { CastoriceIcon } from "../../components/CastoriceIcon";
import { OrderDisplayStatusBadge } from "../../components/StatusBadge";
import { useToast } from "../../components/ToastProvider";
import type {
  AdminCodeListResponse,
  AdminCodeSummary,
  AdminOrderStatusFilter,
  AdminOverviewCounts,
} from "../../types/api";
import {
  formatDateTime,
  formatOrderErrorMessage,
  formatUsd,
  getChannelLabel,
  getSkuShortLabel,
} from "../../utils/format";
import { isImeComposing } from "../../utils/ime";

interface AdminOrdersPageProps {
  token: string;
  counts?: AdminOverviewCounts;
  refreshKey: number;
  onUnauthorized: () => void;
  onSelectCode: (code: string) => void;
}

const STATUS_TABS: Array<{
  value: AdminOrderStatusFilter | "";
  label: string;
  countKey?: keyof AdminOverviewCounts;
  alertTone?: "danger" | "warning";
}> = [
  {
    value: "manual_attention",
    label: "需人工处理",
    countKey: "manual_attention",
    alertTone: "danger",
  },
  {
    value: "pending_warranty_ack",
    label: "待质保登记",
    countKey: "pending_warranty_ack",
    alertTone: "warning",
  },
  {
    value: "waiting_fulfillment",
    label: "排队拉人中",
    countKey: "waiting_fulfillment",
  },
  {
    value: "dispatched_unused",
    label: "已发出待提货",
    countKey: "dispatched_unused",
  },
  { value: "in_stock", label: "在库存", countKey: "in_stock" },
  { value: "completed", label: "已完成", countKey: "completed" },
  { value: "revoked", label: "已作废", countKey: "revoked" },
  { value: "", label: "全部" },
];

const ALLOWED_STATUS_VALUES = new Set<string>(
  STATUS_TABS.map((item) => item.value).filter(Boolean)
);

const COMMON_ERROR_CODES: Array<{ code: string; label: string }> = [
  { code: "PRICE_LIMIT_EXCEEDED", label: "进价超上限" },
  { code: "LOW_BALANCE", label: "上游余额不足" },
  { code: "UPSTREAM_OUT_OF_STOCK", label: "上游暂时没货" },
  { code: "UPSTREAM_TIMEOUT", label: "上游响应超时" },
  { code: "UPSTREAM_ERROR", label: "上游接口报错" },
  { code: "CANBOSO_ORDER_CANCELLED", label: "上游订单已取消/退款" },
  { code: "CANBOSO_PURCHASE_UNCONFIRMED", label: "上游下单未确认" },
  { code: "PURCHASING_STUCK_RECOVERY", label: "下单卡死自动恢复" },
  { code: "FULFILLMENT_TIMEOUT", label: "排队超 24 小时" },
  { code: "CHANNEL16688_OPENAPI_PAUSED", label: "16688 OpenAPI 暂停" },
  { code: "CHANNEL16688_RECIPIENT_DISPUTED", label: "16688 收件邮箱争议" },
  { code: "ORDER_NOT_ELIGIBLE", label: "16688 订单不符合条件" },
];

const ISO_DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

const PAGE_SIZE = 20;

function parseStatusParam(raw: string | null): AdminOrderStatusFilter | "" {
  const trimmed = (raw || "").trim();
  if (trimmed && ALLOWED_STATUS_VALUES.has(trimmed)) {
    return trimmed as AdminOrderStatusFilter;
  }
  return "";
}

function parseDateParam(raw: string | null): string {
  const trimmed = (raw || "").trim();
  return ISO_DATE_RE.test(trimmed) ? trimmed : "";
}

function parsePageParam(raw: string | null): number {
  const num = Number.parseInt(raw || "1", 10);
  return Number.isFinite(num) && num >= 1 ? num : 1;
}

function renderSkuTag(skuKey: AdminCodeSummary["sku_key"]) {
  const isExclusive = skuKey === "ultra_exclusive_admin";
  return (
    <span className={`sku-tag ${isExclusive ? "sku-tag-exclusive" : "sku-tag-shared"}`}>
      {getSkuShortLabel(skuKey)}
    </span>
  );
}

function renderOrderEmails(item: AdminCodeSummary) {
  const buyerEmail = item.buyer_email || item.buyer_email_masked;
  const storeEmail = item.store_email;
  const isInStock =
    item.status === "unused" &&
    !(typeof item.is_dispatched === "boolean"
      ? item.is_dispatched
      : Boolean(item.external_order_id && item.external_order_id.trim()));

  if (item.sku_key === "ultra_exclusive_admin") {
    if (!buyerEmail && !storeEmail) {
      return (
        <span className="muted" style={{ color: "var(--ink-3)", fontSize: 12.5 }}>
          {isInStock ? "在库存未发出" : "还没提货"}
        </span>
      );
    }

    return (
      <div className="email-stack">
        <div className="email-line">
          <span className="email-role-tag email-role-tag-buyer">主邮</span>
          {buyerEmail ? (
            <span className="mono">{buyerEmail}</span>
          ) : (
            <span className="muted" style={{ color: "var(--ink-3)" }}>
              待买家提交
            </span>
          )}
        </div>
        {storeEmail && (
          <div className="email-line">
            <span className="email-role-tag email-role-tag-store">商店</span>
            <span className="mono">{storeEmail}</span>
          </div>
        )}
      </div>
    );
  }

  if (!buyerEmail) {
    return (
      <span className="muted" style={{ color: "var(--ink-3)", fontSize: 12.5 }}>
        {isInStock ? "在库存未发出" : "还没提货"}
      </span>
    );
  }

  return (
    <span className="mono" style={{ fontSize: 12.5 }}>
      {buyerEmail}
    </span>
  );
}

function getTimeColumnLabel(statusFilter: AdminOrderStatusFilter | ""): string {
  if (statusFilter === "in_stock") {
    return "生成时间";
  }
  if (statusFilter === "dispatched_unused") {
    return "发出时间";
  }
  return "使用时间";
}

function renderOrderTimeCell(
  item: AdminCodeSummary,
  statusFilter: AdminOrderStatusFilter | ""
) {
  if (statusFilter === "in_stock") {
    return (
      <span className="mono" style={{ fontSize: 12.5 }}>
        {formatDateTime(item.created_at)}
      </span>
    );
  }
  if (statusFilter === "dispatched_unused") {
    return (
      <div className="stack-sm" style={{ gap: 3 }}>
        <span className="mono" style={{ fontSize: 12.5 }}>
          {formatDateTime(item.updated_at)}
        </span>
        <span style={{ fontSize: 12, color: "var(--ink-3)" }}>尚未提货</span>
      </div>
    );
  }
  if (item.submitted_at) {
    return (
      <span className="mono" style={{ fontSize: 12.5 }}>
        {formatDateTime(item.submitted_at)}
      </span>
    );
  }
  const fallbackLabel = item.status === "revoked" ? "作废" : "发出";
  return (
    <div className="stack-sm" style={{ gap: 3 }}>
      <span style={{ fontSize: 12.5, color: "var(--ink-3)" }}>未使用</span>
      <span className="mono" style={{ fontSize: 12, color: "var(--ink-3)" }}>
        {fallbackLabel} {formatDateTime(item.updated_at)}
      </span>
    </div>
  );
}

function formatMobileOrderTime(
  item: AdminCodeSummary,
  statusFilter: AdminOrderStatusFilter | ""
): string {
  if (statusFilter === "in_stock") {
    return `生成 ${formatDateTime(item.created_at)}`;
  }
  if (statusFilter === "dispatched_unused") {
    return `发出 ${formatDateTime(item.updated_at)}`;
  }
  if (item.submitted_at) {
    return `使用 ${formatDateTime(item.submitted_at)}`;
  }
  const fallbackLabel = item.status === "revoked" ? "作废" : "发出";
  return `未使用 · ${fallbackLabel} ${formatDateTime(item.updated_at)}`;
}

export function AdminOrdersPage({
  token,
  counts,
  refreshKey,
  onUnauthorized,
  onSelectCode,
}: AdminOrdersPageProps) {
  const { pushToast } = useToast();
  const [searchParams, setSearchParams] = useSearchParams();

  const statusFilter = parseStatusParam(searchParams.get("status"));
  const channelFilter = (searchParams.get("channel") || "").trim();
  const skuFilter = (searchParams.get("sku_key") || "").trim();
  const errorCodeFilter = (searchParams.get("error_code") || "").trim();
  const startDateFilter = parseDateParam(searchParams.get("start_date"));
  const endDateFilter = parseDateParam(searchParams.get("end_date"));
  const searchQuery = (searchParams.get("q") || "").trim();
  const page = parsePageParam(searchParams.get("page"));

  const [searchDraft, setSearchDraft] = useState(searchQuery);
  const [errorCodeDraft, setErrorCodeDraft] = useState(errorCodeFilter);
  const [excludeInactiveExport, setExcludeInactiveExport] = useState(false);
  const [exportingCsv, setExportingCsv] = useState(false);

  const [data, setData] = useState<AdminCodeListResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setSearchDraft(searchQuery);
  }, [searchQuery]);

  useEffect(() => {
    setErrorCodeDraft(errorCodeFilter);
  }, [errorCodeFilter]);

  const dateRangeInvalid = Boolean(
    startDateFilter && endDateFilter && startDateFilter > endDateFilter
  );

  const updateParams = useCallback(
    (patch: Record<string, string | number | null | undefined>, resetPage = true) => {
      const next = new URLSearchParams(searchParams);
      for (const [key, value] of Object.entries(patch)) {
        if (value === null || value === undefined || value === "") {
          next.delete(key);
        } else {
          next.set(key, String(value));
        }
      }
      if (resetPage && !("page" in patch)) {
        next.delete("page");
      } else if (next.get("page") === "1") {
        next.delete("page");
      }
      setSearchParams(next, { replace: true });
    },
    [searchParams, setSearchParams]
  );

  const fetchList = useCallback(async () => {
    if (dateRangeInvalid) {
      setLoading(false);
      setError("开始日期不能晚于结束日期，请调整后再查。");
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const result = await listAdminCodes(token, {
        status: statusFilter || undefined,
        channel: channelFilter || undefined,
        sku_key: skuFilter || undefined,
        error_code: errorCodeFilter || undefined,
        start_date: startDateFilter || undefined,
        end_date: endDateFilter || undefined,
        q: searchQuery || undefined,
        page,
        page_size: PAGE_SIZE,
      });
      setData(result);
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 401) {
        onUnauthorized();
        return;
      }
      setError(caught instanceof ApiError ? caught.message : "加载订单列表失败，请重试");
    } finally {
      setLoading(false);
    }
  }, [
    token,
    statusFilter,
    channelFilter,
    skuFilter,
    errorCodeFilter,
    startDateFilter,
    endDateFilter,
    searchQuery,
    page,
    dateRangeInvalid,
    onUnauthorized,
  ]);

  useEffect(() => {
    void fetchList();
  }, [fetchList, refreshKey]);

  const handleSearchSubmit = (event: FormEvent) => {
    event.preventDefault();
    updateParams({
      q: searchDraft.trim(),
      error_code: errorCodeDraft.trim(),
    });
  };

  const handleSearchKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Enter" && isImeComposing(event)) {
      event.preventDefault();
    }
  };

  const handleExportCsv = async () => {
    if (dateRangeInvalid) {
      pushToast("开始日期不能晚于结束日期，请先调整日期范围", "error");
      return;
    }
    setExportingCsv(true);
    try {
      const filename = await exportAdminReconciliationCsv(token, {
        start_date: startDateFilter || undefined,
        end_date: endDateFilter || undefined,
        channel: channelFilter || undefined,
        exclude_inactive: excludeInactiveExport,
      });
      pushToast(`已导出对账表 ${filename}`, "success");
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 401) {
        onUnauthorized();
        return;
      }
      pushToast(caught instanceof ApiError ? caught.message : "导出对账 CSV 失败", "error");
    } finally {
      setExportingCsv(false);
    }
  };

  const hasActiveFilters = Boolean(
    statusFilter ||
      channelFilter ||
      skuFilter ||
      errorCodeFilter ||
      startDateFilter ||
      endDateFilter ||
      searchQuery
  );

  const handleResetFilters = () => {
    setSearchDraft("");
    setErrorCodeDraft("");
    setSearchParams(new URLSearchParams(), { replace: true });
  };

  const total = data?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const items = data?.items ?? [];

  return (
    <div className="stack-lg">
      <header className="admin-header">
        <div>
          <h1 className="admin-page-title">订单和保修</h1>
          <p className="admin-page-sub">
            点开任意订单可查看买家与商店邮箱、保修主邮箱、TG 自动报备状态与越南语话术，以及查进度、重下或作废。
          </p>
        </div>
        <div className="row" style={{ flexWrap: "wrap", justifyContent: "flex-end" }}>
          <span className="badge badge-neutral">共 {total} 条</span>
          <label
            className="admin-inline-check"
            title="排除已作废以及上游状态为 cancelled/refunded 的订单"
          >
            <input
              type="checkbox"
              checked={excludeInactiveExport}
              onChange={(event) => setExcludeInactiveExport(event.target.checked)}
            />
            <span>仅导出有效单</span>
          </label>
          <button
            type="button"
            className={`btn btn-secondary btn-sm ${exportingCsv ? "is-loading" : ""}`}
            disabled={exportingCsv || dateRangeInvalid}
            aria-busy={exportingCsv}
            onClick={() => void handleExportCsv()}
          >
            <CastoriceIcon name={exportingCsv ? "refresh" : "download"} size={16} spin={exportingCsv} />
            <span>{exportingCsv ? "导出中..." : "导出对账 CSV"}</span>
          </button>
          <button
            type="button"
            className={`btn btn-secondary btn-sm ${loading ? "is-loading" : ""}`}
            disabled={loading}
            aria-busy={loading}
            onClick={() => void fetchList()}
          >
            <CastoriceIcon name="refresh" size={16} spin={loading} />
            <span>刷新</span>
          </button>
        </div>
      </header>

      <section className="admin-toolbar-glass stack-sm" aria-label="订单筛选工具栏">
        <div className="pill-scroller" role="tablist" aria-label="按订单状态筛选">
          {STATUS_TABS.map((tab) => {
            const tabCount = tab.countKey && counts ? counts[tab.countKey] : undefined;
            return (
              <button
                key={tab.value || "all"}
                type="button"
                role="tab"
                aria-selected={statusFilter === tab.value}
                className={`filter-pill ${statusFilter === tab.value ? "is-active" : ""}`}
                onClick={() => updateParams({ status: tab.value })}
              >
                <span>{tab.label}</span>
                {tabCount !== undefined && tabCount > 0 && (
                  <span
                    className={`filter-pill-count ${
                      tab.alertTone === "danger"
                        ? "is-danger"
                        : tab.alertTone === "warning"
                          ? "is-warning"
                          : ""
                    }`}
                  >
                    {tabCount}
                  </span>
                )}
              </button>
            );
          })}
        </div>

        <form className="stack-sm" onSubmit={handleSearchSubmit}>
          <div className="filter-bar">
            <div className="input-wrap">
              <input
                type="search"
                className="input"
                placeholder="搜兑换码 / 淘宝单号 / 上游单号 / 买家或商店邮箱 / 备注"
                aria-label="搜索订单关键词"
                enterKeyHint="search"
                value={searchDraft}
                onChange={(event) => setSearchDraft(event.target.value)}
                onKeyDown={handleSearchKeyDown}
              />
            </div>

            <select
              className="select"
              aria-label="按销售渠道筛选"
              value={channelFilter}
              onChange={(event) => updateParams({ channel: event.target.value })}
            >
              <option value="">全部渠道</option>
              <option value="taobao">淘宝</option>
              <option value="16688">16688</option>
            </select>

            <select
              className="select"
              aria-label="按商品规格筛选"
              value={skuFilter}
              onChange={(event) => updateParams({ sku_key: event.target.value })}
            >
              <option value="">全部规格</option>
              <option value="ultra_shared_slot">共享车位</option>
              <option value="ultra_exclusive_admin">独享整组</option>
            </select>

            <button type="submit" className="btn btn-primary">
              <CastoriceIcon name="admin-ledger" size={19} />
              <span>筛选</span>
            </button>
          </div>

          <div className="admin-filter-secondary">
            <div className="input-wrap">
              <input
                type="text"
                list="admin-error-code-options"
                className="input mono"
                placeholder="按错误码筛选（如 LOW_BALANCE / CANBOSO_ORDER_CANCELLED）"
                aria-label="按错误码筛选"
                value={errorCodeDraft}
                onChange={(event) => setErrorCodeDraft(event.target.value)}
                onKeyDown={handleSearchKeyDown}
              />
              <datalist id="admin-error-code-options">
                {COMMON_ERROR_CODES.map((entry) => (
                  <option key={entry.code} value={entry.code}>
                    {entry.label}
                  </option>
                ))}
              </datalist>
            </div>

            <div className="admin-date-range">
              <input
                type="date"
                className="input mono"
                aria-label="开始日期"
                value={startDateFilter}
                onChange={(event) => updateParams({ start_date: event.target.value })}
              />
              <span className="admin-date-sep" aria-hidden="true">
                至
              </span>
              <input
                type="date"
                className="input mono"
                aria-label="结束日期"
                value={endDateFilter}
                onChange={(event) => updateParams({ end_date: event.target.value })}
              />
            </div>

            {hasActiveFilters && (
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                onClick={handleResetFilters}
              >
                清空条件
              </button>
            )}
          </div>
        </form>
      </section>

      {error && (
        <div className="callout callout-danger" role="alert">
          <CastoriceIcon name="alert" size={20} className="callout-icon" />
          <div>{error}</div>
        </div>
      )}

      {loading && !data ? (
        <div className="card stack">
          <div className="skeleton" style={{ height: 22, width: "42%" }} />
          <div className="skeleton" style={{ height: 48, width: "100%" }} />
          <div className="skeleton" style={{ height: 48, width: "100%" }} />
          <div className="skeleton" style={{ height: 48, width: "100%" }} />
        </div>
      ) : items.length === 0 ? (
        <div className="card stack" style={{ alignItems: "center", textAlign: "center", padding: "36px 20px" }}>
          <CastoriceIcon name="admin-wait" size={64} />
          <h2 className="card-title" style={{ fontSize: 16 }}>
            当前条件下暂无订单记录
          </h2>
          <p className="card-subtitle">
            {statusFilter === ""
              ? "「全部」不包含在库存码；可清空搜索词或切到「在库存」查看库存码。"
              : "可尝试清空搜索词或切换上方状态标签页查看其他记录。"}
          </p>
          {hasActiveFilters && (
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              onClick={handleResetFilters}
            >
              重置筛选条件
            </button>
          )}
        </div>
      ) : (
        <>
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>兑换码 / 外部单号</th>
                  <th>规格与渠道</th>
                  <th>当前状态</th>
                  <th>买家 / 商店邮箱</th>
                  <th>上游单号与成本</th>
                  <th>{getTimeColumnLabel(statusFilter)}</th>
                </tr>
              </thead>
              <tbody>
                {items.map((item) => (
                  <tr
                    key={item.code}
                    tabIndex={0}
                    aria-label={`查看兑换码 ${item.code} 详情`}
                    onClick={() => onSelectCode(item.code)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        onSelectCode(item.code);
                      }
                    }}
                  >
                    <td>
                      <div className="stack-sm" style={{ gap: 3 }}>
                        <strong className="mono" style={{ color: "var(--ink-1)" }}>
                          {item.code}
                        </strong>
                        <span style={{ fontSize: 12, color: "var(--ink-3)" }}>
                          {item.external_order_id ||
                            item.remark ||
                            (item.status === "unused" ? "在库存（未绑定订单）" : "未绑定外部单号")}
                        </span>
                      </div>
                    </td>
                    <td>
                      <div className="stack-sm" style={{ gap: 4, alignItems: "flex-start" }}>
                        {renderSkuTag(item.sku_key)}
                        <span style={{ fontSize: 12, color: "var(--ink-3)" }}>
                          {getChannelLabel(item.channel)}
                          {item.goods_no ? ` · ${item.goods_no}` : ""}
                        </span>
                      </div>
                    </td>
                    <td>
                      <div className="stack-sm" style={{ gap: 4, alignItems: "flex-start" }}>
                        <OrderDisplayStatusBadge
                          skuKey={item.sku_key}
                          status={item.status}
                          warrantyStatus={item.warranty_status}
                          externalOrderId={item.external_order_id}
                          isDispatched={item.is_dispatched}
                        />
                        {item.is_overdue_24h && (
                          <span className="badge badge-danger">
                            <CastoriceIcon name="admin-alert" size={16} />
                            <span>超 24 小时</span>
                          </span>
                        )}
                        {item.last_error_code && (
                          <span
                            className="badge badge-danger mono"
                            style={{ fontSize: 11 }}
                            title={formatOrderErrorMessage(
                              item.last_error_code,
                              item.last_error_message
                            )}
                          >
                            {item.last_error_code}
                          </span>
                        )}
                      </div>
                    </td>
                    <td>{renderOrderEmails(item)}</td>
                    <td>
                      <div className="stack-sm" style={{ gap: 3 }}>
                        <span className="mono" style={{ fontSize: 12.5 }}>
                          {item.upstream_order_code || "-"}
                        </span>
                        <span className="mono" style={{ fontSize: 12, color: "var(--ink-3)" }}>
                          {formatUsd(item.cost_usd)}
                        </span>
                        {item.upstream_refund_usd !== null &&
                          item.upstream_refund_usd !== undefined && (
                            <span className="mono admin-refund-tag">
                              实退 {formatUsd(item.upstream_refund_usd)}
                            </span>
                          )}
                      </div>
                    </td>
                    <td>{renderOrderTimeCell(item, statusFilter)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="mobile-order-list">
            {items.map((item) => (
              <button
                key={item.code}
                type="button"
                className="mobile-order-card"
                onClick={() => onSelectCode(item.code)}
              >
                <div className="row-between">
                  <strong className="mono" style={{ fontSize: 14, color: "var(--ink-1)" }}>
                    {item.code}
                  </strong>
                  <OrderDisplayStatusBadge
                    skuKey={item.sku_key}
                    status={item.status}
                    warrantyStatus={item.warranty_status}
                    externalOrderId={item.external_order_id}
                    isDispatched={item.is_dispatched}
                  />
                </div>

                <div className="row-between" style={{ fontSize: 12.5, color: "var(--ink-3)" }}>
                  <span className="row" style={{ gap: 6 }}>
                    {renderSkuTag(item.sku_key)}
                    <span>{getChannelLabel(item.channel)}</span>
                  </span>
                  <span className="mono">
                    {formatUsd(item.cost_usd)}
                    {item.upstream_refund_usd !== null && item.upstream_refund_usd !== undefined
                      ? ` · 实退 ${formatUsd(item.upstream_refund_usd)}`
                      : ""}
                  </span>
                </div>

                <div style={{ fontSize: 12.5, color: "var(--ink-2)", textAlign: "left" }}>
                  {renderOrderEmails(item)}
                </div>

                {item.external_order_id && (
                  <div style={{ fontSize: 12, color: "var(--ink-3)", textAlign: "left" }}>
                    外部单号：{item.external_order_id}
                  </div>
                )}

                <div className="row-between" style={{ fontSize: 12, color: "var(--ink-3)" }}>
                  <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
                    {item.is_overdue_24h && <span className="badge badge-danger">超 24 小时</span>}
                    {item.last_error_code && (
                      <span className="badge badge-danger mono" style={{ fontSize: 11 }}>
                        {item.last_error_code}
                      </span>
                    )}
                  </div>
                  <span className="mono">{formatMobileOrderTime(item, statusFilter)}</span>
                </div>
              </button>
            ))}
          </div>

          {totalPages > 1 && (
            <div className="row-between admin-pagination-bar">
              <span style={{ fontSize: 13, color: "var(--ink-3)" }}>
                第 {page} / {totalPages} 页 · 共 {total} 条
              </span>
              <div className="row">
                <button
                  type="button"
                  className="btn btn-secondary btn-sm"
                  disabled={page <= 1 || loading}
                  onClick={() => updateParams({ page: Math.max(1, page - 1) }, false)}
                >
                  <CastoriceIcon name="arrow-left" size={16} />
                  <span>上一页</span>
                </button>
                <button
                  type="button"
                  className="btn btn-secondary btn-sm"
                  disabled={page >= totalPages || loading}
                  onClick={() => updateParams({ page: Math.min(totalPages, page + 1) }, false)}
                >
                  <span>下一页</span>
                  <CastoriceIcon name="arrow-right" size={16} />
                </button>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}
