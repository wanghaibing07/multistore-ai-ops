import { useEffect, useState, type FormEvent } from "react";
import { CheckCircle2, Loader2, Mail, Save, Sliders } from "lucide-react";
import { ApiError, sendAdminTestEmail, updateAdminSku } from "../../api/client";
import { useToast } from "../../components/ToastProvider";
import type { AdminOverviewResponse, AdminSkuStatus, SkuKey } from "../../types/api";
import { formatUsd, getSkuShortLabel } from "../../utils/format";

interface AdminSettingsPageProps {
  token: string;
  overview: AdminOverviewResponse;
  onUnauthorized: () => void;
  onUpdated: () => Promise<void>;
}

interface SkuDraft {
  maxCost: string;
  goodsNo: string;
}

function buildDrafts(skus: AdminSkuStatus[]): Record<SkuKey, SkuDraft> {
  const result: Record<SkuKey, SkuDraft> = {
    ultra_shared_slot: { maxCost: "16", goodsNo: "" },
    ultra_exclusive_admin: { maxCost: "72", goodsNo: "" },
  };
  for (const sku of skus) {
    result[sku.sku_key] = {
      maxCost: String(sku.max_cost_usd),
      goodsNo: sku.goods_no || "",
    };
  }
  return result;
}

export function AdminSettingsPage({
  token,
  overview,
  onUnauthorized,
  onUpdated,
}: AdminSettingsPageProps) {
  const { pushToast } = useToast();
  const [drafts, setDrafts] = useState<Record<SkuKey, SkuDraft>>(() =>
    buildDrafts(overview.skus)
  );
  const [savingSku, setSavingSku] = useState<SkuKey | null>(null);
  const [sendingEmail, setSendingEmail] = useState(false);
  const [emailResult, setEmailResult] = useState<string | null>(null);

  useEffect(() => {
    setDrafts(buildDrafts(overview.skus));
  }, [overview.skus]);

  const handleSaveSku = async (event: FormEvent, skuKey: SkuKey) => {
    event.preventDefault();
    const draft = drafts[skuKey];
    const parsedCost = Number(draft.maxCost);
    if (!Number.isFinite(parsedCost) || parsedCost <= 0) {
      pushToast("单笔最高进价必须是大于 0 的数字", "error");
      return;
    }

    setSavingSku(skuKey);
    try {
      await updateAdminSku(token, skuKey, {
        max_cost_usd: parsedCost,
        goods_no: draft.goodsNo.trim() || null,
      });
      await onUpdated();
      pushToast("价格上限和 16688 商品编号已保存", "success");
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 401) {
        onUnauthorized();
        return;
      }
      pushToast(caught instanceof ApiError ? caught.message : "保存设置失败", "error");
    } finally {
      setSavingSku(null);
    }
  };

  const handleSendTestEmail = async () => {
    setSendingEmail(true);
    setEmailResult(null);
    try {
      const response = await sendAdminTestEmail(token);
      setEmailResult(response.message);
      pushToast(response.ok ? "测试邮件已发送" : response.message, response.ok ? "success" : "error");
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 401) {
        onUnauthorized();
        return;
      }
      const message = caught instanceof ApiError ? caught.message : "发送测试邮件失败";
      setEmailResult(message);
      pushToast(message, "error");
    } finally {
      setSendingEmail(false);
    }
  };

  return (
    <div className="stack-lg">
      <header className="admin-header">
        <div>
          <h1 className="admin-page-title">价格与设置</h1>
          <p className="admin-page-sub">
            设置共享车位和独享整组的单笔最高进价，超过会自动暂停下单，防止上游突然涨价亏钱。
          </p>
        </div>
      </header>

      <div className="admin-two-col">
        {overview.skus.map((sku) => {
          const draft = drafts[sku.sku_key];
          const saving = savingSku === sku.sku_key;
          const isExclusive = sku.sku_key === "ultra_exclusive_admin";
          return (
            <form
              key={sku.sku_key}
              className="card stack"
              onSubmit={(event) => void handleSaveSku(event, sku.sku_key)}
            >
              <div className="row-between">
                <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
                  <Sliders size={17} style={{ color: "var(--accent)" }} aria-hidden="true" />
                  <span
                    className={`sku-tag ${
                      isExclusive ? "sku-tag-exclusive" : "sku-tag-shared"
                    }`}
                  >
                    {getSkuShortLabel(sku.sku_key)}
                  </span>
                  <h2 className="card-title" style={{ fontSize: 15.5 }}>
                    {sku.title}
                  </h2>
                </div>
                <span className={`badge ${sku.price_exceeded ? "badge-danger" : "badge-success"}`}>
                  {sku.price_exceeded
                    ? `超价停单 · 现价 ${formatUsd(sku.current_price_usd)}`
                    : `当前进价 ${formatUsd(sku.current_price_usd)}`}
                </span>
              </div>

              <div className="kv-grid">
                <div className="kv-item">
                  <span className="kv-label">Canboso 商品编号</span>
                  <span className="kv-value mono">{sku.canboso_product_id}</span>
                </div>
                <div className="kv-item">
                  <span className="kv-label">发货方式</span>
                  <span className="kv-value">
                    {isExclusive ? "秒发商店套餐邮箱" : "排队拉进家庭组"}
                  </span>
                </div>
              </div>

              <div className="field-group">
                <label className="field-label" htmlFor={`cost-${sku.sku_key}`}>
                  <span>单笔最高进价（USD）</span>
                  <span className="field-hint">超过自动暂停下单，防上游突然涨价亏钱</span>
                </label>
                <input
                  id={`cost-${sku.sku_key}`}
                  type="number"
                  step="0.01"
                  min="0.01"
                  className="input"
                  value={draft.maxCost}
                  onChange={(event) =>
                    setDrafts((prev) => ({
                      ...prev,
                      [sku.sku_key]: { ...prev[sku.sku_key], maxCost: event.target.value },
                    }))
                  }
                />
              </div>

              <div className="field-group">
                <label className="field-label" htmlFor={`goods-${sku.sku_key}`}>
                  <span>16688 商品编号</span>
                  <span className="field-hint">可选，填了之后 16688 提货会自动核对防串货</span>
                </label>
                <input
                  id={`goods-${sku.sku_key}`}
                  type="text"
                  className="input"
                  placeholder="例如：G16688-SLOT-01"
                  value={draft.goodsNo}
                  onChange={(event) =>
                    setDrafts((prev) => ({
                      ...prev,
                      [sku.sku_key]: { ...prev[sku.sku_key], goodsNo: event.target.value },
                    }))
                  }
                />
              </div>

              <button
                type="submit"
                className={`btn btn-primary btn-block ${saving ? "is-loading" : ""}`}
                disabled={saving}
                aria-busy={saving}
              >
                {saving ? (
                  <>
                    <Loader2 size={15} className="spin" />
                    <span>保存中...</span>
                  </>
                ) : (
                  <>
                    <Save size={15} />
                    <span>保存这项设置</span>
                  </>
                )}
              </button>
            </form>
          );
        })}
      </div>

      <section className="card stack" aria-label="异常提醒邮件测试">
        <div className="row-between">
          <div className="row">
            <Mail size={18} style={{ color: "var(--accent-2)" }} aria-hidden="true" />
            <div>
              <h2 className="card-title">商家邮件通知测试</h2>
              <p className="card-subtitle">
                只在余额偏低、超价停单、上游出错、超 24 小时没拉人，或者独享买家提交保修时发邮件提醒，平常不打扰。
              </p>
            </div>
          </div>
          <button
            type="button"
            className={`btn btn-secondary ${sendingEmail ? "is-loading" : ""}`}
            disabled={sendingEmail}
            aria-busy={sendingEmail}
            onClick={() => void handleSendTestEmail()}
          >
            {sendingEmail ? <Loader2 size={15} className="spin" /> : <Mail size={15} />}
            <span>发送测试邮件</span>
          </button>
        </div>

        {emailResult && (
          <div className="callout callout-success" role="status">
            <CheckCircle2 size={17} className="callout-icon" aria-hidden="true" />
            <div>{emailResult}</div>
          </div>
        )}
      </section>
    </div>
  );
}
