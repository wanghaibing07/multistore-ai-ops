import { Link, Route, Routes } from "react-router-dom";
import { ArrowLeft, Compass, TicketCheck } from "lucide-react";
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
  return (
    <div className="app-shell">
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
