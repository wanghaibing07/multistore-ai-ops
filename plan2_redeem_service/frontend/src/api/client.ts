import type {
  AdminAgisoEventsResponse,
  AdminAgisoPushCardsRequest,
  AdminAgisoPushCardsResponse,
  AdminAgisoRemoteSkusResponse,
  AdminAgisoRestockResponse,
  AdminAgisoRetryOrderResponse,
  AdminAgisoSkuMapping,
  AdminAgisoSkuMappingsResponse,
  AdminAgisoStatus,
  AdminAgisoStockResponse,
  AdminAgisoSyncOrdersRequest,
  AdminAgisoSyncOrdersResponse,
  AdminBatchCodeRequest,
  AdminBatchCodeResponse,
  AdminChannel16688RestockResponse,
  AdminChannel16688StockResponse,
  AdminCodeDetail,
  AdminCodeListResponse,
  AdminCreateAgisoSkuMappingRequest,
  AdminIssueCodeRequest,
  AdminIssueCodeResponse,
  AdminOverviewResponse,
  AdminSkuStatus,
  AdminTestEmailResponse,
  AdminUpdateSkuRequest,
  AdminUpstreamRefundRequest,
  ApiErrorEnvelope,
  PublicCodeResponse,
  RedeemExclusiveRequest,
  RedeemSlotRequest,
  SkuKey,
  SubmitPublicWarrantyRequest,
} from "../types/api";

export class ApiError extends Error {
  status: number;
  errorCode: string;

  constructor(status: number, errorCode: string, message: string) {
    super(message);
    this.status = status;
    this.errorCode = errorCode;
  }
}

async function requestJson<T>(url: string, init: RequestInit = {}): Promise<T> {
  let response: Response;
  try {
    response = await fetch(url, init);
  } catch {
    throw new ApiError(0, "NETWORK_ERROR", "无法连接服务器，请检查网络或确认后端服务已启动");
  }

  const contentType = response.headers.get("content-type") || "";
  const isJson = contentType.includes("application/json");
  const body = isJson ? ((await response.json()) as unknown) : null;

  if (!response.ok) {
    const raw = body as
      | (Partial<ApiErrorEnvelope> & {
          detail?: Partial<ApiErrorEnvelope> | string | Array<{ msg?: string }>;
        })
      | null;
    let errorCode = raw?.error_code;
    let message = raw?.message;

    if ((!errorCode || !message) && raw?.detail) {
      if (typeof raw.detail === "string") {
        message = message || raw.detail;
      } else if (Array.isArray(raw.detail)) {
        const firstMsg = raw.detail.find((item) => item?.msg)?.msg;
        if (firstMsg) message = message || firstMsg;
        errorCode = errorCode || "VALIDATION_ERROR";
      } else {
        errorCode = errorCode || raw.detail.error_code;
        message = message || raw.detail.message;
      }
    }

    throw new ApiError(
      response.status,
      errorCode || `HTTP_${response.status}`,
      message || `请求失败（HTTP ${response.status}）`
    );
  }

  return body as T;
}

function authHeaders(token: string, withJson = false): HeadersInit {
  const headers: Record<string, string> = {
    Authorization: `Bearer ${token}`,
  };
  if (withJson) {
    headers["Content-Type"] = "application/json";
  }
  return headers;
}

export async function getPublicCode(code: string): Promise<PublicCodeResponse> {
  return requestJson<PublicCodeResponse>(`/api/v1/public/codes/${encodeURIComponent(code)}`);
}

export async function redeemPublicCode(
  code: string,
  payload: RedeemSlotRequest | RedeemExclusiveRequest
): Promise<PublicCodeResponse> {
  return requestJson<PublicCodeResponse>(
    `/api/v1/public/codes/${encodeURIComponent(code)}/redeem`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }
  );
}

