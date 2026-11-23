import { useEffect, useState } from "react";
import { Link, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { ArrowLeft, Compass, TicketCheck } from "lucide-react";
import { isDemoMode, setDemoMode, syncDemoModeFromLocation } from "./api/client";
import { BuyerWallpaper } from "./components/BuyerWallpaper";
import { HomePage } from "./pages/HomePage";
import { RedeemPage } from "./pages/RedeemPage";
import { AdminPage } from "./pages/admin/AdminPage";

function NotFoundPage() {
  return (
    <div className="buyer-page">
      <BuyerWallpaper variant="detail" />
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
          <div className="row">
            <Compass size={22} style={{ color: "var(--accent)" }} aria-hidden="true" />
            <h1 className="card-title">找不到这个页面</h1>
          </div>
          <p className="card-subtitle">
            链接可能不完整或输错了，回首页重新输兑换码试试。
          </p>
          <div className="row">
            <Link to="/" className="btn btn-primary">
              <ArrowLeft size={16} />
              <span>回首页输兑换码</span>
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
    <div className={`app-shell ${demoActive ? "has-demo-banner" : ""}`}>
      {demoActive && (
        <div className="demo-banner" role="region" aria-label="本地演练模式提示">
          <div className="demo-banner-inner">
            <span className="demo-banner-text">
              <span className="demo-pill-dot" aria-hidden="true" />
              <span>正在用本地演练数据，不会真的下单或扣钱。</span>
            </span>
            <button type="button" className="demo-banner-action" onClick={handleExitDemo}>
              退出演练
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
