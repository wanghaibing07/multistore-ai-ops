import { useState, type FormEvent, type KeyboardEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ArrowRight, KeyRound, ShieldCheck, TicketCheck, Users } from "lucide-react";
import { isDemoMode } from "../api/client";
import { isImeComposing } from "../utils/ime";
import { extractRedeemCode } from "../utils/validation";

const DEMO_SHORTCUTS = [
  { code: "DEMO-SLOT-NEW", label: "共享车位 · 待填邮箱" },
  { code: "DEMO-SLOT-WAIT", label: "共享车位 · 排队拉人中" },
  { code: "DEMO-SLOT-DONE", label: "共享车位 · 已完成" },
  { code: "DEMO-EXCL-NEW", label: "独享整组 · 待领商店邮箱" },
  { code: "DEMO-EXCL-PENDING", label: "独享整组 · 待交保修" },
  { code: "DEMO-EXCL-WARRANTY", label: "独享整组 · 等商家报备" },
  { code: "DEMO-EXCL-DONE", label: "独享整组 · 已报备完成" },
  { code: "DEMO-REVIEW", label: "人工处理中" },
  { code: "DEMO-REVOKED", label: "已作废码" },
];

export function HomePage() {
  const [rawInput, setRawInput] = useState("");
  const [error, setError] = useState<string | null>(null);
  const navigate = useNavigate();
  const demo = isDemoMode();

  const submitLookup = () => {
    const extracted = extractRedeemCode(rawInput);
    if (!extracted || extracted.length < 4) {
      setError("请输入有效的兑换码，或者直接粘贴客服发给你的整条提货链接");
      return;
    }
    setError(null);
    navigate(`/r/${encodeURIComponent(extracted)}`);
  };

  const handleSubmit = (event: FormEvent) => {
    event.preventDefault();
    submitLookup();
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key !== "Enter") return;
    if (isImeComposing(event)) {
      event.preventDefault();
    }
  };

  return (
    <div className="buyer-page">
      <div className="buyer-container">
        <header className="buyer-topbar">
          <Link to="/" className="brand-lockup">
            <span className="brand-mark" aria-hidden="true">
              <TicketCheck size={18} />
            </span>
            <span>Google AI Ultra 20x 自助提货</span>
          </Link>
        </header>

        <main className="card stack">
          <div>
            <h1 className="card-title" style={{ fontSize: 22 }}>
              输入兑换码，立即提货或查进度
            </h1>
            <p className="card-subtitle">
              淘宝、16688 订单都能用。直接粘贴客服发的整段消息或链接，系统会自动认出兑换码。
            </p>
          </div>

          <form onSubmit={handleSubmit} className="stack">
            <div className="field-group">
              <label className="field-label" htmlFor="redeem-code-input">
                <span>兑换码或提货链接</span>
                <span className="field-hint">自动识别兑换码</span>
              </label>
              <input
                id="redeem-code-input"
                type="text"
                enterKeyHint="go"
                autoComplete="off"
                spellCheck={false}
                aria-invalid={Boolean(error) || undefined}
                aria-describedby={error ? "redeem-code-error" : undefined}
                className={`input mono ${error ? "input-error" : ""}`}
                placeholder="例如：U20S-8K3M-9P2Q 或整条提货链接"
                value={rawInput}
                onKeyDown={handleKeyDown}
                onChange={(event) => {
                  setRawInput(event.target.value);
                  if (error) setError(null);
                }}
              />
              {error ? (
                <div id="redeem-code-error" className="field-error" role="alert">
                  {error}
                </div>
              ) : null}
            </div>

            <button type="submit" className="btn btn-primary btn-lg btn-block">
              <KeyRound size={17} />
              <span>查进度 / 去提货</span>
              <ArrowRight size={17} />
            </button>
          </form>

          <div className="sku-ledger" aria-label="两种规格说明">
            <div className="sku-ledger-row">
              <div className="sku-ledger-head">
                <span className="sku-ledger-icon slot" aria-hidden="true">
                  <Users size={15} />
                </span>
                <span className="sku-tag sku-tag-shared">共享车位</span>
              </div>
              <p className="sku-ledger-desc">
                填你的 Gmail 等拉人，一般 1~6 小时、最晚 24 小时，随时回本页看进度。
              </p>
            </div>
            <div className="sku-ledger-row">
              <div className="sku-ledger-head">
                <span className="sku-ledger-icon account" aria-hidden="true">
                  <ShieldCheck size={15} />
                </span>
                <span className="sku-tag sku-tag-exclusive">独享整组</span>
              </div>
              <p className="sku-ledger-desc">
                先领商店邮箱，用你主力 Gmail 建家庭组邀请它，再传截图交保修。
              </p>
            </div>
          </div>
        </main>

        {demo ? (
          <section className="card card-compact stack-sm" aria-label="演示模式快捷入口">
            <div className="row-between">
              <strong style={{ fontSize: 13.5 }}>演练快捷入口（本地模拟，不扣钱）</strong>
            </div>
            <div className="pill-scroller">
              {DEMO_SHORTCUTS.map((item) => (
                <Link key={item.code} to={`/r/${item.code}`} className="filter-pill">
                  {item.label}
                </Link>
              ))}
            </div>
          </section>
        ) : null}

        <footer className="footer-links">
          <span>提货后建议收藏专属链接，随时回来查进度</span>
        </footer>
      </div>
    </div>
  );
}
