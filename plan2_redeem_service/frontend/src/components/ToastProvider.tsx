import { createContext, useCallback, useContext, useState, type ReactNode } from "react";
import { CastoriceIcon } from "./CastoriceIcon";

type ToastTone = "info" | "success" | "error";

interface ToastItem {
  id: number;
  message: string;
  tone: ToastTone;
}

interface ToastContextValue {
  pushToast: (message: string, tone?: ToastTone) => void;
}

const ToastContext = createContext<ToastContextValue>({
  pushToast: () => undefined,
});

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);

  const pushToast = useCallback((message: string, tone: ToastTone = "info") => {
    const id = Date.now() + Math.floor(Math.random() * 1000);
    setItems((prev) => [...prev.slice(-3), { id, message, tone }]);
    window.setTimeout(() => {
      setItems((prev) => prev.filter((item) => item.id !== id));
    }, 3200);
  }, []);

  return (
    <ToastContext.Provider value={{ pushToast }}>
      {children}
      <div className="toast-stack" role="status" aria-live="polite">
        {items.map((item) => (
          <div
            key={item.id}
            className={`toast ${
              item.tone === "success"
                ? "toast-success"
                : item.tone === "error"
                ? "toast-error"
                : ""
            }`}
          >
            {item.tone === "success" ? (
              <CastoriceIcon name="check" size={17} className="toast-icon-success" />
            ) : item.tone === "error" ? (
              <CastoriceIcon name="alert" size={17} className="toast-icon-error" />
            ) : (
              <CastoriceIcon name="emblem" size={18} className="toast-icon-info" />
            )}
            <span>{item.message}</span>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast(): ToastContextValue {
  return useContext(ToastContext);
}