export async function refreshPublicCode(code: string): Promise<PublicCodeResponse> {
  return requestJson<PublicCodeResponse>(
    `/api/v1/public/codes/${encodeURIComponent(code)}/refresh`,
    { method: "POST" }
  );
}

export async function submitPublicWarranty(
  code: string,
  payload: SubmitPublicWarrantyRequest
): Promise<PublicCodeResponse> {
  return requestJson<PublicCodeResponse>(
    `/api/v1/public/codes/${encodeURIComponent(code)}/warranty`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }
  );
}

export async function getAdminOverview(token: string): Promise<AdminOverviewResponse> {
  return requestJson<AdminOverviewResponse>("/api/v1/admin/overview", {
    headers: authHeaders(token),
  });
}

export async function refreshAdminUpstream(token: string): Promise<AdminOverviewResponse> {
  return requestJson<AdminOverviewResponse>("/api/v1/admin/upstream/refresh", {
    method: "POST",
    headers: authHeaders(token),
  });
}

export async function issueAdminCode(
  token: string,
  payload: AdminIssueCodeRequest
): Promise<AdminIssueCodeResponse> {
  return requestJson<AdminIssueCodeResponse>("/api/v1/admin/codes/issue", {
    method: "POST",
    headers: authHeaders(token, true),
    body: JSON.stringify(payload),
  });
}

export async function batchAdminCodes(
  token: string,
  payload: AdminBatchCodeRequest
): Promise<AdminBatchCodeResponse> {
  return requestJson<AdminBatchCodeResponse>("/api/v1/admin/codes/batch", {
    method: "POST",
    headers: authHeaders(token, true),
    body: JSON.stringify(payload),
  });
}

export interface ListCodesParams {
  status?: string;
  channel?: string;
  sku_key?: string;
  q?: string;
  error_code?: string;
  start_date?: string;
  end_date?: string;
  page?: number;
  page_size?: number;
}

export async function listAdminCodes(
  token: string,
  params: ListCodesParams
): Promise<AdminCodeListResponse> {
  const search = new URLSearchParams();
  if (params.status) search.set("status", params.status);
  if (params.channel) search.set("channel", params.channel);
  if (params.sku_key) search.set("sku_key", params.sku_key);
  if (params.q) search.set("q", params.q);
  if (params.error_code) search.set("error_code", params.error_code);
  if (params.start_date) search.set("start_date", params.start_date);
  if (params.end_date) search.set("end_date", params.end_date);
  if (params.page) search.set("page", String(params.page));
  if (params.page_size) search.set("page_size", String(params.page_size));

  return requestJson<AdminCodeListResponse>(`/api/v1/admin/codes?${search.toString()}`, {
    headers: authHeaders(token),
  });
}

export async function getAdminCodeDetail(token: string, code: string): Promise<AdminCodeDetail> {
  return requestJson<AdminCodeDetail>(`/api/v1/admin/codes/${encodeURIComponent(code)}`, {
    headers: authHeaders(token),
  });
}

export async function fetchAdminWarrantyScreenshot(
  token: string,
  code: string
): Promise<{ url: string; revoke: () => void }> {
  const response = await fetch(
    `/api/v1/admin/codes/${encodeURIComponent(code)}/warranty-screenshot`,
    { headers: authHeaders(token) }
  );
  if (!response.ok) {
    const contentType = response.headers.get("content-type") || "";
    if (contentType.includes("application/json")) {
      const raw = (await response.json().catch(() => null)) as
        | (Partial<ApiErrorEnvelope> & { detail?: Partial<ApiErrorEnvelope> | string })
        | null;
      const detailObj = raw?.detail && typeof raw.detail === "object" ? raw.detail : undefined;
      const errorCode = raw?.error_code || detailObj?.error_code || `HTTP_${response.status}`;
      const message =
        raw?.message ||
        detailObj?.message ||
        (typeof raw?.detail === "string" ? raw.detail : undefined) ||
        "无法加载保修截图";
      throw new ApiError(response.status, errorCode, message);
    }
    throw new ApiError(
      response.status,
      response.status === 401 ? "UNAUTHORIZED" : `HTTP_${response.status}`,
      response.status === 401 ? "登录凭据已失效，请重新登录" : "无法加载保修截图"
    );
  }
  const blob = await response.blob();
  const objectUrl = URL.createObjectURL(blob);
  return {
    url: objectUrl,
    revoke: () => URL.revokeObjectURL(objectUrl),
  };
}

