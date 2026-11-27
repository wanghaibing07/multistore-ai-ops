import { useCallback, useEffect, useState, type FormEvent } from "react";
import {
  ApiError,
  createAdminAgisoSkuMapping,
  deleteAdminAgisoSkuMapping,
  getAdminAgisoRemoteSkus,
  getAdminAgisoStatus,
  getAdminAgisoStock,
  getAdminChannel16688Stock,
  listAdminAgisoEvents,
  listAdminAgisoSkuMappings,
  pushAdminAgisoCards,
  restockAdminAgisoCards,
  restockAdminChannel16688Cards,
  retryAdminAgisoOrder,
  syncAdminAgisoOrders,
} from "../../api/client";
import { CastoriceIcon } from "../../components/CastoriceIcon";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { CopyButton } from "../../components/CopyButton";
import { useToast } from "../../components/ToastProvider";
import type {
  AdminAgisoEventsResponse,
  AdminAgisoPushCardsResponse,
  AdminAgisoRemoteSkuItem,
  AdminAgisoRestockResponse,
  AdminAgisoRetryOrderResponse,
  AdminAgisoSkuMapping,
  AdminAgisoSkuMappingsResponse,
  AdminAgisoStatus,
  AdminAgisoStockResponse,
  AdminAgisoSyncOrdersResponse,
  AdminChannel16688RestockResponse,
  AdminChannel16688StockResponse,
  SkuKey,
} from "../../types/api";
import { formatDateTime, getSkuShortLabel } from "../../utils/format";

interface AdminAutomationPageProps {
  token: string;
  onUnauthorized: () => void;
  onMutated: () => void;
  onSelectCode: (code: string) => void;
}

type AutomationSubTab = "stock" | "mappings" | "events";

const EVENTS_PAGE_SIZE = 20;

function formatDeliverySummaryLabel(key: string): string {
  switch (key) {
    case "delivered":
      return "已发货";
    case "pending":
      return "待发货";
    case "failed":
      return "失败";
    case "revoked":
      return "已作废";
    case "restored":
      return "已恢复";
    default:
      return key;
  }
}

function isUpstreamDegradedError(caught: unknown): boolean {
  return caught instanceof ApiError && (caught.status === 503 || caught.status === 504 || caught.status === 502);
}

