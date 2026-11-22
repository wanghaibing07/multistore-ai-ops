import { useId, useState, type FormEvent, type KeyboardEvent } from "react";
import { Link } from "react-router-dom";
import { AlertCircle, ArrowLeft, Eye, EyeOff, KeyRound, Loader2, ShieldCheck } from "lucide-react";
import { ApiError, getAdminOverview, isDemoMode } from "../../api/client";
import type { AdminOverviewResponse } from "../../types/api";
import { isImeComposing } from "../../utils/ime";

interface AdminLoginPageProps {
  onAuthenticated: (token: string, initialOverview: AdminOverviewResponse) => void;
}

export function AdminLoginPage({ onAuthenticated }: AdminLoginPageProps) {
  const [token, setToken] = useState("");
  const [showToken, setShowToken] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputId = useId();
  const errorId = useId();
  const demo = isDemoMode();

  const handleSubmit = async (event: FormEvent) => {
    event.preventDefault();
    const trimmed = token.trim();
    if (!trimmed) {
      setError("请输入管理员令牌（ADMIN_TOKEN）");
      return;
    }
    if (demo && trimmed.length < 4) {
      setError("演练模式下请输入至少 4 位任意字符作为口令");
      return;
    }

    setSubmitting(true);
    setError(null);
    try {
      const overview = await getAdminOverview(trimmed);
      onAuthenticated(trimmed, overview);
    } catch (caught) {
      if (caught instanceof ApiError) {
        setError(caught.message || "管理员令牌校验失败");
      } else {
        setError("无法连接后台服务，请确认后端已启动或改用演练模式");
      }
    } finally {
      setSubmitting(false);
    }
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Enter" && isImeComposing(event)) {
      event.preventDefault();
    }
  };

  return (
    <div className="login-shell">
      <div className="login-card stack">
        <div className="row-between">
          <Link to="/" className="btn btn-ghost btn-sm">
            <ArrowLeft size={15} />
            <span>返回买家提货页</span>
          </Link>
          {demo && <span className="badge badge-accent">本地演练模式</span>}
        </div>

        <form className="card stack" onSubmit={handleSubmit} noValidate>
          <div className="row">
            <span className="brand-mark" aria-hidden="true">
              <ShieldCheck size={18} />
            </span>
            <div>
              <h1 className="card-title">商家履约工作台登录</h1>
              <p className="card-subtitle">使用后端配置的 Bearer 管理员口令进入控制台</p>
            </div>
          </div>

          {demo && (
            <div className="callout callout-info">
              <KeyRound size={17} className="callout-icon" aria-hidden="true" />
              <div>
                演练模式已启用：输入任意 4 位以上字符（例如 <strong>demo-admin</strong>）即可进入后台体验完整发码、保修和熔断流程。
              </div>
            </div>
          )}

          <div className="field-group">
            <label htmlFor={inputId} className="field-label">
              <span>管理员口令（ADMIN_TOKEN）</span>
              <span className="field-hint">仅保存于当前标签页 sessionStorage</span>
            </label>
            <div className="input-wrap">
              <input
                id={inputId}
                type={showToken ? "text" : "password"}
                className={`input input-with-action ${error ? "input-error" : ""}`}
                placeholder={demo ? "例如：demo-admin" : "请输入后端 .env 中配置的管理员口令"}
                autoComplete="current-password"
                enterKeyHint="go"
                value={token}
                disabled={submitting}
                aria-invalid={Boolean(error)}
                aria-describedby={error ? errorId : undefined}
                onChange={(event) => {
                  setToken(event.target.value);
                  if (error) setError(null);
                }}
                onKeyDown={handleKeyDown}
              />
              <button
                type="button"
                className="input-action-btn"
                aria-label={showToken ? "隐藏口令" : "显示口令"}
                onClick={() => setShowToken((prev) => !prev)}
              >
                {showToken ? <EyeOff size={16} /> : <Eye size={16} />}
              </button>
            </div>
            {error && (
              <p id={errorId} className="field-error" role="alert">
                <AlertCircle size={14} aria-hidden="true" />
                <span>{error}</span>
              </p>
            )}
          </div>

          <button
            type="submit"
            className={`btn btn-primary btn-lg btn-block ${submitting ? "is-loading" : ""}`}
            disabled={submitting}
            aria-busy={submitting}
          >
            {submitting ? (
              <>
                <Loader2 size={16} className="spin" />
                <span>正在验证口令...</span>
              </>
            ) : (
              <span>进入商家工作台</span>
            )}
          </button>

          <p className="field-hint">
            关闭浏览器标签页后登录状态会自动失效，避免在公用设备上残留管理员凭据。
          </p>
        </form>
      </div>
    </div>
  );
}
