import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { App } from "./App";
import { syncDemoModeFromLocation } from "./api/client";
import { ToastProvider } from "./components/ToastProvider";
import "./styles/theme.css";

syncDemoModeFromLocation();

const rootElement = document.getElementById("root");
if (!rootElement) {
  throw new Error("未找到应用挂载节点 #root");
}

createRoot(rootElement).render(
  <StrictMode>
    <BrowserRouter>
      <ToastProvider>
        <App />
      </ToastProvider>
    </BrowserRouter>
  </StrictMode>
);
