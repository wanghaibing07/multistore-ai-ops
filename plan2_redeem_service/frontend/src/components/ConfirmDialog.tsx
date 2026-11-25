import { useEffect, useId, useRef, type ReactNode } from "react";
import { CastoriceIcon } from "./CastoriceIcon";

interface ConfirmDialogProps {
  open: boolean;
  title: string;
  confirmLabel: string;
  cancelLabel?: string;
  confirmVariant?: "primary" | "danger";
  busy?: boolean;
  onConfirm: () => void;
  onClose: () => void;
  children: ReactNode;
}

export function ConfirmDialog({
  open,
  title,
  confirmLabel,
  cancelLabel = "取消",
  confirmVariant = "primary",
  busy = false,
  onConfirm,
  onClose,
  children,
}: ConfirmDialogProps) {
  const dialogRef = useRef<HTMLDialogElement | null>(null);
  const titleId = useId();

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open) {
      if (!dialog.open) {
        dialog.showModal();
      }
    } else if (dialog.open) {
      dialog.close();
    }
  }, [open]);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;

    const handleCancel = (event: Event) => {
      if (busy) {
        event.preventDefault();
        return;
      }
      onClose();
    };

    const handleClose = () => {
      dialog.returnValue = "";
      onClose();
    };

    dialog.addEventListener("cancel", handleCancel);
    dialog.addEventListener("close", handleClose);
    return () => {
      dialog.removeEventListener("cancel", handleCancel);
      dialog.removeEventListener("close", handleClose);
    };
  }, [busy, onClose]);

  const handleBackdropClick = (event: React.MouseEvent<HTMLDialogElement>) => {
    if (busy) return;
    const dialog = dialogRef.current;
    if (!dialog || event.target !== dialog) return;
    const rect = dialog.getBoundingClientRect();
    const clickedOutside =
      event.clientX < rect.left ||
      event.clientX > rect.right ||
      event.clientY < rect.top ||
      event.clientY > rect.bottom;
    if (clickedOutside) {
      dialog.close();
    }
  };

  return (
    <dialog
      ref={dialogRef}
      className="native-dialog"
      aria-labelledby={titleId}
      onClick={handleBackdropClick}
    >
      <div className="dialog-body">
        <div className="dialog-header">
          <div className="dialog-title-row">
            <CastoriceIcon
              name={confirmVariant === "danger" ? "admin-alert" : "alert"}
              size={confirmVariant === "danger" ? 26 : 22}
              style={{ flex: "0 0 auto" }}
            />
            <h3 id={titleId} className="card-title" style={{ fontSize: 16.5 }}>
              {title}
            </h3>
          </div>
          <button
            type="button"
            className="btn btn-ghost btn-icon"
            aria-label="关闭弹窗"
            disabled={busy}
            onClick={() => dialogRef.current?.close()}
          >
            <CastoriceIcon name="close" size={19} />
          </button>
        </div>

        <div className="stack-sm">{children}</div>

        <div className="dialog-actions">
          <button
            type="button"
            className="btn btn-secondary"
            autoFocus
            disabled={busy}
            onClick={() => dialogRef.current?.close()}
          >
            {cancelLabel}
          </button>
          <button
            type="button"
            className={`btn ${confirmVariant === "danger" ? "btn-danger-solid" : "btn-primary"} ${
              busy ? "is-loading" : ""
            }`}
            disabled={busy}
            aria-busy={busy}
            onClick={onConfirm}
          >
            {busy ? (
              <>
                <CastoriceIcon name="refresh" size={16} spin />
                <span>处理中...</span>
              </>
            ) : (
              <span>{confirmLabel}</span>
            )}
          </button>
        </div>
      </div>
    </dialog>
  );
}
