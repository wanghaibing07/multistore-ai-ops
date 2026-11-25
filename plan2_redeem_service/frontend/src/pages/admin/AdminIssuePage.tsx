import { useId, useState, type FormEvent } from "react";
import { AlertCircle, Download, Layers, Loader2, Send, TicketPlus } from "lucide-react";
import { ApiError, batchAdminCodes, issueAdminCode } from "../../api/client";
import { CopyButton } from "../../components/CopyButton";
import { useToast } from "../../components/ToastProvider";
import type {
  AdminBatchCodeResponse,
  AdminIssueCodeResponse,
  AdminOverviewResponse,
  Channel,
  SkuKey,
} from "../../types/api";
import { downloadTextFile, getSkuShortLabel } from "../../utils/format";

interface AdminIssuePageProps {
  token: string;
  overview: AdminOverviewResponse;
  onUnauthorized: () => void;
  onIssued: () => void;
  onOpenCode: (code: string) => void;
}

function getSkuIssueHint(skuKey: SkuKey): string {
  if (skuKey === "ultra_exclusive_admin") {
    return "独享整组：买家提货立刻拿到商店套餐邮箱，拉进自己的家庭组后再交主邮箱和截图登记保修。";
  }
  return "共享车位：买家提货时填自己的邮箱，系统自动向上游下单排队拉人，每 20 分钟自动查一次进度。";
}

