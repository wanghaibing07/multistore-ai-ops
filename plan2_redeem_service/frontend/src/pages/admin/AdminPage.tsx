import { useCallback, useEffect, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import {
  ApiError,
  getAdminOverview,
  refreshAdminUpstream,
} from "../../api/client";
import { CastoriceIcon, type CastoriceIconName } from "../../components/CastoriceIcon";
import { useToast } from "../../components/ToastProvider";
import type { AdminOrderStatusFilter, AdminOverviewResponse } from "../../types/api";
import "../../styles/admin-glass.css";
import { AdminIssuePage } from "./AdminIssuePage";
import { AdminLoginPage } from "./AdminLoginPage";
import { AdminOrderDrawer } from "./AdminOrderDrawer";
import { AdminOrdersPage } from "./AdminOrdersPage";
import { AdminOverviewPage } from "./AdminOverviewPage";
import { AdminSettingsPage } from "./AdminSettingsPage";
import { AdminWallpaperBackdrop } from "./AdminWallpaperBackdrop";

const ADMIN_TOKEN_STORAGE_KEY = "plan2_admin_token";

type AdminTab = "overview" | "issue" | "orders" | "settings";

const TAB_PATHS: Record<AdminTab, string> = {
  overview: "/admin",
  issue: "/admin/issue",
  orders: "/admin/orders",
  settings: "/admin/settings",
};

function resolveTabFromPath(pathname: string): AdminTab {
  const normalized = pathname.replace(/\/+$/, "").toLowerCase();
  if (normalized === "/admin/issue" || normalized.startsWith("/admin/issue/")) {
    return "issue";
  }
  if (
    normalized === "/admin/orders" ||
    normalized.startsWith("/admin/orders/") ||
    normalized === "/admin/codes" ||
    normalized.startsWith("/admin/codes/")
  ) {
    return "orders";
  }
  if (normalized === "/admin/settings" || normalized.startsWith("/admin/settings/")) {
    return "settings";
  }
  return "overview";
}

export function AdminPage() {
  const { pushToast } = useToast();
  const location = useLocation();
  const navigate = useNavigate();
  const [token, setToken] = useState<string | null>(() => {
    try {
      return sessionStorage.getItem(ADMIN_TOKEN_STORAGE_KEY);
    } catch {
      return null;
    }
  });
  const [overview, setOverview] = useState<AdminOverviewResponse | null>(null);
  const [checkingSession, setCheckingSession] = useState<boolean>(() => Boolean(token));
  const [activeTab, setActiveTab] = useState<AdminTab>(() =>
    resolveTabFromPath(location.pathname)
  );
  const [orderStatusFilter, setOrderStatusFilter] = useState<AdminOrderStatusFilter | "">("");
  const [selectedCode, setSelectedCode] = useState<string | null>(null);
  const [ordersRefreshKey, setOrdersRefreshKey] = useState(0);

  useEffect(() => {
    setActiveTab(resolveTabFromPath(location.pathname));
  }, [location.pathname]);

  const switchTab = useCallback(
    (nextTab: AdminTab) => {
      setActiveTab(nextTab);
      const targetPath = TAB_PATHS[nextTab];
      if (location.pathname !== targetPath) {
        navigate(targetPath);
      }
    },
    [location.pathname, navigate]
  );

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
    clearSession("登录已过期，请重新输入管理口令");
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
          caught instanceof ApiError ? caught.message : "刷新后台数据失败",
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
          clearSession("连不上后台服务，请重新登录");
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
    pushToast("已登录商家后台", "success");
  };

  const handleRefreshUpstream = async () => {
    if (!token) return;
    try {
      const refreshed = await refreshAdminUpstream(token);
      setOverview(refreshed);
      pushToast("已刷新上游余额和实时进价", "success");
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 401) {
        handleUnauthorized();
        return;
      }
      pushToast(
        caught instanceof ApiError ? caught.message : "刷新上游进价失败",
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

  const isAuthenticated = Boolean(!checkingSession && token && overview);

  if (!isAuthenticated || !token || !overview) {
    return (
      <div className="admin-glass-stage">
        <AdminWallpaperBackdrop variant="login" />
        {checkingSession ? (
          <div className="login-shell">
            <div className="login-card card admin-login-panel stack">
              <div className="skeleton" style={{ height: 26, width: "55%" }} />
              <div className="skeleton" style={{ height: 18, width: "80%" }} />
              <div className="skeleton" style={{ height: 48, width: "100%" }} />
            </div>
          </div>
        ) : (
          <AdminLoginPage embeddedBackdrop onAuthenticated={handleAuthenticated} />
        )}
      </div>
    );
  }

  const manualAttentionCount =
    overview.counts.manual_attention ??
    overview.counts.needs_manual_review +
      overview.counts.delivered_pending_warranty +
      (overview.counts.pending_warranty_ack ?? 0);

  const navItems: Array<{
    id: AdminTab;
    label: string;
    icon: CastoriceIconName;
    count?: number;
  }> = [
    { id: "overview", label: "总览", icon: "admin-overview" },
    { id: "issue", label: "发兑换码", icon: "admin-craft" },
    {
      id: "orders",
      label: "订单和保修",
      icon: "admin-ledger",
      count: manualAttentionCount,
    },
    { id: "settings", label: "价格与设置", icon: "admin-settings" },
  ];

  return (
    <div className="admin-glass-stage">
      <AdminWallpaperBackdrop variant="workspace" />
      <div className="admin-shell">
      <aside className="admin-rail" aria-label="商家后台侧栏">
        <div className="admin-rail-top-mobile">
          <Link
            to="/admin"
            className="admin-rail-brand"
            onClick={() => setActiveTab("overview")}
          >
            <span className="admin-rail-mark" aria-hidden="true">
              <CastoriceIcon name="admin-brand" size={32} />
            </span>
            <div>
              <div className="admin-rail-title">Ultra 20x 商家台</div>
              <div className="admin-rail-sub">自助提货后台</div>
            </div>
          </Link>

          <div className="admin-mobile-actions">
            <button
              type="button"
              className="rail-nav-btn"
              style={{ minHeight: 36, padding: "0 10px" }}
              aria-label="退出后台登录"
              onClick={() => clearSession("已退出商家后台")}
            >
              <CastoriceIcon name="admin-bye" size={20} />
            </button>
          </div>
        </div>

        <nav className="admin-rail-nav" aria-label="商家后台导航">
          {navItems.map((navItem) => {
            const isActive = activeTab === navItem.id;
            return (
              <button
                key={navItem.id}
                type="button"
                className={`rail-nav-btn ${isActive ? "is-active" : ""}`}
                aria-current={isActive ? "page" : undefined}
                onClick={() => switchTab(navItem.id)}
              >
                <span className="rail-nav-left">
                  <CastoriceIcon name={navItem.icon} size={22} />
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
          <Link to="/" className="rail-nav-btn">
            <span className="rail-nav-left">
              <CastoriceIcon name="arrow-left" size={18} />
              <span>返回买家页</span>
            </span>
          </Link>
          <button
            type="button"
            className="rail-nav-btn"
            onClick={() => clearSession("已退出商家后台")}
          >
            <span className="rail-nav-left">
              <CastoriceIcon name="admin-bye" size={20} />
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
              switchTab("orders");
            }}
            onJumpToSettings={() => switchTab("settings")}
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
    </div>
  );
}
