import { useCallback, useEffect, useState, type FormEvent, type KeyboardEvent } from "react";
import {
  AlertCircle,
  ChevronLeft,
  ChevronRight,
  Clock,
  Inbox,
  Loader2,
  RefreshCw,
  Search,
} from "lucide-react";
import { ApiError, listAdminCodes } from "../../api/client";
import { OrderDisplayStatusBadge } from "../../components/StatusBadge";
import type { AdminCodeListResponse, AdminCodeSummary, AdminOrderStatusFilter } from "../../types/api";
import {
  formatDateTime,
  formatUsd,
  getChannelLabel,
  getSkuShortLabel,
} from "../../utils/format";
import { isImeComposing } from "../../utils/ime";

interface AdminOrdersPageProps {
  token: string;
  initialStatus: AdminOrderStatusFilter | "";
  refreshKey: number;
  onUnauthorized: () => void;
  onSelectCode: (code: string) => void;
}

const STATUS_TABS: Array<{ value: AdminOrderStatusFilter | ""; label: string }> = [
  { value: "", label: "全部" },
  { value: "in_stock", label: "在库存" },
  { value: "dispatched_unused", label: "已发出待提货" },
  { value: "waiting_fulfillment", label: "排队拉人中" },
  { value: "completed", label: "已完成" },
  { value: "manual_attention", label: "需人工处理" },
  { value: "revoked", label: "已作废" },
];

const PAGE_SIZE = 20;

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
  initialStatus,
  refreshKey,
  onUnauthorized,
  onSelectCode,
}: AdminOrdersPageProps) {
  const [statusFilter, setStatusFilter] = useState<AdminOrderStatusFilter | "">(initialStatus);
  const [channelFilter, setChannelFilter] = useState("");
  const [skuFilter, setSkuFilter] = useState("");
  const [searchDraft, setSearchDraft] = useState("");
  const [searchQuery, setSearchQuery] = useState("");
  const [page, setPage] = useState(1);

  const [data, setData] = useState<AdminCodeListResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setStatusFilter(initialStatus);
    setPage(1);
  }, [initialStatus]);

  const fetchList = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await listAdminCodes(token, {
        status: statusFilter || undefined,
        channel: channelFilter || undefined,
        sku_key: skuFilter || undefined,
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
  }, [token, statusFilter, channelFilter, skuFilter, searchQuery, page, onUnauthorized]);

  useEffect(() => {
    void fetchList();
  }, [fetchList, refreshKey]);

  const handleSearchSubmit = (event: FormEvent) => {
    event.preventDefault();
    setPage(1);
    setSearchQuery(searchDraft.trim());
  };

  const handleSearchKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Enter" && isImeComposing(event)) {
      event.preventDefault();
    }
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
        <div className="row">
          <span className="badge badge-neutral">共 {total} 条</span>
          <button
            type="button"
            className={`btn btn-secondary btn-sm ${loading ? "is-loading" : ""}`}
            disabled={loading}
            aria-busy={loading}
            onClick={() => void fetchList()}
          >
            {loading ? <Loader2 size={14} className="spin" /> : <RefreshCw size={14} />}
            <span>刷新</span>
          </button>
        </div>
      </header>

      <section className="admin-toolbar-glass stack-sm" aria-label="订单筛选工具栏">
        <div className="pill-scroller" role="tablist" aria-label="按订单状态筛选">
          {STATUS_TABS.map((tab) => (
            <button
              key={tab.value || "all"}
              type="button"
              role="tab"
              aria-selected={statusFilter === tab.value}
              className={`filter-pill ${statusFilter === tab.value ? "is-active" : ""}`}
              onClick={() => {
                setStatusFilter(tab.value);
                setPage(1);
              }}
            >
              {tab.label}
            </button>
          ))}
        </div>

        <form className="filter-bar" onSubmit={handleSearchSubmit}>
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
            onChange={(event) => {
              setChannelFilter(event.target.value);
              setPage(1);
            }}
          >
            <option value="">全部渠道</option>
            <option value="taobao">淘宝</option>
            <option value="16688">16688</option>
          </select>

          <select
            className="select"
            aria-label="按商品规格筛选"
            value={skuFilter}
            onChange={(event) => {
              setSkuFilter(event.target.value);
              setPage(1);
            }}
          >
            <option value="">全部规格</option>
            <option value="ultra_shared_slot">共享车位</option>
            <option value="ultra_exclusive_admin">独享整组</option>
          </select>

          <button type="submit" className="btn btn-primary">
            <Search size={15} />
            <span>筛选</span>
          </button>
        </form>
      </section>

      {error && (
        <div className="callout callout-danger" role="alert">
          <AlertCircle size={18} className="callout-icon" aria-hidden="true" />
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
          <Inbox size={28} style={{ color: "var(--ink-3)" }} aria-hidden="true" />
          <h2 className="card-title" style={{ fontSize: 16 }}>
            当前条件下暂无订单记录
          </h2>
          <p className="card-subtitle">
            {statusFilter === ""
              ? "「全部」不包含在库存码；可清空搜索词或切到「在库存」查看库存码。"
              : "可尝试清空搜索词或切换上方状态标签页查看其他记录。"}
          </p>
          {(statusFilter || channelFilter || skuFilter || searchQuery) && (
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              onClick={() => {
                setStatusFilter("");
                setChannelFilter("");
                setSkuFilter("");
                setSearchDraft("");
                setSearchQuery("");
                setPage(1);
              }}
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
                            <Clock size={12} aria-hidden="true" />
                            <span>超 24 小时</span>
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
                  <span className="mono">{formatUsd(item.cost_usd)}</span>
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
                  <div className="row" style={{ gap: 6 }}>
                    {item.is_overdue_24h && <span className="badge badge-danger">超 24 小时</span>}
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
                  onClick={() => setPage((prev) => Math.max(1, prev - 1))}
                >
                  <ChevronLeft size={15} />
                  <span>上一页</span>
                </button>
                <button
                  type="button"
                  className="btn btn-secondary btn-sm"
                  disabled={page >= totalPages || loading}
                  onClick={() => setPage((prev) => Math.min(totalPages, prev + 1))}
                >
                  <span>下一页</span>
                  <ChevronRight size={15} />
                </button>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}