export async function pollAdminCode(token: string, code: string): Promise<AdminCodeDetail> {
  return requestJson<AdminCodeDetail>(`/api/v1/admin/codes/${encodeURIComponent(code)}/poll`, {
    method: "POST",
    headers: authHeaders(token),
  });
}

export async function revokeAdminCode(
  token: string,
  code: string,
  reason: string,
  force = false
): Promise<AdminCodeDetail> {
  return requestJson<AdminCodeDetail>(`/api/v1/admin/codes/${encodeURIComponent(code)}/revoke`, {
    method: "POST",
    headers: authHeaders(token, true),
    body: JSON.stringify({ reason, force }),
  });
}

export async function retryAdminCode(
  token: string,
  code: string,
  payload: {
    override_price_limit?: boolean;
    note?: string;
    merchant_verified_order_no?: string;
    merchant_verified_recipient_email?: string;
    force_new_order?: boolean;
    new_buyer_email?: string;
  }
): Promise<AdminCodeDetail> {
  return requestJson<AdminCodeDetail>(`/api/v1/admin/codes/${encodeURIComponent(code)}/retry`, {
    method: "POST",
    headers: authHeaders(token, true),
    body: JSON.stringify(payload),
  });
}

export async function recordAdminUpstreamRefund(
  token: string,
  code: string,
  payload: AdminUpstreamRefundRequest
): Promise<AdminCodeDetail> {
  return requestJson<AdminCodeDetail>(`/api/v1/admin/codes/${encodeURIComponent(code)}/upstream-refund`, {
    method: "POST",
    headers: authHeaders(token, true),
    body: JSON.stringify(payload),
  });
}

export async function ackAdminWarranty(
  token: string,
  code: string,
  note: string
): Promise<AdminCodeDetail> {
  return requestJson<AdminCodeDetail>(
    `/api/v1/admin/codes/${encodeURIComponent(code)}/warranty-ack`,
    {
      method: "POST",
      headers: authHeaders(token, true),
      body: JSON.stringify({ note }),
    }
  );
}

export async function forwardAdminWarranty(
  token: string,
  code: string
): Promise<AdminCodeDetail> {
  return requestJson<AdminCodeDetail>(
    `/api/v1/admin/codes/${encodeURIComponent(code)}/warranty-forward`,
    {
      method: "POST",
      headers: authHeaders(token),
    }
  );
}

export async function updateAdminSku(
  token: string,
  skuKey: SkuKey,
  payload: AdminUpdateSkuRequest
): Promise<AdminSkuStatus> {
  return requestJson<AdminSkuStatus>(
    `/api/v1/admin/settings/skus/${encodeURIComponent(skuKey)}`,
    {
      method: "PATCH",
      headers: authHeaders(token, true),
      body: JSON.stringify(payload),
    }
  );
}

export async function sendAdminTestEmail(token: string): Promise<AdminTestEmailResponse> {
  return requestJson<AdminTestEmailResponse>("/api/v1/admin/notifications/test-email", {
    method: "POST",
    headers: authHeaders(token),
  });
}

export interface ExportReconciliationParams {
  start_date?: string;
  end_date?: string;
  channel?: string;
  exclude_inactive?: boolean;
}

