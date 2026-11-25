import type { MouseEvent } from "react";
import { Link, Route, Routes, useNavigate } from "react-router-dom";
import { ButterflyBurstFX } from "./components/ButterflyBurstFX";
import { BuyerWallpaper } from "./components/BuyerWallpaper";
import { CastoriceIcon } from "./components/CastoriceIcon";
import { HomePage } from "./pages/HomePage";
import { RedeemPage } from "./pages/RedeemPage";
import { AdminPage } from "./pages/admin/AdminPage";
import { runRedeemViewTransition } from "./utils/viewTransition";

function NotFoundPage() {
  const navigate = useNavigate();

  const handleBackHome = (event: MouseEvent<HTMLAnchorElement>) => {
    if (
      event.defaultPrevented ||
      event.button !== 0 ||
      event.metaKey ||
      event.altKey ||
      event.ctrlKey ||
      event.shiftKey
    ) {
      return;
    }
    event.preventDefault();
    runRedeemViewTransition(() => navigate("/"), "back");
  };

  return (
    <div className="buyer-page">
      <BuyerWallpaper variant="detail" />
      <div className="buyer-container">
        <header className="buyer-topbar">
          <Link to="/" className="brand-lockup" onClick={handleBackHome}>
            <span className="brand-mark" aria-hidden="true">
              <CastoriceIcon name="emblem" size={30} />
            </span>
            <span>Google AI Ultra 20x 自助提货</span>
          </Link>
        </header>

        <main className="card stack redeem-card-morph">
          <div className="row">
            <CastoriceIcon name="alert" size={24} />
            <h1 className="card-title">找不到这个页面</h1>
          </div>
          <p className="card-subtitle">
            链接可能不完整或输错了，回首页重新输兑换码试试。
          </p>
          <div className="row">
            <Link to="/" className="btn btn-primary" onClick={handleBackHome}>
              <CastoriceIcon name="arrow-left" size={16} />
              <span>回首页输兑换码</span>
            </Link>
          </div>
        </main>
      </div>
    </div>
  );
}

export function App() {
  return (
    <div className="app-shell">
      <ButterflyBurstFX />
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
