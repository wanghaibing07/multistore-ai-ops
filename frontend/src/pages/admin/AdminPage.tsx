import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  ArrowLeft,
  ClipboardList,
  LayoutDashboard,
  LogOut,
  ShieldCheck,
  Sliders,
  TicketPlus,
} from "lucide-react";
import {
  ApiError,
  getAdminOverview,
  refreshAdminUpstream,
} from "../../api/client";
import { useToast } from "../../components/ToastProvider";
import type { AdminOverviewResponse, CodeStatus } from "../../types/api";
import { AdminIssuePage } from "./AdminIssuePage";
import { AdminLoginPage } from "./AdminLoginPage";
import { AdminOrderDrawer } from "./AdminOrderDrawer";
import { AdminOrdersPage } from "./AdminOrdersPage";
import { AdminOverviewPage } from "./AdminOverviewPage";
import { AdminSettingsPage } from "./AdminSettingsPage";

const ADMIN_TOKEN_STORAGE_KEY = "plan2_admin_token";

type AdminTab = "overview" | "issue" | "orders" | "settings";

export function AdminPage() {
  const { pushToast } = useToast();
  const [token, setToken] = useState<string | null>(() => {
    try {
      return sessionStorage.getItem(ADMIN_TOKEN_STORAGE_KEY);
    } catch {
      return null;
    }
  });
  const [overview, setOverview] = useState<AdminOverviewResponse | null>(null);
  const [checkingSession, setCheckingSession] = useState<boolean>(() => Boolean(token));
  const [activeTab, setActiveTab] = useState<AdminTab>("overview");
  const [orderStatusFilter, setOrderStatusFilter] = useState<CodeStatus | "">("");
  const [selectedCode, setSelectedCode] = useState<string | null>(null);
  const [ordersRefreshKey, setOrdersRefreshKey] = useState(0);

  const clearSession = useCallback(
    (reason?: string) => {
      try {
        sessionStorage.removeItem(ADMIN_TOKEN_STORAGE_KEY);
      } catch {
        // ignore storage errors
      }
      setToken(null);
      setOverview(null);
      setSelectedCode(null);
      if (reason) {
        pushToast(reason, "error");
      }
    },
    [pushToast]
  );

  const handleUnauthorized = useCallback(() => {
    clearSession("登录凭据已失效，请重新输入管理员口令");
  }, [clearSession]);

  const fetchOverview = useCallback(
    async (activeToken: string) => {
      try {
        const data = await getAdminOverview(activeToken);
        setOverview(data);
      } catch (caught) {
        if (caught instanceof ApiError && caught.status === 401) {
          handleUnauthorized();
          return;
        }
        pushToast(
          caught instanceof ApiError ? caught.message : "刷新控制台数据失败",
          "error"
        );
      }
    },
    [handleUnauthorized, pushToast]
  );

  useEffect(() => {
    if (!token || overview) {
      setCheckingSession(false);
      return;
    }
    let cancelled = false;
    setCheckingSession(true);
    getAdminOverview(token)
      .then((data) => {
        if (!cancelled) {
          setOverview(data);
        }
      })
      .catch((caught) => {
        if (cancelled) return;
        if (caught instanceof ApiError && caught.status === 401) {
          handleUnauthorized();
        } else {
          clearSession("无法连接后台服务，请重新登录");
        }
      })
      .finally(() => {
        if (!cancelled) {
          setCheckingSession(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [token, overview, handleUnauthorized, clearSession]);

  const handleAuthenticated = (nextToken: string, initialOverview: AdminOverviewResponse) => {
    try {
      sessionStorage.setItem(ADMIN_TOKEN_STORAGE_KEY, nextToken);
    } catch {
      // ignore storage errors
    }
    setToken(nextToken);
    setOverview(initialOverview);
    setCheckingSession(false);
    pushToast("已登录商家履约工作台", "success");
  };

  const handleRefreshUpstream = async () => {
    if (!token) return;
    try {
      const refreshed = await refreshAdminUpstream(token);
      setOverview(refreshed);
      pushToast("已刷新上游余额与实时价格", "success");
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 401) {
        handleUnauthorized();
        return;
      }
      pushToast(
        caught instanceof ApiError ? caught.message : "刷新上游行情失败",
        "error"
      );
    }
  };

  const handleDataMutated = () => {
    setOrdersRefreshKey((prev) => prev + 1);
    if (token) {
      void fetchOverview(token);
    }
  };

  if (checkingSession) {
    return (
      <div className="login-shell">
        <div className="login-card card stack">
          <div className="skeleton" style={{ height: 26, width: "55%" }} />
          <div className="skeleton" style={{ height: 18, width: "80%" }} />
          <div className="skeleton" style={{ height: 48, width: "100%" }} />
        </div>
      </div>
    );
  }

  if (!token || !overview) {
    return <AdminLoginPage onAuthenticated={handleAuthenticated} />;
  }

  const navItems: Array<{
    id: AdminTab;
    label: string;
    icon: typeof LayoutDashboard;
    count?: number;
  }> = [
    { id: "overview", label: "总览监控", icon: LayoutDashboard },
    { id: "issue", label: "制码发码", icon: TicketPlus },
    {
      id: "orders",
      label: "订单保修",
      icon: ClipboardList,
      count: overview.counts.needs_manual_review,
    },
    { id: "settings", label: "熔断通知", icon: Sliders },
  ];

  return (
    <div className="admin-shell">
      <aside className="admin-rail" aria-label="商家后台侧栏">
        <div className="admin-rail-top-mobile">
          <Link to="/admin" className="admin-rail-brand" onClick={() => setActiveTab("overview")}>
            <span className="admin-rail-mark" aria-hidden="true">
              <ShieldCheck size={18} />
            </span>
            <div>
              <div className="admin-rail-title">Ultra 履约台</div>
              <div className="admin-rail-sub">Plan 2 自动提货</div>
            </div>
          </Link>

          <div className="admin-mobile-actions">
            <span
              className={`rail-mode-tag ${
                overview.execution_mode === "live" ? "live" : "dry"
              }`}
            >
              {overview.execution_mode === "live" ? "LIVE" : "DRY"}
            </span>
            <button
              type="button"
              className="rail-nav-btn"
              style={{ minHeight: 36, padding: "0 10px" }}
              aria-label="退出后台登录"
              onClick={() => clearSession("已退出商家后台")}
            >
              <LogOut size={15} />
            </button>
          </div>
        </div>

        <nav className="admin-rail-nav" aria-label="控制台功能切换">
          {navItems.map((navItem) => {
            const IconComponent = navItem.icon;
            const isActive = activeTab === navItem.id;
            return (
              <button
                key={navItem.id}
                type="button"
                className={`rail-nav-btn ${isActive ? "is-active" : ""}`}
                aria-current={isActive ? "page" : undefined}
                onClick={() => setActiveTab(navItem.id)}
              >
                <span className="rail-nav-left">
                  <IconComponent size={16} aria-hidden="true" />
                  <span>{navItem.label}</span>
                </span>
                {navItem.count !== undefined && navItem.count > 0 && (
                  <span className="rail-count" aria-label={`待处理 ${navItem.count} 项`}>
                    {navItem.count}
                  </span>
                )}
              </button>
            );
          })}
        </nav>

        <div className="admin-rail-footer">
          <div className="rail-mode-pill">
            <span>执行模式</span>
            <span
              className={`rail-mode-tag ${
                overview.execution_mode === "live" ? "live" : "dry"
              }`}
            >
              {overview.execution_mode === "live" ? "LIVE" : "DRY-RUN"}
            </span>
          </div>
          <Link to="/" className="rail-nav-btn">
            <span className="rail-nav-left">
              <ArrowLeft size={15} aria-hidden="true" />
              <span>返回买家页</span>
            </span>
          </Link>
          <button
            type="button"
            className="rail-nav-btn"
            onClick={() => clearSession("已退出商家后台")}
          >
            <span className="rail-nav-left">
              <LogOut size={15} aria-hidden="true" />
              <span>退出登录</span>
            </span>
          </button>
        </div>
      </aside>

      <main className="admin-workspace">
        {activeTab === "overview" && (
          <AdminOverviewPage
            overview={overview}
            onRefreshUpstream={handleRefreshUpstream}
            onJumpToOrders={(status) => {
              setOrderStatusFilter(status ?? "");
              setActiveTab("orders");
            }}
            onJumpToSettings={() => setActiveTab("settings")}
          />
        )}

        {activeTab === "issue" && (
          <AdminIssuePage
            token={token}
            overview={overview}
            onUnauthorized={handleUnauthorized}
            onIssued={handleDataMutated}
            onOpenCode={(issuedCode) => setSelectedCode(issuedCode)}
          />
        )}

        {activeTab === "orders" && (
          <AdminOrdersPage
            token={token}
            initialStatus={orderStatusFilter}
            refreshKey={ordersRefreshKey}
            onUnauthorized={handleUnauthorized}
            onSelectCode={(targetCode) => setSelectedCode(targetCode)}
          />
        )}

        {activeTab === "settings" && (
          <AdminSettingsPage
            token={token}
            overview={overview}
            onUnauthorized={handleUnauthorized}
            onUpdated={async () => {
              await fetchOverview(token);
            }}
          />
        )}
      </main>

      <AdminOrderDrawer
        token={token}
        code={selectedCode}
        onClose={() => setSelectedCode(null)}
        onUnauthorized={handleUnauthorized}
        onMutated={handleDataMutated}
      />
    </div>
  );
}
