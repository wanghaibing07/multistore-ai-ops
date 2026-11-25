import { useId, useState, type FormEvent, type KeyboardEvent } from "react";
import { Link } from "react-router-dom";
import { ApiError, getAdminOverview } from "../../api/client";
import { CastoriceIcon } from "../../components/CastoriceIcon";
import type { AdminOverviewResponse } from "../../types/api";
import { isImeComposing } from "../../utils/ime";
import "../../styles/admin-glass.css";
import { AdminWallpaperBackdrop } from "./AdminWallpaperBackdrop";

interface AdminLoginPageProps {
  onAuthenticated: (token: string, initialOverview: AdminOverviewResponse) => void;
  embeddedBackdrop?: boolean;
}

export function AdminLoginPage({ onAuthenticated, embeddedBackdrop = false }: AdminLoginPageProps) {
  const [token, setToken] = useState("");
  const [showToken, setShowToken] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputId = useId();
  const errorId = useId();

  const handleSubmit = async (event: FormEvent) => {
    event.preventDefault();
    const trimmed = token.trim();
    if (!trimmed) {
      setError("请输入管理口令");
      return;
    }

    setSubmitting(true);
    setError(null);
    try {
      const overview = await getAdminOverview(trimmed);
      onAuthenticated(trimmed, overview);
    } catch (caught) {
      if (caught instanceof ApiError) {
        setError(caught.message || "管理口令不对，请重试");
      } else {
        setError("连不上后台服务，请确认服务已启动");
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

  const content = (
    <div className="login-shell">
      <div className="login-card stack">
        <div className="row-between">
          <Link to="/" className="btn btn-ghost btn-sm">
            <CastoriceIcon name="arrow-left" size={17} />
            <span>返回买家提货页</span>
          </Link>
        </div>

        <form className="card admin-login-panel stack" onSubmit={handleSubmit} noValidate>
          <div className="admin-login-head">
            <span className="admin-login-emblem" aria-hidden="true">
              <CastoriceIcon name="admin-brand" size={42} />
            </span>
            <div>
              <h1 className="card-title">商家后台登录</h1>
              <p className="card-subtitle">输入管理口令进入 Ultra 20x 自助提货后台</p>
            </div>
          </div>

          <div className="field-group">
            <label htmlFor={inputId} className="field-label">
              <span>管理口令</span>
              <span className="field-hint">仅在当前浏览器标签页有效</span>
            </label>
            <div className="input-wrap">
              <input
                id={inputId}
                type={showToken ? "text" : "password"}
                className={`input input-with-action ${error ? "input-error" : ""}`}
                placeholder="请输入后台配置的管理口令"
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
                title={showToken ? "隐藏口令" : "显示口令"}
                onClick={() => setShowToken((prev) => !prev)}
              >
                <CastoriceIcon name={showToken ? "admin-happy" : "chibi-peek"} size={24} />
              </button>
            </div>
            {error && (
              <p id={errorId} className="field-error" role="alert">
                <CastoriceIcon name="alert" size={16} />
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
                <CastoriceIcon name="refresh" size={17} spin />
                <span>正在验证口令...</span>
              </>
            ) : (
              <span>进入商家后台</span>
            )}
          </button>

          <p className="field-hint">
            关掉这个标签页会自动退出登录，避免在公用电脑上残留权限。
          </p>
        </form>
      </div>
    </div>
  );

  if (embeddedBackdrop) {
    return content;
  }

  return (
    <div className="admin-glass-stage">
      <AdminWallpaperBackdrop variant="login" />
      {content}
    </div>
  );
}