export async function exportAdminReconciliationCsv(
  token: string,
  params: ExportReconciliationParams = {}
): Promise<string> {
  const search = new URLSearchParams();
  search.set("format", "csv");
  if (params.start_date) search.set("start_date", params.start_date);
  if (params.end_date) search.set("end_date", params.end_date);
  if (params.channel) search.set("channel", params.channel);
  if (params.exclude_inactive) search.set("exclude_inactive", "true");

  let response: Response;
  try {
    response = await fetch(`/api/v1/admin/reconciliation-export?${search.toString()}`, {
      headers: authHeaders(token),
    });
  } catch {
    throw new ApiError(0, "NETWORK_ERROR", "无法连接服务器，导出对账 CSV 失败");
  }

  if (!response.ok) {
    const contentType = response.headers.get("content-type") || "";
    if (contentType.includes("application/json")) {
      const raw = (await response.json().catch(() => null)) as
        | (Partial<ApiErrorEnvelope> & { detail?: Partial<ApiErrorEnvelope> | string })
        | null;
      const detailObj = raw?.detail && typeof raw.detail === "object" ? raw.detail : undefined;
      throw new ApiError(
        response.status,
        raw?.error_code || detailObj?.error_code || `HTTP_${response.status}`,
        raw?.message ||
          detailObj?.message ||
          (typeof raw?.detail === "string" ? raw.detail : undefined) ||
          `导出对账 CSV 失败（HTTP ${response.status}）`
      );
    }
    throw new ApiError(
      response.status,
      response.status === 401 ? "UNAUTHORIZED" : `HTTP_${response.status}`,
      response.status === 401 ? "登录凭据已失效，请重新登录" : `导出对账 CSV 失败（HTTP ${response.status}）`
    );
  }

  const disposition = response.headers.get("content-disposition") || "";
  const match = /filename="?([^";]+)"?/i.exec(disposition);
  const filename = match?.[1] || `plan2-reconciliation-${new Date().toISOString().slice(0, 10)}.csv`;
  const blob = await response.blob();
  const objectUrl = URL.createObjectURL(blob);
  try {
    const link = document.createElement("a");
    link.href = objectUrl;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
  } finally {
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
  }
  return filename;
}

export async function getAdminAgisoStatus(token: string): Promise<AdminAgisoStatus> {
  return requestJson<AdminAgisoStatus>("/api/v1/admin/agiso/status", {
    headers: authHeaders(token),
  });
}

export async function listAdminAgisoSkuMappings(
  token: string
): Promise<AdminAgisoSkuMappingsResponse> {
  return requestJson<AdminAgisoSkuMappingsResponse>("/api/v1/admin/agiso/sku-mappings", {
    headers: authHeaders(token),
  });
}

export async function createAdminAgisoSkuMapping(
  token: string,
  payload: AdminCreateAgisoSkuMappingRequest
): Promise<AdminAgisoSkuMapping> {
  return requestJson<AdminAgisoSkuMapping>("/api/v1/admin/agiso/sku-mappings", {
    method: "POST",
    headers: authHeaders(token, true),
    body: JSON.stringify(payload),
  });
}

export async function deleteAdminAgisoSkuMapping(
  token: string,
  mappingId: number
): Promise<{ ok: boolean; mapping_id: number }> {
  return requestJson<{ ok: boolean; mapping_id: number }>(
    `/api/v1/admin/agiso/sku-mappings/${encodeURIComponent(String(mappingId))}`,
    {
      method: "DELETE",
      headers: authHeaders(token),
    }
  );
}

export interface ListAgisoEventsParams {
  tid?: string;
  aopic?: number;
  status?: string;
  page?: number;
  page_size?: number;
}

export async function listAdminAgisoEvents(
  token: string,
  params: ListAgisoEventsParams = {}
): Promise<AdminAgisoEventsResponse> {
  const search = new URLSearchParams();
  if (params.tid) search.set("tid", params.tid);
  if (params.aopic !== undefined && Number.isFinite(params.aopic)) {
    search.set("aopic", String(params.aopic));
  }
  if (params.status) search.set("status", params.status);
  if (params.page) search.set("page", String(params.page));
  if (params.page_size) search.set("page_size", String(params.page_size));
  return requestJson<AdminAgisoEventsResponse>(`/api/v1/admin/agiso/events?${search.toString()}`, {
    headers: authHeaders(token),
  });
}

