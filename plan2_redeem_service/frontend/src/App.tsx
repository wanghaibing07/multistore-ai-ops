import { useEffect, useState } from "react";
import { Link, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { ArrowLeft, Compass, TicketCheck } from "lucide-react";
import { isDemoMode, setDemoMode, syncDemoModeFromLocation } from "./api/client";
import { HomePage } from "./pages/HomePage";
import { RedeemPage } from "./pages/RedeemPage";
import { AdminPage } from "./pages/admin/AdminPage";

function NotFoundPage() {
  return (
    <div className="buyer-page">
      <div className="buyer-container">
        <header className="buyer-topbar">
          <Link to="/" className="brand-lockup">
            <span className="brand-mark" aria-hidden="true">
              <TicketCheck size={18} />
            </span>
            <span>VEO3 Ultra x20 自助提货</span>
          </Link>
          <Link to="/admin" className="btn btn-ghost btn-sm">
            商家后台
          </Link>
        </header>

        <main className="card stack">
          <div className="row">
            <Compass size={22} style={{ color: "var(--accent)" }} aria-hidden="true" />
            <h1 className="card-title">页面不存在</h1>
          </div>
          <p className="card-subtitle">
            当前访问路径没有对应的提货或管理页面，请检查链接是否完整，或返回首页重新输入兑换码。
          </p>
          <div className="row">
            <Link to="/" className="btn btn-primary">
              <ArrowLeft size={16} />
              <span>返回自助提货首页</span>
            </Link>
            <Link to="/admin" className="btn btn-secondary">
              <span>进入商家后台</span>
            </Link>
          </div>
        </main>
      </div>
    </div>
  );
}

export function App() {
  const location = useLocation();
  const navigate = useNavigate();
  const [demoActive, setDemoActive] = useState<boolean>(() => isDemoMode());

  useEffect(() => {
    const synced = syncDemoModeFromLocation();
    setDemoActive(synced);
  }, [location.search]);

  const handleExitDemo = () => {
    setDemoMode(false);
    setDemoActive(false);
    const params = new URLSearchParams(location.search);
    params.delete("demo");
    const nextSearch = params.toString();
    navigate(
      {
        pathname: location.pathname,
        search: nextSearch ? `?${nextSearch}` : "",
      },
      { replace: true }
    );
    window.location.reload();
  };

  return (
    <div className="app-shell">
      {demoActive && (
        <div className="demo-banner" role="region" aria-label="本地演练模式提示">
          <div className="demo-banner-inner">
            <span className="demo-banner-text">
              <span className="demo-pill-dot" aria-hidden="true" />
              <span>当前处于本地演练模式（Mock 数据），所有操作仅保存在当前浏览器内存，不会调用真实接口。</span>
            </span>
            <button type="button" className="demo-banner-action" onClick={handleExitDemo}>
              退出演练模式
            </button>
          </div>
        </div>
      )}

      <Routes>
        <Route path="/" element={<HomePage />} />
        <Route path="/r/:code" element={<RedeemPage />} />
        <Route path="/admin" element={<AdminPage />} />
        <Route path="/admin/*" element={<AdminPage />} />
        <Route path="*" element={<NotFoundPage />} />
      </Routes>
    </div>
  );
}