export function AdminIssuePage({
  token,
  overview,
  onUnauthorized,
  onIssued,
  onOpenCode,
}: AdminIssuePageProps) {
  const { pushToast } = useToast();

  // Single issue state
  const [singleSku, setSingleSku] = useState<SkuKey>("ultra_shared_slot");
  const [singleChannel, setSingleChannel] = useState<Channel>("taobao");
  const [externalOrderId, setExternalOrderId] = useState("");
  const [remark, setRemark] = useState("");
  const [singleLoading, setSingleLoading] = useState(false);
  const [singleError, setSingleError] = useState<string | null>(null);
  const [singleResult, setSingleResult] = useState<AdminIssueCodeResponse | null>(null);

  // Batch issue state
  const [batchSku, setBatchSku] = useState<SkuKey>("ultra_shared_slot");
  const [batchChannel, setBatchChannel] = useState<Channel>("16688");
  const [batchCount, setBatchCount] = useState("20");
  const [batchGoodsNo, setBatchGoodsNo] = useState(() => {
    const matched = overview.skus.find((sku) => sku.sku_key === "ultra_shared_slot");
    return matched?.goods_no || "";
  });
  const [batchLabel, setBatchLabel] = useState("");
  const [batchLoading, setBatchLoading] = useState(false);
  const [batchError, setBatchError] = useState<string | null>(null);
  const [batchResult, setBatchResult] = useState<AdminBatchCodeResponse | null>(null);
  const [exportFormat, setExportFormat] = useState<"with_url" | "code_only">("with_url");

  const singleSkuId = useId();
  const singleChannelId = useId();
  const singleOrderId = useId();
  const singleRemarkId = useId();
  const batchSkuId = useId();
  const batchChannelId = useId();
  const batchCountId = useId();
  const batchGoodsId = useId();
  const batchLabelId = useId();

  const handleBatchSkuChange = (nextSku: SkuKey) => {
    setBatchSku(nextSku);
    const matched = overview.skus.find((sku) => sku.sku_key === nextSku);
    setBatchGoodsNo(matched?.goods_no || "");
  };

  const handleSingleSubmit = async (event: FormEvent) => {
    event.preventDefault();
    const trimmedOrder = externalOrderId.trim();
    if (!trimmedOrder) {
      setSingleError("请填写淘宝或外部订单号，方便以后核对售后");
      return;
    }

    setSingleLoading(true);
    setSingleError(null);
    try {
      const response = await issueAdminCode(token, {
        sku_key: singleSku,
        channel: singleChannel,
        external_order_id: trimmedOrder,
        remark: remark.trim() || undefined,
      });
      setSingleResult(response);
      setExternalOrderId("");
      setRemark("");
      pushToast(`已生成兑换码 ${response.code}`, "success");
      onIssued();
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 401) {
        onUnauthorized();
        return;
      }
      const message = caught instanceof ApiError ? caught.message : "生成兑换码失败，请重试";
      setSingleError(message);
      pushToast(message, "error");
    } finally {
      setSingleLoading(false);
    }
  };

  const handleBatchSubmit = async (event: FormEvent) => {
    event.preventDefault();
    const parsedCount = Number(batchCount);
    if (!Number.isInteger(parsedCount) || parsedCount < 1 || parsedCount > 200) {
      setBatchError("批量生成数量请填 1 到 200 之间的整数");
      return;
    }

    setBatchLoading(true);
    setBatchError(null);
    try {
      const response = await batchAdminCodes(token, {
        sku_key: batchSku,
        channel: batchChannel,
        count: parsedCount,
        goods_no: batchChannel === "16688" ? batchGoodsNo.trim() || undefined : undefined,
        batch_label: batchLabel.trim() || undefined,
      });
      setBatchResult(response);
      pushToast(`批量生成成功：共 ${response.count} 个兑换码`, "success");
      onIssued();
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 401) {
        onUnauthorized();
        return;
      }
      const message = caught instanceof ApiError ? caught.message : "批量制码失败，请重试";
      setBatchError(message);
      pushToast(message, "error");
    } finally {
      setBatchLoading(false);
    }
  };

  const activeBatchText = batchResult
    ? exportFormat === "with_url"
      ? batchResult.export_lines_with_url
      : batchResult.export_lines_code_only
    : "";

  return (
    <div className="stack-lg">
      <header className="admin-header">
        <div>
          <h1 className="admin-page-title">发兑换码</h1>
          <p className="admin-page-sub">
            支持共享车位和独享整组两种规格：淘宝出一单发一个码，16688 可以一次批量生成导出上货，不提前占压货款。
          </p>
        </div>
      </header>

      <div className="admin-two-col">
        <section className="card stack" aria-label="单笔发码">
          <div className="row">
            <Send size={18} style={{ color: "var(--accent)" }} aria-hidden="true" />
            <div>
              <h2 className="card-title">单笔发码（淘宝 / 手动发货）</h2>
              <p className="card-subtitle">生成 1 个一次性兑换码，并自动生成可直接发给买家的旺旺话术</p>
            </div>
          </div>

          <form className="stack" onSubmit={handleSingleSubmit} noValidate>
            <div className="field-group">
              <label htmlFor={singleSkuId} className="field-label">
                <span>商品规格</span>
                <span
                  className={`sku-tag ${
                    singleSku === "ultra_exclusive_admin"
                      ? "sku-tag-exclusive"
                      : "sku-tag-shared"
                  }`}
                >
                  {getSkuShortLabel(singleSku)}
                </span>
              </label>
              <select
                id={singleSkuId}
                className="select"
                value={singleSku}
                onChange={(event) => setSingleSku(event.target.value as SkuKey)}
              >
                <option value="ultra_shared_slot">Google Ultra x20 共享车位</option>
                <option value="ultra_exclusive_admin">Google Ultra x20 独享整组（管理员版）</option>
              </select>
              <span className="field-hint">{getSkuIssueHint(singleSku)}</span>
            </div>

            <div className="field-group">
              <label htmlFor={singleChannelId} className="field-label">
                <span>销售渠道</span>
              </label>
              <select
                id={singleChannelId}
                className="select"
                value={singleChannel}
                onChange={(event) => setSingleChannel(event.target.value as Channel)}
              >
                <option value="taobao">淘宝（直接发码，不查 16688 订单）</option>
                <option value="16688">16688（买家提货前自动核对 16688 订单）</option>
              </select>
            </div>

            <div className="field-group">
              <label htmlFor={singleOrderId} className="field-label">
                <span>平台订单号</span>
                <span className="field-hint">必填</span>
              </label>
              <input
                id={singleOrderId}
                type="text"
                className={`input ${singleError ? "input-error" : ""}`}
                placeholder="例如：TB202611230001"
                value={externalOrderId}
                onChange={(event) => {
                  setExternalOrderId(event.target.value);
                  if (singleError) setSingleError(null);
                }}
              />
            </div>

            <div className="field-group">
              <label htmlFor={singleRemarkId} className="field-label">
                <span>备注（可选）</span>
              </label>
              <input
                id={singleRemarkId}
                type="text"
                className="input"
                placeholder="例如：买家旺旺名或特殊说明"
                value={remark}
                onChange={(event) => setRemark(event.target.value)}
              />
            </div>

            {singleError && (
              <p className="field-error" role="alert">
                <AlertCircle size={14} aria-hidden="true" />
                <span>{singleError}</span>
              </p>
            )}

            <button
              type="submit"
              className={`btn btn-primary btn-block ${singleLoading ? "is-loading" : ""}`}
              disabled={singleLoading}
              aria-busy={singleLoading}
            >
              {singleLoading ? (
                <>
                  <Loader2 size={15} className="spin" />
                  <span>生成中...</span>
                </>
              ) : (
                <>
                  <TicketPlus size={15} />
                  <span>生成兑换码和旺旺话术</span>
                </>
              )}
            </button>
          </form>

          {singleResult && (
            <div className="inset-panel stack-sm">
              <div className="row-between">
                <strong style={{ fontSize: 14 }}>最新生成结果</strong>
                <button
                  type="button"
                  className="btn btn-ghost btn-sm"
                  onClick={() => onOpenCode(singleResult.code)}
                >
                  查看详情
                </button>
              </div>

              <div className="credential-box">
                <div className="credential-label">提货码与直达链接</div>
                <div className="credential-value">{singleResult.code}</div>
                <div style={{ fontSize: 12.5, color: "var(--ink-3)", wordBreak: "break-all" }}>
                  {singleResult.redeem_url}
                </div>
                <div className="row">
                  <CopyButton text={singleResult.redeem_url} label="复制提货链接" variant="secondary" />
                  <CopyButton text={singleResult.code} label="仅复制兑换码" variant="ghost" />
                </div>
              </div>

              <div className="stack-sm">
                <div className="row-between">
                  <span className="field-label">旺旺发货话术</span>
                  <CopyButton
                    text={singleResult.wangwang_message}
                    label="复制旺旺话术"
                    toastMessage="已复制旺旺话术，可直接发给买家"
                    variant="primary"
                  />
                </div>
                <pre className="code-output">{singleResult.wangwang_message}</pre>
              </div>
            </div>
          )}
        </section>

        <section className="card stack" aria-label="批量制码">
          <div className="row">
            <Layers size={18} style={{ color: "var(--accent-2)" }} aria-hidden="true" />
            <div>
              <h2 className="card-title">批量制码（16688 上货 / 提前备码）</h2>
              <p className="card-subtitle">一次最多生成 200 个兑换码，买家提货时才向上游下单，支持导出 TXT</p>
            </div>
          </div>

          <form className="stack" onSubmit={handleBatchSubmit} noValidate>
            <div className="field-group">
              <label htmlFor={batchSkuId} className="field-label">
                <span>商品规格</span>
                <span
                  className={`sku-tag ${
                    batchSku === "ultra_exclusive_admin"
                      ? "sku-tag-exclusive"
                      : "sku-tag-shared"
                  }`}
                >
                  {getSkuShortLabel(batchSku)}
                </span>
              </label>
              <select
                id={batchSkuId}
                className="select"
                value={batchSku}
                onChange={(event) => handleBatchSkuChange(event.target.value as SkuKey)}
              >
                <option value="ultra_shared_slot">Google Ultra x20 共享车位</option>
                <option value="ultra_exclusive_admin">Google Ultra x20 独享整组（管理员版）</option>
              </select>
              <span className="field-hint">{getSkuIssueHint(batchSku)}</span>
            </div>

            <div className="field-group">
              <label htmlFor={batchChannelId} className="field-label">
                <span>上架渠道</span>
              </label>
              <select
                id={batchChannelId}
                className="select"
                value={batchChannel}
                onChange={(event) => setBatchChannel(event.target.value as Channel)}
              >
                <option value="16688">16688 自动发卡上货</option>
                <option value="taobao">淘宝提前备码</option>
              </select>
            </div>

            <div className="field-group">
              <label htmlFor={batchCountId} className="field-label">
                <span>生成数量（1 至 200）</span>
              </label>
              <input
                id={batchCountId}
                type="number"
                min={1}
                max={200}
                className={`input ${batchError ? "input-error" : ""}`}
                value={batchCount}
                onChange={(event) => {
                  setBatchCount(event.target.value);
                  if (batchError) setBatchError(null);
                }}
              />
            </div>

            {batchChannel === "16688" && (
              <div className="field-group">
                <label htmlFor={batchGoodsId} className="field-label">
                  <span>16688 商品编号</span>
                  <span className="field-hint">提货前自动核对，防止不同规格串货</span>
                </label>
                <input
                  id={batchGoodsId}
                  type="text"
                  className="input"
                  placeholder="例如：G16688-SLOT-01"
                  value={batchGoodsNo}
                  onChange={(event) => setBatchGoodsNo(event.target.value)}
                />
              </div>
            )}

            <div className="field-group">
              <label htmlFor={batchLabelId} className="field-label">
                <span>批次标签（可选）</span>
              </label>
              <input
                id={batchLabelId}
                type="text"
                className="input"
                placeholder="例如：11月23日首批补货"
                value={batchLabel}
                onChange={(event) => setBatchLabel(event.target.value)}
              />
            </div>

            {batchError && (
              <p className="field-error" role="alert">
                <AlertCircle size={14} aria-hidden="true" />
                <span>{batchError}</span>
              </p>
            )}

            <button
              type="submit"
              className={`btn btn-warm btn-block ${batchLoading ? "is-loading" : ""}`}
              disabled={batchLoading}
              aria-busy={batchLoading}
            >
              {batchLoading ? (
                <>
                  <Loader2 size={15} className="spin" />
                  <span>批量生成中...</span>
                </>
              ) : (
                <>
                  <Layers size={15} />
                  <span>批量生成兑换码</span>
                </>
              )}
            </button>
          </form>

          {batchResult && (
            <div className="inset-panel stack-sm">
              <div className="row-between">
                <div>
                  <strong style={{ fontSize: 14 }}>批次 {batchResult.batch_id}</strong>
                  <span style={{ marginLeft: 8, fontSize: 12.5, color: "var(--ink-3)" }}>
                    共 {batchResult.count} 条
                  </span>
                </div>
                <div className="row" style={{ gap: 6 }}>
                  <button
                    type="button"
                    className={`filter-pill ${exportFormat === "with_url" ? "is-active" : ""}`}
                    style={{ minHeight: 34, padding: "0 12px" }}
                    onClick={() => setExportFormat("with_url")}
                  >
                    码 + 链接
                  </button>
                  <button
                    type="button"
                    className={`filter-pill ${exportFormat === "code_only" ? "is-active" : ""}`}
                    style={{ minHeight: 34, padding: "0 12px" }}
                    onClick={() => setExportFormat("code_only")}
                  >
                    仅兑换码
                  </button>
                </div>
              </div>

              <pre className="code-output">{activeBatchText}</pre>

              <div className="row">
                <CopyButton
                  text={activeBatchText}
                  label="复制全部导出行"
                  toastMessage="已复制本批次全部兑换码"
                  variant="primary"
                />
                <button
                  type="button"
                  className="btn btn-secondary btn-sm"
                  onClick={() => {
                    downloadTextFile(
                      `${batchResult.batch_id}-${exportFormat}.txt`,
                      `${activeBatchText}\n`
                    );
                    pushToast("已导出 TXT 文件", "success");
                  }}
                >
                  <Download size={14} />
                  <span>下载 TXT</span>
                </button>
              </div>
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
