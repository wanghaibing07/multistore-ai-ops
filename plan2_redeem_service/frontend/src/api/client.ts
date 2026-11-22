import type {
  AdminBatchCodeRequest,
  AdminBatchCodeResponse,
  AdminCodeDetail,
  AdminCodeListResponse,
  AdminIssueCodeRequest,
  AdminIssueCodeResponse,
  AdminOverviewResponse,
  AdminSkuStatus,
  AdminTestEmailResponse,
  AdminUpdateSkuRequest,
  ApiErrorEnvelope,
  PublicCodeResponse,
  RedeemExclusiveRequest,
  RedeemSlotRequest,
  SkuKey,
} from "../types/api";
import { demoMockStore, MockApiError } from "./mockServer";

const DEMO_STORAGE_KEY = "plan2_demo_mode";

export class ApiError extends Error {
  status: number;
  errorCode: string;

  constructor(status: number, errorCode: string, message: string) {
    super(message);
    this.status = status;
    this.errorCode = errorCode;
  }
}

export function syncDemoModeFromLocation(): boolean {
  if (typeof window === "undefined") return false;
  const params = new URLSearchParams(window.location.search);
  const flag = params.get("demo");
  if (flag === "1" || flag === "true") {
    sessionStorage.setItem(DEMO_STORAGE_KEY, "1");
    return true;
  }
  if (flag === "0" || flag === "false") {
    sessionStorage.removeItem(DEMO_STORAGE_KEY);
    return false;
  }
  return sessionStorage.getItem(DEMO_STORAGE_KEY) === "1";
}

export function isDemoMode(): boolean {
  if (typeof window === "undefined") return false;
  return sessionStorage.getItem(DEMO_STORAGE_KEY) === "1";
}

export function setDemoMode(enabled: boolean): void {
  if (typeof window === "undefined") return;
  if (enabled) {
    sessionStorage.setItem(DEMO_STORAGE_KEY, "1");
  } else {
    sessionStorage.removeItem(DEMO_STORAGE_KEY);
  }
}

function mapMockError(error: unknown): never {
  if (error instanceof MockApiError) {
    throw new ApiError(error.status, error.errorCode, error.message);
  }
  throw error;
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
  if (isDemoMode()) {
    try {
      return demoMockStore.getPublicCode(code);
    } catch (error) {
      mapMockError(error);
    }
  }
  return requestJson<PublicCodeResponse>(`/api/v1/public/codes/${encodeURIComponent(code)}`);
}

export async function redeemPublicCode(
  code: string,
  payload: RedeemSlotRequest | RedeemExclusiveRequest
): Promise<PublicCodeResponse> {
  if (isDemoMode()) {
    try {
      return demoMockStore.redeemCode(code, payload);
    } catch (error) {
      mapMockError(error);
    }
  }
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
  if (isDemoMode()) {
    try {
      return demoMockStore.refreshPublicCode(code);
    } catch (error) {
      mapMockError(error);
    }
  }
  return requestJson<PublicCodeResponse>(
    `/api/v1/public/codes/${encodeURIComponent(code)}/refresh`,
    { method: "POST" }
  );
}

export async function submitPublicWarranty(
  code: string,
  formData: FormData
): Promise<PublicCodeResponse> {
  if (isDemoMode()) {
    try {
      return await demoMockStore.submitWarranty(code, formData);
    } catch (error) {
      mapMockError(error);
    }
  }
  return requestJson<PublicCodeResponse>(
    `/api/v1/public/codes/${encodeURIComponent(code)}/warranty`,
    {
      method: "POST",
      body: formData,
    }
  );
}

export async function getAdminOverview(token: string): Promise<AdminOverviewResponse> {
  if (isDemoMode()) {
    try {
      return demoMockStore.getOverview(token);
    } catch (error) {
      mapMockError(error);
    }
  }
  return requestJson<AdminOverviewResponse>("/api/v1/admin/overview", {
    headers: authHeaders(token),
  });
}

export async function refreshAdminUpstream(token: string): Promise<AdminOverviewResponse> {
  if (isDemoMode()) {
    try {
      return demoMockStore.refreshUpstream(token);
    } catch (error) {
      mapMockError(error);
    }
  }
  return requestJson<AdminOverviewResponse>("/api/v1/admin/upstream/refresh", {
    method: "POST",
    headers: authHeaders(token),
  });
}

