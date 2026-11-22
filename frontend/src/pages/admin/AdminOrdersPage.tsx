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
import { CodeStatusBadge, WarrantyStatusBadge } from "../../components/StatusBadge";
import type { AdminCodeListResponse, CodeStatus } from "../../types/api";
import {
  formatDateTime,
  formatUsd,
  getChannelLabel,
  getSkuShortLabel,
} from "../../utils/format";
import { isImeComposing } from "../../utils/ime";

interface AdminOrdersPageProps {
  token: string;
  initialStatus: CodeStatus | "";
  refreshKey: number;
  onUnauthorized: () => void;
  onSelectCode: (code: string) => void;
}

const STATUS_TABS: Array<{ value: CodeStatus | ""; label: string }> = [
  { value: "", label: "全部" },
  { value: "unused", label: "待核销" },
  { value: "waiting_fulfillment", label: "排单拉人中" },
  { value: "delivered_pending_warranty", label: "待登记保修" },
  { value: "completed", label: "已完成" },
  { value: "needs_manual_review", label: "需人工处理" },
  { value: "revoked", label: "已作废" },
];

const PAGE_SIZE = 20;

export function AdminOrdersPage({
  token,
  initialStatus,
  refreshKey,
  onUnauthorized,
  onSelectCode,
}: AdminOrdersPageProps) {
  const [statusFilter, setStatusFilter] = useState<CodeStatus | "">(initialStatus);
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
          <h1 className="admin-page-title">订单与保修明细</h1>
          <p className="admin-page-sub">
            点击任意订单可打开详情抽屉，查看保修截图、转发模板、上游轮询、重试与作废操作。
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

      <div className="stack-sm">
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
              placeholder="搜兑换码 / 淘宝单号 / 上游单号 / 邮箱 / 备注"
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
      </div>

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
          <p className="card-subtitle">可尝试清空搜索词或切换上方状态标签页查看全部记录。</p>
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
                  <th>买家邮箱 / 保修</th>
                  <th>上游单号与成本</th>
                  <th>更新时间</th>
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
                          {item.external_order_id || item.remark || "未绑定外部单号"}
                        </span>
                      </div>
                    </td>
                    <td>
                      <div className="stack-sm" style={{ gap: 4 }}>
                        <span>{getSkuShortLabel(item.sku_key)}</span>
                        <span style={{ fontSize: 12, color: "var(--ink-3)" }}>
                          {getChannelLabel(item.channel)}
                          {item.goods_no ? ` · ${item.goods_no}` : ""}
                        </span>
                      </div>
                    </td>
                    <td>
                      <div className="stack-sm" style={{ gap: 4, alignItems: "flex-start" }}>
                        <CodeStatusBadge status={item.status} />
                        {item.is_overdue_24h && (
                          <span className="badge badge-danger">
                            <Clock size={12} aria-hidden="true" />
                            <span>超 24 小时</span>
                          </span>
                        )}
                      </div>
                    </td>
                    <td>
                      <div className="stack-sm" style={{ gap: 4, alignItems: "flex-start" }}>
                        <span className="mono" style={{ fontSize: 12.5 }}>
                          {item.buyer_email_masked || "-"}
                        </span>
                        {item.sku_key === "ultra_exclusive_admin" && item.warranty_status && (
                          <WarrantyStatusBadge status={item.warranty_status} />
                        )}
                      </div>
                    </td>
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
                    <td>
                      <span className="mono" style={{ fontSize: 12.5 }}>
                        {formatDateTime(item.updated_at)}
                      </span>
                    </td>
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
                  <CodeStatusBadge status={item.status} />
                </div>

                <div className="row-between" style={{ fontSize: 12.5, color: "var(--ink-3)" }}>
                  <span>
                    {getSkuShortLabel(item.sku_key)} · {getChannelLabel(item.channel)}
                  </span>
                  <span className="mono">{formatUsd(item.cost_usd)}</span>
                </div>

                {(item.buyer_email_masked || item.external_order_id) && (
                  <div style={{ fontSize: 12.5, color: "var(--ink-2)" }}>
                    {item.buyer_email_masked ? `邮箱：${item.buyer_email_masked}` : `外部单号：${item.external_order_id}`}
                  </div>
                )}

                <div className="row-between" style={{ fontSize: 12, color: "var(--ink-3)" }}>
                  <div className="row" style={{ gap: 6 }}>
                    {item.is_overdue_24h && <span className="badge badge-danger">超 24 小时</span>}
                    {item.sku_key === "ultra_exclusive_admin" && item.warranty_status && (
                      <WarrantyStatusBadge status={item.warranty_status} />
                    )}
                  </div>
                  <span className="mono">{formatDateTime(item.updated_at)}</span>
                </div>
              </button>
            ))}
          </div>

          {totalPages > 1 && (
            <div className="row-between">
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