export function AdminAutomationPage({
  token,
  onUnauthorized,
  onMutated,
  onSelectCode,
}: AdminAutomationPageProps) {
  const { pushToast } = useToast();
  const [subTab, setSubTab] = useState<AutomationSubTab>("stock");

  // Status header state
  const [status, setStatus] = useState<AdminAgisoStatus | null>(null);
  const [statusLoading, setStatusLoading] = useState(true);
  const [statusError, setStatusError] = useState<string | null>(null);

  // Sub-tab 1: Stock & Auto Restock
  const [agisoStock, setAgisoStock] = useState<AdminAgisoStockResponse | null>(null);
  const [agisoStockLoading, setAgisoStockLoading] = useState(false);
  const [agisoStockError, setAgisoStockError] = useState<string | null>(null);
  const [agisoRestocking, setAgisoRestocking] = useState(false);
  const [agisoRestockResult, setAgisoRestockResult] = useState<AdminAgisoRestockResponse | null>(null);
  const [agisoRestockConfirmOpen, setAgisoRestockConfirmOpen] = useState(false);

  const [channelStock, setChannelStock] = useState<AdminChannel16688StockResponse | null>(null);
  const [channelStockLoading, setChannelStockLoading] = useState(false);
  const [channelStockError, setChannelStockError] = useState<string | null>(null);
  const [channelSyncSold, setChannelSyncSold] = useState(true);
  const [channelRestocking, setChannelRestocking] = useState(false);
  const [channelRestockResult, setChannelRestockResult] =
    useState<AdminChannel16688RestockResponse | null>(null);
  const [channelRestockConfirmOpen, setChannelRestockConfirmOpen] = useState(false);

  // Sub-tab 2: SKU Mappings & Manual Push Cards
  const [remoteNumIids, setRemoteNumIids] = useState("");
  const [remoteSkus, setRemoteSkus] = useState<AdminAgisoRemoteSkuItem[] | null>(null);
  const [remoteSkusLoading, setRemoteSkusLoading] = useState(false);
  const [remoteSkusError, setRemoteSkusError] = useState<string | null>(null);

  const [mappingsData, setMappingsData] = useState<AdminAgisoSkuMappingsResponse | null>(null);
  const [mappingsLoading, setMappingsLoading] = useState(false);
  const [mappingsError, setMappingsError] = useState<string | null>(null);
  const [mapSkuKey, setMapSkuKey] = useState<SkuKey>("ultra_shared_slot");
  const [mapNumIid, setMapNumIid] = useState("");
  const [mapSkuId, setMapSkuId] = useState("");
  const [mapOuterIid, setMapOuterIid] = useState("");
  const [mapOuterSkuId, setMapOuterSkuId] = useState("");
  const [mapPropsContains, setMapPropsContains] = useState("");
  const [mapEnabled, setMapEnabled] = useState(true);
  const [mapNote, setMapNote] = useState("");
  const [creatingMapping, setCreatingMapping] = useState(false);
  const [deleteTargetMapping, setDeleteTargetMapping] = useState<AdminAgisoSkuMapping | null>(null);
  const [deletingMapping, setDeletingMapping] = useState(false);

  const [pushSkuKey, setPushSkuKey] = useState<SkuKey>("ultra_shared_slot");
  const [pushCpcId, setPushCpcId] = useState("");
  const [pushCount, setPushCount] = useState("10");
  const [pushBatchLabel, setPushBatchLabel] = useState("");
  const [pushUseFirst, setPushUseFirst] = useState(false);
  const [pushAllowReplace, setPushAllowReplace] = useState(false);
  const [pushConfirmOpen, setPushConfirmOpen] = useState(false);
  const [pushingCards, setPushingCards] = useState(false);
  const [pushResult, setPushResult] = useState<AdminAgisoPushCardsResponse | null>(null);

  // Sub-tab 3: Order Sync & Event Stream
  const [syncLookback, setSyncLookback] = useState("60");
  const [syncPageSize, setSyncPageSize] = useState("50");
  const [syncMaxPages, setSyncMaxPages] = useState("5");
  const [syncingOrders, setSyncingOrders] = useState(false);
  const [syncResult, setSyncResult] = useState<AdminAgisoSyncOrdersResponse | null>(null);

  const [retryTidInput, setRetryTidInput] = useState("");
  const [retryFetchFromAgiso, setRetryFetchFromAgiso] = useState(true);
  const [retryingOrder, setRetryingOrder] = useState(false);
  const [retryResult, setRetryResult] = useState<AdminAgisoRetryOrderResponse | null>(null);

  const [eventsTidDraft, setEventsTidDraft] = useState("");
  const [eventsAopicDraft, setEventsAopicDraft] = useState("");
  const [eventsStatusDraft, setEventsStatusDraft] = useState("");
  const [eventsQuery, setEventsQuery] = useState<{
    tid: string;
    aopic: string;
    status: string;
    page: number;
  }>({ tid: "", aopic: "", status: "", page: 1 });
  const [eventsData, setEventsData] = useState<AdminAgisoEventsResponse | null>(null);
  const [eventsLoading, setEventsLoading] = useState(false);
  const [eventsError, setEventsError] = useState<string | null>(null);

  const handleAuthCheck = useCallback(
    (caught: unknown): boolean => {
      if (caught instanceof ApiError && caught.status === 401) {
        onUnauthorized();
        return true;
      }
      return false;
    },
    [onUnauthorized]
  );

  const loadStatus = useCallback(async () => {
    setStatusLoading(true);
    setStatusError(null);
    try {
      const data = await getAdminAgisoStatus(token);
      setStatus(data);
    } catch (caught) {
      if (handleAuthCheck(caught)) return;
      setStatusError(caught instanceof ApiError ? caught.message : "加载渠道自动化状态失败");
    } finally {
      setStatusLoading(false);
    }
  }, [token, handleAuthCheck]);

  const loadAgisoStock = useCallback(async () => {
    setAgisoStockLoading(true);
    setAgisoStockError(null);
    try {
      const data = await getAdminAgisoStock(token);
      setAgisoStock(data);
    } catch (caught) {
      if (handleAuthCheck(caught)) return;
      if (isUpstreamDegradedError(caught)) {
        setAgisoStockError(
          `阿奇索远端接口暂不可用（HTTP ${(caught as ApiError).status}）：${(caught as ApiError).message}`
        );
      } else {
        setAgisoStockError(caught instanceof ApiError ? caught.message : "查询阿奇索库存失败");
      }
    } finally {
      setAgisoStockLoading(false);
    }
  }, [token, handleAuthCheck]);

  const loadChannelStock = useCallback(async () => {
    setChannelStockLoading(true);
    setChannelStockError(null);
    try {
      const data = await getAdminChannel16688Stock(token);
      setChannelStock(data);
    } catch (caught) {
      if (handleAuthCheck(caught)) return;
      if (isUpstreamDegradedError(caught)) {
        setChannelStockError(
          `16688 远端接口暂不可用（HTTP ${(caught as ApiError).status}）：${(caught as ApiError).message}`
        );
      } else {
        setChannelStockError(caught instanceof ApiError ? caught.message : "查询 16688 库存失败");
      }
    } finally {
      setChannelStockLoading(false);
    }
  }, [token, handleAuthCheck]);

  const loadMappings = useCallback(async () => {
    setMappingsLoading(true);
    setMappingsError(null);
    try {
      const data = await listAdminAgisoSkuMappings(token);
      setMappingsData(data);
    } catch (caught) {
      if (handleAuthCheck(caught)) return;
      setMappingsError(caught instanceof ApiError ? caught.message : "加载 SKU 映射失败");
    } finally {
      setMappingsLoading(false);
    }
  }, [token, handleAuthCheck]);

  const loadEvents = useCallback(async () => {
    setEventsLoading(true);
    setEventsError(null);
    try {
      const parsedAopic =
        eventsQuery.aopic.trim() !== "" ? Number.parseInt(eventsQuery.aopic.trim(), 10) : undefined;
      const data = await listAdminAgisoEvents(token, {
        tid: eventsQuery.tid.trim() || undefined,
        aopic: parsedAopic !== undefined && Number.isFinite(parsedAopic) ? parsedAopic : undefined,
        status: eventsQuery.status.trim() || undefined,
        page: eventsQuery.page,
        page_size: EVENTS_PAGE_SIZE,
      });
      setEventsData(data);
    } catch (caught) {
      if (handleAuthCheck(caught)) return;
      setEventsError(caught instanceof ApiError ? caught.message : "加载阿奇索事件流失败");
    } finally {
      setEventsLoading(false);
    }
  }, [token, eventsQuery, handleAuthCheck]);

  useEffect(() => {
    void loadStatus();
  }, [loadStatus]);

  useEffect(() => {
    if (subTab === "stock") {
      void loadAgisoStock();
      void loadChannelStock();
    } else if (subTab === "mappings") {
      void loadMappings();
    } else if (subTab === "events") {
      void loadEvents();
    }
  }, [subTab, loadAgisoStock, loadChannelStock, loadMappings, loadEvents]);

  const handleRefreshAll = () => {
    void loadStatus();
    if (subTab === "stock") {
      void loadAgisoStock();
      void loadChannelStock();
    } else if (subTab === "mappings") {
      void loadMappings();
    } else {
      void loadEvents();
    }
  };

  const runAgisoRestock = async (dryCheck: boolean) => {
    setAgisoRestocking(true);
    try {
      const result = await restockAdminAgisoCards(token, { dry_check: dryCheck });
      setAgisoRestockResult(result);
      setAgisoRestockConfirmOpen(false);
      if (!dryCheck) {
        onMutated();
        void loadStatus();
        void loadAgisoStock();
      }
      pushToast(
        dryCheck
          ? `阿奇索补卡试跑完成：检查 ${result.checked} 项`
          : `阿奇索补卡完成：已补 ${result.total_pushed} 张`,
        result.errors.length > 0 ? "error" : "success"
      );
    } catch (caught) {
      if (handleAuthCheck(caught)) return;
      pushToast(caught instanceof ApiError ? caught.message : "阿奇索补卡执行失败", "error");
    } finally {
      setAgisoRestocking(false);
    }
  };

  const runChannelRestock = async (dryCheck: boolean) => {
    setChannelRestocking(true);
    try {
      const result = await restockAdminChannel16688Cards(token, {
        dry_check: dryCheck,
        sync_sold: channelSyncSold,
      });
      setChannelRestockResult(result);
      setChannelRestockConfirmOpen(false);
      if (!dryCheck) {
        onMutated();
        void loadStatus();
        void loadChannelStock();
      }
      pushToast(
        dryCheck
          ? `16688 补卡试跑完成：检查 ${result.checked} 项`
          : `16688 补卡完成：已补 ${result.total_pushed} 张`,
        result.errors.length > 0 ? "error" : "success"
      );
    } catch (caught) {
      if (handleAuthCheck(caught)) return;
      pushToast(caught instanceof ApiError ? caught.message : "16688 补卡执行失败", "error");
    } finally {
      setChannelRestocking(false);
    }
  };

  const handleQueryRemoteSkus = async (event: FormEvent) => {
    event.preventDefault();
    const cleaned = remoteNumIids.trim();
    if (!cleaned) {
      pushToast("请先输入至少一个淘宝宝贝 NumIid", "error");
      return;
    }
    setRemoteSkusLoading(true);
    setRemoteSkusError(null);
    try {
      const data = await getAdminAgisoRemoteSkus(token, cleaned);
      setRemoteSkus(data.items);
      pushToast(`查询到 ${data.items.length} 条远端宝贝 SKU`, "success");
    } catch (caught) {
      if (handleAuthCheck(caught)) return;
      const msg = caught instanceof ApiError ? caught.message : "查询远端宝贝 SKU 失败";
      setRemoteSkusError(msg);
      pushToast(msg, "error");
    } finally {
      setRemoteSkusLoading(false);
    }
  };

  const handlePrefillMappingFromRemote = (item: AdminAgisoRemoteSkuItem) => {
    if (item.NumIid !== undefined && item.NumIid !== null) setMapNumIid(String(item.NumIid));
    if (item.SkuId !== undefined && item.SkuId !== null) setMapSkuId(String(item.SkuId));
    if (item.OuterId) setMapOuterSkuId(String(item.OuterId));
    if (item.PropertiesName) setMapPropsContains(String(item.PropertiesName));
    pushToast("已将远端 SKU 信息填入下方映射表单", "success");
  };

  const handleCreateMapping = async (event: FormEvent) => {
    event.preventDefault();
    if (
      !mapNumIid.trim() &&
      !mapSkuId.trim() &&
      !mapOuterIid.trim() &&
      !mapOuterSkuId.trim() &&
      !mapPropsContains.trim()
    ) {
      pushToast("请至少填写一个匹配条件（宝贝ID / SKU ID / 商家编码 / 属性关键字）", "error");
      return;
    }
    setCreatingMapping(true);
    try {
      await createAdminAgisoSkuMapping(token, {
        sku_key: mapSkuKey,
        num_iid: mapNumIid.trim(),
        sku_id: mapSkuId.trim(),
        outer_iid: mapOuterIid.trim(),
        outer_sku_id: mapOuterSkuId.trim(),
        sku_props_contains: mapPropsContains.trim(),
        enabled: mapEnabled,
        note: mapNote.trim(),
      });
      setMapNumIid("");
      setMapSkuId("");
      setMapOuterIid("");
      setMapOuterSkuId("");
      setMapPropsContains("");
      setMapNote("");
      await loadMappings();
      void loadStatus();
      pushToast("已新增 SKU 映射规则", "success");
    } catch (caught) {
      if (handleAuthCheck(caught)) return;
      pushToast(caught instanceof ApiError ? caught.message : "新增 SKU 映射失败", "error");
    } finally {
      setCreatingMapping(false);
    }
  };

  const handleDeleteMappingConfirm = async () => {
    if (!deleteTargetMapping?.mapping_id) return;
    setDeletingMapping(true);
    try {
      await deleteAdminAgisoSkuMapping(token, deleteTargetMapping.mapping_id);
      setDeleteTargetMapping(null);
      await loadMappings();
      void loadStatus();
      pushToast("已删除该 SKU 映射规则", "success");
    } catch (caught) {
      if (handleAuthCheck(caught)) return;
      pushToast(caught instanceof ApiError ? caught.message : "删除 SKU 映射失败", "error");
    } finally {
      setDeletingMapping(false);
    }
  };

  const parsedPushCpcId = Number.parseInt(pushCpcId.trim(), 10);
  const parsedPushCount = Number.parseInt(pushCount.trim(), 10);
  const pushFormValid =
    Number.isFinite(parsedPushCpcId) &&
    parsedPushCpcId >= 1 &&
    Number.isFinite(parsedPushCount) &&
    parsedPushCount >= 1 &&
    parsedPushCount <= 500;

  const handlePushCardsSubmit = (event: FormEvent) => {
    event.preventDefault();
    if (!pushFormValid) {
      pushToast("请检查卡种 ID（≥ 1）和推卡数量（1~500）", "error");
      return;
    }
    setPushConfirmOpen(true);
  };

  const handlePushCardsConfirm = async () => {
    if (!pushFormValid) return;
    setPushingCards(true);
    try {
      const result = await pushAdminAgisoCards(token, {
        sku_key: pushSkuKey,
        cpc_id: parsedPushCpcId,
        count: parsedPushCount,
        batch_label: pushBatchLabel.trim() || undefined,
        is_use_first: pushUseFirst,
        allow_replace: pushAllowReplace,
      });
      setPushResult(result);
      setPushConfirmOpen(false);
      onMutated();
      void loadStatus();
      pushToast(
        result.partial_failure
          ? `手工推卡部分完成：成功 ${result.add_success_count ?? 0}，失败 ${result.add_fail_count ?? 0}`
          : `已成功推送 ${result.count} 张兑换码到卡种 ${result.cpc_id}`,
        result.partial_failure ? "error" : "success"
      );
    } catch (caught) {
      if (handleAuthCheck(caught)) return;
      pushToast(caught instanceof ApiError ? caught.message : "手工推卡失败", "error");
    } finally {
      setPushingCards(false);
    }
  };

  const handleSyncOrders = async (event: FormEvent) => {
    event.preventDefault();
    const lookback = Number.parseInt(syncLookback.trim(), 10);
    const pageSize = Number.parseInt(syncPageSize.trim(), 10);
    const maxPages = Number.parseInt(syncMaxPages.trim(), 10);
    if (
      !Number.isFinite(lookback) ||
      lookback < 1 ||
      lookback > 10080 ||
      !Number.isFinite(pageSize) ||
      pageSize < 1 ||
      pageSize > 100 ||
      !Number.isFinite(maxPages) ||
      maxPages < 1 ||
      maxPages > 20
    ) {
      pushToast("请检查拉单参数：回溯 1~10080 分钟，每页 1~100，最多 1~20 页", "error");
      return;
    }
    setSyncingOrders(true);
    try {
      const res = await syncAdminAgisoOrders(token, {
        lookback_minutes: lookback,
        page_size: pageSize,
        max_pages: maxPages,
      });
      setSyncResult(res);
      onMutated();
      void loadStatus();
      void loadEvents();
      pushToast(
        `拉单完成：拉取 ${res.fetched} 单，补发 ${res.fulfilled} 单，失败 ${res.failed} 单`,
        res.failed > 0 ? "error" : "success"
      );
    } catch (caught) {
      if (handleAuthCheck(caught)) return;
      pushToast(caught instanceof ApiError ? caught.message : "拉单补发失败", "error");
    } finally {
      setSyncingOrders(false);
    }
  };

  const handleRetryAgisoOrder = async (event: FormEvent) => {
    event.preventDefault();
    const tid = retryTidInput.trim();
    if (!tid) {
      pushToast("请先填写要重试发货的淘宝订单号（tid）", "error");
      return;
    }
    setRetryingOrder(true);
    try {
      const res = await retryAdminAgisoOrder(token, tid, retryFetchFromAgiso);
      setRetryResult(res);
      onMutated();
      void loadStatus();
      setEventsTidDraft(tid);
      setEventsQuery((prev) => ({ ...prev, tid, page: 1 }));
      pushToast(`订单 ${tid} 重试完成：${res.status}`, "success");
    } catch (caught) {
      if (handleAuthCheck(caught)) return;
      pushToast(caught instanceof ApiError ? caught.message : "重试阿奇索发货失败", "error");
    } finally {
      setRetryingOrder(false);
    }
  };

  const handleEventsSearch = (event: FormEvent) => {
    event.preventDefault();
    setEventsQuery({
      tid: eventsTidDraft.trim(),
      aopic: eventsAopicDraft.trim(),
      status: eventsStatusDraft.trim(),
      page: 1,
    });
  };

  const totalEventsPages = Math.max(
    1,
    Math.ceil((eventsData?.total ?? 0) / EVENTS_PAGE_SIZE)
  );

  const flagItems = status
    ? [
        { label: "自动建单结果", on: status.auto_create_result },
        { label: "自动虚拟发货", on: status.auto_dummy_send },
        { label: "自动发淘宝卡密", on: status.auto_send_tb_card },
        { label: "自动发旺旺消息", on: status.auto_send_ww_msg },
        { label: "退款自动作废", on: status.auto_revoke_on_refund },
        { label: "关退自动恢复", on: status.auto_restore_on_refund_close },
        {
          label: `定时拉单（${status.poll_interval_seconds}s）`,
          on: status.poll_enabled,
        },
        {
          label: `定时补卡（${status.restock_interval_seconds}s）`,
          on: status.restock_enabled,
        },
      ]
    : [];

  return (
    <div className="stack-lg">
      <header className="admin-header">
        <div>
          <h1 className="admin-page-title">渠道自动化</h1>
          <p className="admin-page-sub">
            统一监控阿奇索（淘宝）与 16688 库存水位、SKU 映射、手工推卡和订单同步事件流
          </p>
        </div>
        <div className="row" style={{ gap: 8 }}>
          <button
            type="button"
            className={`btn btn-secondary btn-sm ${statusLoading ? "is-loading" : ""}`}
            disabled={statusLoading}
            onClick={handleRefreshAll}
          >
            <CastoriceIcon name="refresh" size={16} spin={statusLoading} />
            <span>刷新数据</span>
          </button>
        </div>
      </header>

      <section className="card stack-sm" aria-label="阿奇索与自动化运行状态">
        {statusLoading && !status ? (
          <div className="stack-sm">
            <div className="skeleton" style={{ height: 24, width: "45%" }} />
            <div className="skeleton" style={{ height: 54, width: "100%" }} />
          </div>
        ) : statusError && !status ? (
          <div className="callout callout-danger" role="alert">
            <CastoriceIcon name="admin-alert" size={20} className="callout-icon" />
            <div className="row-between" style={{ width: "100%", gap: 10 }}>
              <span>{statusError}</span>
              <button
                type="button"
                className="btn btn-secondary btn-sm"
                onClick={() => void loadStatus()}
              >
                重试
              </button>
            </div>
          </div>
        ) : status ? (
          <>
            <div className="row-between" style={{ flexWrap: "wrap", gap: 10 }}>
              <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
                <span className={`badge ${status.configured ? "badge-success" : "badge-neutral"}`}>
                  配置：{status.configured ? "已配置" : "未配置"}
                </span>
                <span className={`badge ${status.webhook_ready ? "badge-success" : "badge-warning"}`}>
                  Webhook：{status.webhook_ready ? "就绪" : "未就绪"}
                </span>
                <span className={`badge ${status.api_ready ? "badge-success" : "badge-warning"}`}>
                  开放 API：{status.api_ready ? "就绪" : "未就绪"}
                </span>
                <span
                  className={`badge mono ${
                    status.execution_mode === "live" ? "badge-info" : "badge-warning"
                  }`}
                >
                  模式：{status.execution_mode}
                </span>
              </div>
              <div className="row" style={{ gap: 10, flexWrap: "wrap", fontSize: 12.5, color: "var(--ink-3)" }}>
                <span>SKU 映射：{status.sku_mappings_count}</span>
                <span>补卡规则：{status.restock_rules_count}</span>
                <span>事件总数：{status.events_total}</span>
              </div>
            </div>

            <div className="inset-panel row-between" style={{ gap: 10, flexWrap: "wrap" }}>
              <div className="stack-sm" style={{ gap: 2, minWidth: 0, flex: "1 1 280px" }}>
                <span className="kv-label">阿奇索 Webhook 回调地址</span>
                <span className="mono" style={{ fontSize: 12.5, wordBreak: "break-all" }}>
                  {status.webhook_url || "-"}
                </span>
              </div>
              {status.webhook_url && (
                <CopyButton
                  text={status.webhook_url}
                  label="复制回调地址"
                  toastMessage="已复制阿奇索 Webhook 回调地址"
                  variant="secondary"
                />
              )}
            </div>

            <div className="automation-chip-grid" aria-label="自动化开关状态">
              {flagItems.map((flag) => (
                <span
                  key={flag.label}
                  className={`automation-flag-chip ${flag.on ? "is-on" : "is-off"}`}
                >
                  <span className="automation-flag-dot" aria-hidden="true" />
                  <span>{flag.label}</span>
                </span>
              ))}
            </div>

            {Object.keys(status.deliveries_summary || {}).length > 0 && (
              <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
                <span style={{ fontSize: 12.5, color: "var(--ink-3)" }}>发货汇总：</span>
                {Object.entries(status.deliveries_summary).map(([key, value]) => (
                  <span key={key} className="badge badge-neutral mono">
                    {formatDeliverySummaryLabel(key)} {value}
                  </span>
                ))}
              </div>
            )}
          </>
        ) : null}
      </section>

      <div className="pill-scroller" role="tablist" aria-label="渠道自动化功能切换">
        <button
          type="button"
          role="tab"
          aria-selected={subTab === "stock"}
          className={`filter-pill ${subTab === "stock" ? "is-active" : ""}`}
          onClick={() => setSubTab("stock")}
        >
          库存与自动补卡
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={subTab === "mappings"}
          className={`filter-pill ${subTab === "mappings" ? "is-active" : ""}`}
          onClick={() => setSubTab("mappings")}
        >
          SKU 映射与手工推卡
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={subTab === "events"}
          className={`filter-pill ${subTab === "events" ? "is-active" : ""}`}
          onClick={() => setSubTab("events")}
        >
          订单同步与事件流
        </button>
      </div>

      {subTab === "stock" && (
        <div className="stack-lg">
          <section className="card stack-sm" aria-label="阿奇索淘宝库存与补卡">
            <div className="row-between" style={{ gap: 10, flexWrap: "wrap" }}>
              <div>
                <h2 className="card-title" style={{ fontSize: 16 }}>
                  阿奇索（淘宝）卡密库存
                </h2>
                <p className="card-subtitle">
                  对比阿奇索卡种现存量与本地未发放库存，支持无副作用试跑与一键补卡
                </p>
              </div>
              <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
                <button
                  type="button"
                  className="btn btn-secondary btn-sm"
                  disabled={agisoStockLoading}
                  onClick={() => void loadAgisoStock()}
                >
                  刷新库存
                </button>
                <button
                  type="button"
                  className="btn btn-secondary btn-sm"
                  disabled={agisoRestocking}
                  onClick={() => void runAgisoRestock(true)}
                >
                  试跑检查
                </button>
                <button
                  type="button"
                  className="btn btn-primary btn-sm"
                  disabled={agisoRestocking}
                  onClick={() => setAgisoRestockConfirmOpen(true)}
                >
                  确认补卡
                </button>
              </div>
            </div>

            {agisoStockError ? (
              <div className="callout callout-warning" role="alert">
                <CastoriceIcon name="alert" size={18} className="callout-icon" />
                <span>{agisoStockError}</span>
              </div>
            ) : agisoStockLoading && !agisoStock ? (
              <div className="skeleton" style={{ height: 90, width: "100%" }} />
            ) : agisoStock && agisoStock.items.length > 0 ? (
              <div className="automation-table-wrap">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>商品规格</th>
                      <th>卡种 ID</th>
                      <th>状态</th>
                      <th>远端余量</th>
                      <th>最低 / 目标</th>
                      <th>计划补卡</th>
                      <th>本地未发 / 已发待提</th>
                    </tr>
                  </thead>
                  <tbody>
                    {agisoStock.items.map((item) => (
                      <tr key={`${item.sku_key}-${item.cpc_id}`}>
                        <td>{getSkuShortLabel(item.sku_key)}</td>
                        <td className="mono">{item.cpc_id}</td>
                        <td>
                          <span
                            className={`badge ${
                              !item.enabled
                                ? "badge-neutral"
                                : item.needs_restock
                                  ? "badge-warning"
                                  : "badge-success"
                            }`}
                          >
                            {!item.enabled
                              ? "规则关闭"
                              : !item.storage_found
                                ? "未找到卡种"
                                : item.needs_restock
                                  ? "低于水位"
                                  : "水位充足"}
                          </span>
                        </td>
                        <td className="mono">{item.current_qty ?? "-"}</td>
                        <td className="mono">
                          {item.min_stock} / {item.target_stock}
                        </td>
                        <td className="mono">{item.planned_push}</td>
                        <td className="mono">
                          {item.local_in_stock_taobao} / {item.local_dispatched_unused_taobao}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="field-hint">暂未配置阿奇索自动补卡规则。</p>
            )}

            {agisoRestockResult && (
              <div className="inset-panel stack-sm">
                <div className="row-between" style={{ flexWrap: "wrap", gap: 6 }}>
                  <strong style={{ fontSize: 13 }}>
                    最近一次{agisoRestockResult.dry_check ? "试跑" : "补卡"}结果
                  </strong>
                  <span className="mono" style={{ fontSize: 12, color: "var(--ink-3)" }}>
                    检查 {agisoRestockResult.checked} · 补货 {agisoRestockResult.restocked} · 推卡{" "}
                    {agisoRestockResult.total_pushed}
                  </span>
                </div>
                {agisoRestockResult.errors.length > 0 && (
                  <p className="field-error">{agisoRestockResult.errors.join("；")}</p>
                )}
                {agisoRestockResult.items.length > 0 && (
                  <div className="automation-table-wrap">
                    <table className="data-table">
                      <thead>
                        <tr>
                          <th>SKU</th>
                          <th>动作</th>
                          <th>现存 / 计划 / 实推</th>
                          <th>批次 / 说明</th>
                        </tr>
                      </thead>
                      <tbody>
                        {agisoRestockResult.items.map((row, idx) => (
                          <tr key={`${row.sku_key}-${idx}`}>
                            <td className="mono">{row.sku_key}</td>
                            <td className="mono">{row.action}</td>
                            <td className="mono">
                              {row.current_qty ?? "-"} / {row.planned_push ?? 0} / {row.pushed_count}
                            </td>
                            <td className="mono">{row.error || row.batch_id || "-"}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            )}
          </section>

          <section className="card stack-sm" aria-label="16688 库存与补卡">
            <div className="row-between" style={{ gap: 10, flexWrap: "wrap" }}>
              <div>
                <h2 className="card-title" style={{ fontSize: 16 }}>
                  16688 自营卡密库存
                </h2>
                <p className="card-subtitle">
                  实时核对 16688 可售/锁定/已售数量，可先同步已售订单再自动补卡
                </p>
              </div>
              <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
                <label className="admin-inline-check">
                  <input
                    type="checkbox"
                    checked={channelSyncSold}
                    onChange={(event) => setChannelSyncSold(event.target.checked)}
                  />
                  <span>先同步已售</span>
                </label>
                <button
                  type="button"
                  className="btn btn-secondary btn-sm"
                  disabled={channelStockLoading}
                  onClick={() => void loadChannelStock()}
                >
                  刷新库存
                </button>
                <button
                  type="button"
                  className="btn btn-secondary btn-sm"
                  disabled={channelRestocking}
                  onClick={() => void runChannelRestock(true)}
                >
                  试跑检查
                </button>
                <button
                  type="button"
                  className="btn btn-primary btn-sm"
                  disabled={channelRestocking}
                  onClick={() => setChannelRestockConfirmOpen(true)}
                >
                  确认补卡
                </button>
              </div>
            </div>

            {channelStockError ? (
              <div className="callout callout-warning" role="alert">
                <CastoriceIcon name="alert" size={18} className="callout-icon" />
                <span>{channelStockError}</span>
              </div>
            ) : channelStockLoading && !channelStock ? (
              <div className="skeleton" style={{ height: 90, width: "100%" }} />
            ) : channelStock && channelStock.items.length > 0 ? (
              <div className="automation-table-wrap">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>商品规格</th>
                      <th>16688 编号</th>
                      <th>状态</th>
                      <th>可用 / 锁定 / 已售</th>
                      <th>最低 / 目标</th>
                      <th>计划补卡</th>
                      <th>本地未发 / 已发待提</th>
                    </tr>
                  </thead>
                  <tbody>
                    {channelStock.items.map((item) => (
                      <tr key={`${item.sku_key}-${item.goods_no}`}>
                        <td>{getSkuShortLabel(item.sku_key)}</td>
                        <td className="mono">{item.goods_no || "-"}</td>
                        <td>
                          <span
                            className={`badge ${
                              item.error
                                ? "badge-danger"
                                : !item.enabled
                                  ? "badge-neutral"
                                  : item.needs_restock
                                    ? "badge-warning"
                                    : "badge-success"
                            }`}
                            title={item.error ?? undefined}
                          >
                            {item.error
                              ? "拉取失败"
                              : !item.enabled
                                ? "规则关闭"
                                : item.needs_restock
                                  ? "低于水位"
                                  : "水位充足"}
                          </span>
                          {item.error && (
                            <div className="field-error" style={{ fontSize: 11.5, marginTop: 4 }}>
                              {item.error}
                            </div>
                          )}
                        </td>
                        <td className="mono">
                          {item.current_qty ?? "-"} / {item.locked_qty ?? "-"} /{" "}
                          {item.sold_qty ?? "-"}
                        </td>
                        <td className="mono">
                          {item.min_stock} / {item.target_stock}
                        </td>
                        <td className="mono">{item.planned_push}</td>
                        <td className="mono">
                          {item.local_in_stock_16688} / {item.local_dispatched_unused_16688}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="field-hint">暂未配置 16688 自动补卡规则。</p>
            )}

            {channelRestockResult && (
              <div className="inset-panel stack-sm">
                <div className="row-between" style={{ flexWrap: "wrap", gap: 6 }}>
                  <strong style={{ fontSize: 13 }}>
                    最近一次 16688 {channelRestockResult.dry_check ? "试跑" : "补卡"}结果
                  </strong>
                  <span className="mono" style={{ fontSize: 12, color: "var(--ink-3)" }}>
                    检查 {channelRestockResult.checked} · 补货 {channelRestockResult.restocked} ·
                    推卡 {channelRestockResult.total_pushed}
                    {channelRestockResult.stock_conflicts
                      ? ` · 库存冲突 ${channelRestockResult.stock_conflicts}`
                      : ""}
                  </span>
                </div>
                {channelRestockResult.errors.length > 0 && (
                  <p className="field-error">{channelRestockResult.errors.join("；")}</p>
                )}
                {channelRestockResult.items.length > 0 && (
                  <div className="automation-table-wrap">
                    <table className="data-table">
                      <thead>
                        <tr>
                          <th>SKU / 编号</th>
                          <th>动作</th>
                          <th>现存 / 计划 / 实推</th>
                          <th>批次 / 说明</th>
                        </tr>
                      </thead>
                      <tbody>
                        {channelRestockResult.items.map((row, idx) => (
                          <tr key={`${row.sku_key}-${idx}`}>
                            <td className="mono">
                              {row.sku_key}
                              {row.goods_no ? ` · ${row.goods_no}` : ""}
                            </td>
                            <td className="mono">{row.action}</td>
                            <td className="mono">
                              {row.current_qty ?? "-"} / {row.planned_push ?? 0} / {row.pushed_count}
                            </td>
                            <td className="mono">{row.error || row.batch_id || "-"}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            )}
          </section>
        </div>
      )}

      {subTab === "mappings" && (
        <div className="stack-lg">
          <section className="card stack-sm" aria-label="查询阿奇索远端宝贝 SKU">
            <h2 className="card-title" style={{ fontSize: 16 }}>
              1. 查询淘宝远端宝贝 SKU
            </h2>
            <form className="automation-inline-form" onSubmit={handleQueryRemoteSkus}>
              <input
                type="text"
                className="input mono"
                placeholder="输入淘宝宝贝 NumIid，多个用英文逗号分隔"
                aria-label="淘宝宝贝 NumIid"
                value={remoteNumIids}
                onChange={(event) => setRemoteNumIids(event.target.value)}
              />
              <button
                type="submit"
                className={`btn btn-secondary ${remoteSkusLoading ? "is-loading" : ""}`}
                disabled={remoteSkusLoading}
              >
                <span>{remoteSkusLoading ? "查询中..." : "拉取远端 SKU"}</span>
              </button>
            </form>
            {remoteSkusError && (
              <div className="callout callout-warning" role="alert">
                <CastoriceIcon name="alert" size={18} className="callout-icon" />
                <span>{remoteSkusError}</span>
              </div>
            )}
            {remoteSkus && remoteSkus.length > 0 && (
              <div className="automation-table-wrap">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>NumIid</th>
                      <th>SkuId</th>
                      <th>规格属性</th>
                      <th>商家编码</th>
                      <th>价格 / 库存</th>
                      <th>操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {remoteSkus.map((item, idx) => (
                      <tr key={`${item.NumIid ?? ""}-${item.SkuId ?? idx}`}>
                        <td className="mono">{String(item.NumIid ?? "-")}</td>
                        <td className="mono">{String(item.SkuId ?? "-")}</td>
                        <td>{item.PropertiesName || item.Properties || "-"}</td>
                        <td className="mono">{item.OuterId || "-"}</td>
                        <td className="mono">
                          {item.Price ?? "-"} / {item.Quantity ?? "-"}
                        </td>
                        <td>
                          <button
                            type="button"
                            className="btn btn-secondary btn-sm"
                            onClick={() => handlePrefillMappingFromRemote(item)}
                          >
                            填入映射表单
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          <section className="card stack-sm" aria-label="SKU 映射规则管理">
            <div className="row-between">
              <h2 className="card-title" style={{ fontSize: 16 }}>
                2. SKU 映射规则（数据库 + 环境变量）
              </h2>
              <button
                type="button"
                className="btn btn-secondary btn-sm"
                disabled={mappingsLoading}
                onClick={() => void loadMappings()}
              >
                刷新映射
              </button>
            </div>

            <form className="inset-panel stack-sm" onSubmit={handleCreateMapping}>
              <strong style={{ fontSize: 13.5 }}>新增数据库映射规则</strong>
              <div className="automation-form-grid">
                <label className="field-group">
                  <span className="field-label">目标商品规格</span>
                  <select
                    className="select"
                    value={mapSkuKey}
                    onChange={(event) => setMapSkuKey(event.target.value as SkuKey)}
                  >
                    <option value="ultra_shared_slot">共享车位（ultra_shared_slot）</option>
                    <option value="ultra_exclusive_admin">独享整组（ultra_exclusive_admin）</option>
                  </select>
                </label>
                <label className="field-group">
                  <span className="field-label">宝贝 ID（num_iid）</span>
                  <input
                    type="text"
                    className="input mono"
                    value={mapNumIid}
                    onChange={(event) => setMapNumIid(event.target.value)}
                  />
                </label>
                <label className="field-group">
                  <span className="field-label">淘宝 SKU ID（sku_id）</span>
                  <input
                    type="text"
                    className="input mono"
                    value={mapSkuId}
                    onChange={(event) => setMapSkuId(event.target.value)}
                  />
                </label>
                <label className="field-group">
                  <span className="field-label">外部宝贝编码（outer_iid）</span>
                  <input
                    type="text"
                    className="input mono"
                    value={mapOuterIid}
                    onChange={(event) => setMapOuterIid(event.target.value)}
                  />
                </label>
                <label className="field-group">
                  <span className="field-label">外部 SKU 编码（outer_sku_id）</span>
                  <input
                    type="text"
                    className="input mono"
                    value={mapOuterSkuId}
                    onChange={(event) => setMapOuterSkuId(event.target.value)}
                  />
                </label>
                <label className="field-group">
                  <span className="field-label">规格属性包含关键字</span>
                  <input
                    type="text"
                    className="input"
                    value={mapPropsContains}
                    onChange={(event) => setMapPropsContains(event.target.value)}
                  />
                </label>
              </div>
              <div className="automation-inline-form">
                <input
                  type="text"
                  className="input"
                  placeholder="备注（可选）"
                  value={mapNote}
                  onChange={(event) => setMapNote(event.target.value)}
                />
                <label className="admin-inline-check">
                  <input
                    type="checkbox"
                    checked={mapEnabled}
                    onChange={(event) => setMapEnabled(event.target.checked)}
                  />
                  <span>立即启用</span>
                </label>
                <button
                  type="submit"
                  className={`btn btn-primary ${creatingMapping ? "is-loading" : ""}`}
                  disabled={creatingMapping}
                >
                  <span>{creatingMapping ? "保存中..." : "新增映射"}</span>
                </button>
              </div>
            </form>

            {mappingsError ? (
              <div className="callout callout-danger" role="alert">
                <span>{mappingsError}</span>
              </div>
            ) : mappingsData ? (
              <div className="stack-sm">
                <strong style={{ fontSize: 13 }}>数据库规则（{mappingsData.items.length}）</strong>
                {mappingsData.items.length === 0 ? (
                  <p className="field-hint">暂无数据库 SKU 映射规则。</p>
                ) : (
                  <div className="automation-table-wrap">
                    <table className="data-table">
                      <thead>
                        <tr>
                          <th>ID</th>
                          <th>目标规格</th>
                          <th>匹配条件</th>
                          <th>状态</th>
                          <th>备注</th>
                          <th>操作</th>
                        </tr>
                      </thead>
                      <tbody>
                        {mappingsData.items.map((rule) => (
                          <tr key={rule.mapping_id}>
                            <td className="mono">{rule.mapping_id}</td>
                            <td>{getSkuShortLabel(rule.sku_key)}</td>
                            <td className="mono" style={{ fontSize: 12 }}>
                              {[
                                rule.num_iid && `num_iid=${rule.num_iid}`,
                                rule.sku_id && `sku_id=${rule.sku_id}`,
                                rule.outer_iid && `outer_iid=${rule.outer_iid}`,
                                rule.outer_sku_id && `outer_sku_id=${rule.outer_sku_id}`,
                                rule.sku_props_contains && `props~${rule.sku_props_contains}`,
                              ]
                                .filter(Boolean)
                                .join(" · ") || "-"}
                            </td>
                            <td>
                              <span className={`badge ${rule.enabled ? "badge-success" : "badge-neutral"}`}>
                                {rule.enabled ? "启用" : "停用"}
                              </span>
                            </td>
                            <td>{rule.note || "-"}</td>
                            <td>
                              <button
                                type="button"
                                className="btn btn-danger btn-sm"
                                onClick={() => setDeleteTargetMapping(rule)}
                              >
                                删除
                              </button>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}

                {mappingsData.env_rules.length > 0 && (
                  <>
                    <strong style={{ fontSize: 13 }}>
                      环境变量只读规则（{mappingsData.env_rules.length}）
                    </strong>
                    <div className="automation-table-wrap">
                      <table className="data-table">
                        <thead>
                          <tr>
                            <th>目标规格</th>
                            <th>匹配条件</th>
                            <th>状态</th>
                          </tr>
                        </thead>
                        <tbody>
                          {mappingsData.env_rules.map((rule, idx) => (
                            <tr key={`env-${idx}`}>
                              <td>{getSkuShortLabel(rule.sku_key)}</td>
                              <td className="mono" style={{ fontSize: 12 }}>
                                {[
                                  rule.num_iid && `num_iid=${rule.num_iid}`,
                                  rule.sku_id && `sku_id=${rule.sku_id}`,
                                  rule.outer_iid && `outer_iid=${rule.outer_iid}`,
                                  rule.outer_sku_id && `outer_sku_id=${rule.outer_sku_id}`,
                                  rule.sku_props_contains && `props~${rule.sku_props_contains}`,
                                ]
                                  .filter(Boolean)
                                  .join(" · ") || "-"}
                              </td>
                              <td>
                                <span className="badge badge-neutral">只读（env）</span>
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </>
                )}
              </div>
            ) : null}
          </section>

          <section className="card stack-sm" aria-label="手工批量推送阿奇索卡密">
            <h2 className="card-title" style={{ fontSize: 16 }}>
              3. 手工推送兑换码到阿奇索卡库
            </h2>
            <form className="stack-sm" onSubmit={handlePushCardsSubmit}>
              <div className="automation-form-grid">
                <label className="field-group">
                  <span className="field-label">商品规格</span>
                  <select
                    className="select"
                    value={pushSkuKey}
                    onChange={(event) => setPushSkuKey(event.target.value as SkuKey)}
                  >
                    <option value="ultra_shared_slot">共享车位</option>
                    <option value="ultra_exclusive_admin">独享整组</option>
                  </select>
                </label>
                <label className="field-group">
                  <span className="field-label">卡种 ID（cpc_id，≥ 1）</span>
                  <input
                    type="number"
                    min="1"
                    className="input mono"
                    required
                    value={pushCpcId}
                    onChange={(event) => setPushCpcId(event.target.value)}
                  />
                </label>
                <label className="field-group">
                  <span className="field-label">推送数量（1~500）</span>
                  <input
                    type="number"
                    min="1"
                    max="500"
                    className="input mono"
                    required
                    value={pushCount}
                    onChange={(event) => setPushCount(event.target.value)}
                  />
                </label>
                <label className="field-group">
                  <span className="field-label">批次标签（可选）</span>
                  <input
                    type="text"
                    className="input"
                    placeholder="例如：手工紧急补库"
                    value={pushBatchLabel}
                    onChange={(event) => setPushBatchLabel(event.target.value)}
                  />
                </label>
              </div>
              <div className="row" style={{ gap: 12, flexWrap: "wrap" }}>
                <label className="admin-inline-check">
                  <input
                    type="checkbox"
                    checked={pushUseFirst}
                    onChange={(event) => setPushUseFirst(event.target.checked)}
                  />
                  <span>优先发新推送的卡（is_use_first）</span>
                </label>
                <label className="admin-inline-check">
                  <input
                    type="checkbox"
                    checked={pushAllowReplace}
                    onChange={(event) => setPushAllowReplace(event.target.checked)}
                  />
                  <span>允许替换重复卡密（allow_replace）</span>
                </label>
                <button
                  type="submit"
                  className="btn btn-primary"
                  disabled={!pushFormValid || pushingCards}
                >
                  生成并推送到阿奇索
                </button>
              </div>
            </form>

            {pushResult && (
              <div className="inset-panel stack-sm">
                <div className="row-between" style={{ gap: 8, flexWrap: "wrap" }}>
                  <strong style={{ fontSize: 13 }}>
                    已推送批次 <span className="mono">{pushResult.batch_id}</span>（共{" "}
                    {pushResult.count} 张）
                  </strong>
                  {pushResult.codes.length > 0 && (
                    <CopyButton
                      text={pushResult.codes.join("\n")}
                      label="复制本批兑换码"
                      toastMessage="已复制本批推送的兑换码"
                      variant="secondary"
                    />
                  )}
                </div>
                <pre className="code-output">{pushResult.codes.join("\n")}</pre>
              </div>
            )}
          </section>
        </div>
      )}

      {subTab === "events" && (
        <div className="stack-lg">
          <div className="automation-two-col">
            <section className="card stack-sm" aria-label="一键拉单补发">
              <h2 className="card-title" style={{ fontSize: 16 }}>
                一键拉单补发
              </h2>
              <form className="stack-sm" onSubmit={handleSyncOrders}>
                <div className="automation-form-grid">
                  <label className="field-group">
                    <span className="field-label">回溯分钟（1~10080）</span>
                    <input
                      type="number"
                      min="1"
                      max="10080"
                      className="input mono"
                      value={syncLookback}
                      onChange={(event) => setSyncLookback(event.target.value)}
                    />
                  </label>
                  <label className="field-group">
                    <span className="field-label">每页条数（1~100）</span>
                    <input
                      type="number"
                      min="1"
                      max="100"
                      className="input mono"
                      value={syncPageSize}
                      onChange={(event) => setSyncPageSize(event.target.value)}
                    />
                  </label>
                  <label className="field-group">
                    <span className="field-label">最大页数（1~20）</span>
                    <input
                      type="number"
                      min="1"
                      max="20"
                      className="input mono"
                      value={syncMaxPages}
                      onChange={(event) => setSyncMaxPages(event.target.value)}
                    />
                  </label>
                </div>
                <button
                  type="submit"
                  className={`btn btn-primary ${syncingOrders ? "is-loading" : ""}`}
                  disabled={syncingOrders}
                >
                  <span>{syncingOrders ? "正在拉单补发..." : "立即拉单补发"}</span>
                </button>
              </form>
              {syncResult && (
                <div className="inset-panel stack-sm">
                  <div className="mono" style={{ fontSize: 12.5 }}>
                    起点：{syncResult.modify_time_start} ｜ 拉取 {syncResult.fetched} · 补发{" "}
                    {syncResult.fulfilled} · 跳过 {syncResult.skipped} · 失败 {syncResult.failed}
                  </div>
                  {syncResult.items.length > 0 && (
                    <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
                      {syncResult.items.flatMap((entry) =>
                        (entry.codes || []).map((codeItem) => (
                          <button
                            key={codeItem}
                            type="button"
                            className="btn btn-secondary btn-sm mono"
                            onClick={() => onSelectCode(codeItem)}
                          >
                            {codeItem}
                          </button>
                        ))
                      )}
                    </div>
                  )}
                </div>
              )}
            </section>

            <section className="card stack-sm" aria-label="按单号重试发货">
              <h2 className="card-title" style={{ fontSize: 16 }}>
                按淘宝单号重试发货
              </h2>
              <form className="stack-sm" onSubmit={handleRetryAgisoOrder}>
                <label className="field-group">
                  <span className="field-label">淘宝订单号（tid）</span>
                  <input
                    type="text"
                    className="input mono"
                    placeholder="输入淘宝主订单号 tid"
                    value={retryTidInput}
                    onChange={(event) => setRetryTidInput(event.target.value)}
                  />
                </label>
                <label className="admin-inline-check">
                  <input
                    type="checkbox"
                    checked={retryFetchFromAgiso}
                    onChange={(event) => setRetryFetchFromAgiso(event.target.checked)}
                  />
                  <span>重拉阿奇索最新订单详情（fetch_from_agiso）</span>
                </label>
                <button
                  type="submit"
                  className={`btn btn-warm ${retryingOrder ? "is-loading" : ""}`}
                  disabled={retryingOrder}
                >
                  <span>{retryingOrder ? "重试中..." : "按单号重试发货"}</span>
                </button>
              </form>
              {retryResult && (
                <div className="inset-panel stack-sm">
                  <div className="mono" style={{ fontSize: 12.5 }}>
                    结果：{retryResult.status}
                    {retryResult.trade_status ? ` · 交易状态 ${retryResult.trade_status}` : ""}
                  </div>
                  {retryResult.codes && retryResult.codes.length > 0 && (
                    <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
                      {retryResult.codes.map((codeItem) => (
                        <button
                          key={codeItem}
                          type="button"
                          className="btn btn-secondary btn-sm mono"
                          onClick={() => onSelectCode(codeItem)}
                        >
                          {codeItem}
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </section>
          </div>

          <section className="card stack-sm" aria-label="阿奇索事件流与发货明细">
            <div className="row-between" style={{ flexWrap: "wrap", gap: 8 }}>
              <h2 className="card-title" style={{ fontSize: 16 }}>
                阿奇索事件流
              </h2>
              <span className="badge badge-neutral">共 {eventsData?.total ?? 0} 条</span>
            </div>

            <form className="automation-events-filter" onSubmit={handleEventsSearch}>
              <input
                type="text"
                className="input mono"
                placeholder="按淘宝单号 tid 筛选（可同时查发货明细）"
                aria-label="按单号 tid 筛选"
                value={eventsTidDraft}
                onChange={(event) => setEventsTidDraft(event.target.value)}
              />
              <input
                type="number"
                className="input mono"
                placeholder="AoPic 类型号"
                aria-label="按 AoPic 筛选"
                value={eventsAopicDraft}
                onChange={(event) => setEventsAopicDraft(event.target.value)}
              />
              <input
                type="text"
                className="input"
                placeholder="状态（如 processed / failed）"
                aria-label="按事件状态筛选"
                value={eventsStatusDraft}
                onChange={(event) => setEventsStatusDraft(event.target.value)}
              />
              <button type="submit" className="btn btn-primary">
                查询事件
              </button>
            </form>

            {eventsData?.deliveries && eventsData.deliveries.length > 0 && (
              <div className="inset-panel stack-sm">
                <strong style={{ fontSize: 13.5 }}>
                  订单 <span className="mono">{eventsQuery.tid}</span> 发货明细（点击兑换码打开详情）
                </strong>
                <div className="automation-table-wrap">
                  <table className="data-table">
                    <thead>
                      <tr>
                        <th>兑换码</th>
                        <th>子单 / 序号</th>
                        <th>规格</th>
                        <th>状态</th>
                        <th>回执（建单/卡密/旺旺/发货）</th>
                        <th>异常</th>
                      </tr>
                    </thead>
                    <tbody>
                      {eventsData.deliveries.map((item) => (
                        <tr key={item.delivery_key}>
                          <td>
                            <button
                              type="button"
                              className="btn btn-ghost btn-sm mono"
                              onClick={() => onSelectCode(item.code)}
                            >
                              {item.code}
                            </button>
                          </td>
                          <td className="mono">
                            {item.oid} #{item.seq}
                          </td>
                          <td className="mono">{item.sku_key}</td>
                          <td>
                            <span className="badge badge-info">{item.delivery_status}</span>
                          </td>
                          <td className="mono" style={{ fontSize: 12 }}>
                            {item.result_created ? "✓" : "-"} / {item.card_sent ? "✓" : "-"} /{" "}
                            {item.ww_sent ? "✓" : "-"} / {item.dummy_sent ? "✓" : "-"}
                          </td>
                          <td style={{ color: "var(--danger-ink, var(--ink-2))" }}>
                            {item.last_error || "-"}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}

            {eventsError ? (
              <div className="callout callout-danger" role="alert">
                <span>{eventsError}</span>
              </div>
            ) : eventsLoading && !eventsData ? (
              <div className="skeleton" style={{ height: 140, width: "100%" }} />
            ) : eventsData && eventsData.items.length > 0 ? (
              <>
                <div className="automation-table-wrap">
                  <table className="data-table">
                    <thead>
                      <tr>
                        <th>时间</th>
                        <th>来源 / AoPic</th>
                        <th>单号（tid / oid）</th>
                        <th>状态</th>
                        <th>详情</th>
                      </tr>
                    </thead>
                    <tbody>
                      {eventsData.items.map((ev) => (
                        <tr key={ev.event_id}>
                          <td className="mono" style={{ whiteSpace: "nowrap", fontSize: 12 }}>
                            {formatDateTime(ev.created_at)}
                          </td>
                          <td className="mono" style={{ fontSize: 12 }}>
                            {ev.source}
                            {ev.aopic !== null ? ` · ${ev.aopic}` : ""}
                          </td>
                          <td className="mono" style={{ fontSize: 12 }}>
                            {ev.tid || "-"}
                            {ev.oid ? ` / ${ev.oid}` : ""}
                          </td>
                          <td>
                            <span
                              className={`badge ${
                                ev.status.includes("fail") || ev.status.includes("error")
                                  ? "badge-danger"
                                  : ev.status.includes("ok") || ev.status.includes("processed")
                                    ? "badge-success"
                                    : "badge-neutral"
                              }`}
                            >
                              {ev.status}
                            </span>
                          </td>
                          <td style={{ fontSize: 12.5 }}>{ev.detail || ev.payload_summary || "-"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <div className="row-between admin-pagination-bar">
                  <span className="mono" style={{ fontSize: 12.5, color: "var(--ink-3)" }}>
                    第 {eventsQuery.page} / {totalEventsPages} 页
                  </span>
                  <div className="row" style={{ gap: 8 }}>
                    <button
                      type="button"
                      className="btn btn-secondary btn-sm"
                      disabled={eventsQuery.page <= 1 || eventsLoading}
                      onClick={() =>
                        setEventsQuery((prev) => ({ ...prev, page: Math.max(1, prev.page - 1) }))
                      }
                    >
                      上一页
                    </button>
                    <button
                      type="button"
                      className="btn btn-secondary btn-sm"
                      disabled={eventsQuery.page >= totalEventsPages || eventsLoading}
                      onClick={() =>
                        setEventsQuery((prev) => ({
                          ...prev,
                          page: Math.min(totalEventsPages, prev.page + 1),
                        }))
                      }
                    >
                      下一页
                    </button>
                  </div>
                </div>
              </>
            ) : (
              <p className="field-hint">当前筛选条件下暂无事件记录。</p>
            )}
          </section>
        </div>
      )}

      <ConfirmDialog
        open={agisoRestockConfirmOpen}
        title="确认执行阿奇索自动补卡"
        confirmLabel="确认立即补卡"
        busy={agisoRestocking}
        onConfirm={() => void runAgisoRestock(false)}
        onClose={() => setAgisoRestockConfirmOpen(false)}
      >
        <p>
          系统将按当前低于水位的阿奇索规则批量生成新兑换码并推送至对应淘宝卡种，请确认规则配置无误。
        </p>
      </ConfirmDialog>

      <ConfirmDialog
        open={channelRestockConfirmOpen}
        title="确认执行 16688 自动补卡"
        confirmLabel="确认立即补卡"
        busy={channelRestocking}
        onConfirm={() => void runChannelRestock(false)}
        onClose={() => setChannelRestockConfirmOpen(false)}
      >
        <p>
          系统将按当前低于水位的 16688 规则批量生成兑换码并上传至 16688 卡密库（
          {channelSyncSold ? "先同步已售记录" : "跳过已售同步"}）。
        </p>
      </ConfirmDialog>

      <ConfirmDialog
        open={Boolean(deleteTargetMapping)}
        title="确认删除 SKU 映射规则"
        confirmLabel="确认删除"
        confirmVariant="danger"
        busy={deletingMapping}
        onConfirm={() => void handleDeleteMappingConfirm()}
        onClose={() => setDeleteTargetMapping(null)}
      >
        <p>
          删除数据库映射规则 #{deleteTargetMapping?.mapping_id}（
          {deleteTargetMapping ? getSkuShortLabel(deleteTargetMapping.sku_key) : ""}
          ）后，新订单将不再匹配此规则。
        </p>
      </ConfirmDialog>

      <ConfirmDialog
        open={pushConfirmOpen}
        title="确认手工推卡到阿奇索"
        confirmLabel="确认生成并推送"
        busy={pushingCards}
        onConfirm={() => void handlePushCardsConfirm()}
        onClose={() => setPushConfirmOpen(false)}
      >
        <p>
          即将为 <strong className="mono">{getSkuShortLabel(pushSkuKey)}</strong> 生成{" "}
          <strong className="mono">{parsedPushCount}</strong> 张淘宝兑换码并推送到阿奇索卡种{" "}
          <strong className="mono">{parsedPushCpcId}</strong>。
        </p>
      </ConfirmDialog>
    </div>
  );
}