export async function issueAdminCode(
  token: string,
  payload: AdminIssueCodeRequest
): Promise<AdminIssueCodeResponse> {
  if (isDemoMode()) {
    try {
      return demoMockStore.issueCode(token, payload);
    } catch (error) {
      mapMockError(error);
    }
  }
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
  if (isDemoMode()) {
    try {
      return demoMockStore.batchCodes(token, payload);
    } catch (error) {
      mapMockError(error);
    }
  }
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
  page?: number;
  page_size?: number;
}

export async function listAdminCodes(
  token: string,
  params: ListCodesParams
): Promise<AdminCodeListResponse> {
  if (isDemoMode()) {
    try {
      return demoMockStore.listCodes(token, params);
    } catch (error) {
      mapMockError(error);
    }
  }
  const search = new URLSearchParams();
  if (params.status) search.set("status", params.status);
  if (params.channel) search.set("channel", params.channel);
  if (params.sku_key) search.set("sku_key", params.sku_key);
  if (params.q) search.set("q", params.q);
  if (params.page) search.set("page", String(params.page));
  if (params.page_size) search.set("page_size", String(params.page_size));

  return requestJson<AdminCodeListResponse>(`/api/v1/admin/codes?${search.toString()}`, {
    headers: authHeaders(token),
  });
}

export async function getAdminCodeDetail(token: string, code: string): Promise<AdminCodeDetail> {
  if (isDemoMode()) {
    try {
      return demoMockStore.getCodeDetail(token, code);
    } catch (error) {
      mapMockError(error);
    }
  }
  return requestJson<AdminCodeDetail>(`/api/v1/admin/codes/${encodeURIComponent(code)}`, {
    headers: authHeaders(token),
  });
}

export async function fetchAdminWarrantyScreenshot(
  token: string,
  code: string
): Promise<{ url: string; revoke: () => void }> {
  if (isDemoMode()) {
    try {
      const dataUrl = demoMockStore.getWarrantyScreenshotUrl(token, code);
      return { url: dataUrl, revoke: () => undefined };
    } catch (error) {
      mapMockError(error);
    }
  }
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
  if (isDemoMode()) {
    try {
      return demoMockStore.pollCode(token, code);
    } catch (error) {
      mapMockError(error);
    }
  }
  return requestJson<AdminCodeDetail>(`/api/v1/admin/codes/${encodeURIComponent(code)}/poll`, {
    method: "POST",
    headers: authHeaders(token),
  });
}

export async function revokeAdminCode(
  token: string,
  code: string,
  reason: string
): Promise<AdminCodeDetail> {
  if (isDemoMode()) {
    try {
      return demoMockStore.revokeCode(token, code, reason);
    } catch (error) {
      mapMockError(error);
    }
  }
  return requestJson<AdminCodeDetail>(`/api/v1/admin/codes/${encodeURIComponent(code)}/revoke`, {
    method: "POST",
    headers: authHeaders(token, true),
    body: JSON.stringify({ reason }),
  });
}

export async function retryAdminCode(
  token: string,
  code: string,
  payload: { override_price_limit?: boolean; note?: string }
): Promise<AdminCodeDetail> {
  if (isDemoMode()) {
    try {
      return demoMockStore.retryCode(token, code, payload);
    } catch (error) {
      mapMockError(error);
    }
  }
  return requestJson<AdminCodeDetail>(`/api/v1/admin/codes/${encodeURIComponent(code)}/retry`, {
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
  if (isDemoMode()) {
    try {
      return demoMockStore.ackWarranty(token, code, note);
    } catch (error) {
      mapMockError(error);
    }
  }
  return requestJson<AdminCodeDetail>(
    `/api/v1/admin/codes/${encodeURIComponent(code)}/warranty-ack`,
    {
      method: "POST",
      headers: authHeaders(token, true),
      body: JSON.stringify({ note }),
    }
  );
}

export async function updateAdminSku(
  token: string,
  skuKey: SkuKey,
  payload: AdminUpdateSkuRequest
): Promise<AdminSkuStatus> {
  if (isDemoMode()) {
    try {
      return demoMockStore.updateSku(token, skuKey, payload);
    } catch (error) {
      mapMockError(error);
    }
  }
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
  if (isDemoMode()) {
    try {
      return demoMockStore.sendTestEmail(token);
    } catch (error) {
      mapMockError(error);
    }
  }
  return requestJson<AdminTestEmailResponse>("/api/v1/admin/notifications/test-email", {
    method: "POST",
    headers: authHeaders(token),
  });
}