export async function retryAdminAgisoOrder(
  token: string,
  tid: string,
  fetchFromAgiso = true
): Promise<AdminAgisoRetryOrderResponse> {
  return requestJson<AdminAgisoRetryOrderResponse>(
    `/api/v1/admin/agiso/orders/${encodeURIComponent(tid.trim())}/retry`,
    {
      method: "POST",
      headers: authHeaders(token, true),
      body: JSON.stringify({ fetch_from_agiso: fetchFromAgiso }),
    }
  );
}

export async function syncAdminAgisoOrders(
  token: string,
  payload: AdminAgisoSyncOrdersRequest = {}
): Promise<AdminAgisoSyncOrdersResponse> {
  return requestJson<AdminAgisoSyncOrdersResponse>("/api/v1/admin/agiso/sync-orders", {
    method: "POST",
    headers: authHeaders(token, true),
    body: JSON.stringify(payload),
  });
}

export async function getAdminAgisoRemoteSkus(
  token: string,
  numIids: string
): Promise<AdminAgisoRemoteSkusResponse> {
  const search = new URLSearchParams({ num_iids: numIids.trim() });
  return requestJson<AdminAgisoRemoteSkusResponse>(
    `/api/v1/admin/agiso/remote-skus?${search.toString()}`,
    { headers: authHeaders(token) }
  );
}

export async function pushAdminAgisoCards(
  token: string,
  payload: AdminAgisoPushCardsRequest
): Promise<AdminAgisoPushCardsResponse> {
  return requestJson<AdminAgisoPushCardsResponse>("/api/v1/admin/agiso/push-cards", {
    method: "POST",
    headers: authHeaders(token, true),
    body: JSON.stringify(payload),
  });
}

export async function getAdminAgisoStock(
  token: string,
  skuKeys?: string[]
): Promise<AdminAgisoStockResponse> {
  const search = new URLSearchParams();
  if (skuKeys && skuKeys.length > 0) search.set("sku_keys", skuKeys.join(","));
  const qs = search.toString();
  return requestJson<AdminAgisoStockResponse>(`/api/v1/admin/agiso/stock${qs ? `?${qs}` : ""}`, {
    headers: authHeaders(token),
  });
}

export async function restockAdminAgisoCards(
  token: string,
  payload: { dry_check?: boolean; sku_keys?: string[] } = {}
): Promise<AdminAgisoRestockResponse> {
  return requestJson<AdminAgisoRestockResponse>("/api/v1/admin/agiso/restock", {
    method: "POST",
    headers: authHeaders(token, true),
    body: JSON.stringify(payload),
  });
}

export async function getAdminChannel16688Stock(
  token: string,
  skuKeys?: string[]
): Promise<AdminChannel16688StockResponse> {
  const search = new URLSearchParams();
  if (skuKeys && skuKeys.length > 0) search.set("sku_keys", skuKeys.join(","));
  const qs = search.toString();
  return requestJson<AdminChannel16688StockResponse>(
    `/api/v1/admin/channel16688/stock${qs ? `?${qs}` : ""}`,
    { headers: authHeaders(token) }
  );
}

export async function restockAdminChannel16688Cards(
  token: string,
  payload: { dry_check?: boolean; sku_keys?: string[]; sync_sold?: boolean } = {}
): Promise<AdminChannel16688RestockResponse> {
  return requestJson<AdminChannel16688RestockResponse>("/api/v1/admin/channel16688/restock", {
    method: "POST",
    headers: authHeaders(token, true),
    body: JSON.stringify(payload),
  });
}
