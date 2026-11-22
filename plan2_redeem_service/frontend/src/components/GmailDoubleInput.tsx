import { useId, useState } from "react";
import { AlertTriangle, CheckCircle2, ShieldAlert } from "lucide-react";
import { validateGmailInput } from "../utils/validation";

interface GmailDoubleInputProps {
  email: string;
  confirmEmail: string;
  onEmailChange: (value: string) => void;
  onConfirmEmailChange: (value: string) => void;
  primaryLabel?: string;
  confirmLabel?: string;
  warningText?: string;
  disabled?: boolean;
}

export function GmailDoubleInput({
  email,
  confirmEmail,
  onEmailChange,
  onConfirmEmailChange,
  primaryLabel = "接收家庭组邀请的 Gmail 邮箱",
  confirmLabel = "请再次手动输入 Gmail 确认",
  warningText = "请务必逐字核对 Gmail 拼写，确认无误后再提交，提交后无法自行修改邮箱。",
  disabled = false,
}: GmailDoubleInputProps) {
  const [pasteBlockedHint, setPasteBlockedHint] = useState(false);
  const [touchedPrimary, setTouchedPrimary] = useState(false);
  const [touchedConfirm, setTouchedConfirm] = useState(false);
  const baseId = useId();
  const primaryId = `${baseId}-primary`;
  const primaryErrorId = `${baseId}-primary-error`;
  const confirmId = `${baseId}-confirm`;
  const confirmErrorId = `${baseId}-confirm-error`;

  const check = validateGmailInput(email);
  const hasPrimary = email.trim().length > 0;
  const hasConfirm = confirmEmail.trim().length > 0;
  const emailsMatch =
    check.valid &&
    hasConfirm &&
    check.normalized.toLowerCase() === confirmEmail.trim().toLowerCase();
  const showPrimaryError = hasPrimary && !check.valid && (touchedPrimary || Boolean(check.suggestion));
  const showConfirmError = hasConfirm && !emailsMatch;

  return (
    <div className="stack">
      <div className="callout callout-warning callout-strong">
        <AlertTriangle size={18} className="callout-icon" />
        <div>{warningText}</div>
      </div>

      <div className="field-group">
        <label className="field-label" htmlFor={primaryId}>
          <span>{primaryLabel}</span>
          <span className="field-hint">仅支持 @gmail.com</span>
        </label>
        <input
          id={primaryId}
          type="email"
          inputMode="email"
          enterKeyHint="next"
          autoComplete="off"
          autoCapitalize="none"
          spellCheck={false}
          disabled={disabled}
          aria-invalid={showPrimaryError || undefined}
          aria-describedby={showPrimaryError ? primaryErrorId : undefined}
          className={`input mono ${
            showPrimaryError
              ? "input-error"
              : check.valid
              ? "input-success"
              : ""
          }`}
          placeholder="例如：yourname@gmail.com"
          value={email}
          onChange={(event) => onEmailChange(event.target.value)}
          onBlur={() => setTouchedPrimary(true)}
        />
        {showPrimaryError ? (
          <div id={primaryErrorId} className="field-error" role="alert">
            <ShieldAlert size={14} />
            <span>{check.error}</span>
            {check.suggestion ? (
              <button
                type="button"
                className="suggestion-chip"
                onClick={() => onEmailChange(check.suggestion || "")}
              >
                一键修正为 {check.suggestion}
              </button>
            ) : null}
          </div>
        ) : null}
        {check.valid ? (
          <div className="field-ok">
            <CheckCircle2 size={14} />
            <span>Gmail 格式校验通过</span>
          </div>
        ) : null}
      </div>

      <div className="field-group">
        <label className="field-label" htmlFor={confirmId}>
          <span>{confirmLabel}</span>
          <span className="field-hint">禁止粘贴以防错字</span>
        </label>
        <input
          id={confirmId}
          type="email"
          inputMode="email"
          enterKeyHint="done"
          autoComplete="off"
          autoCapitalize="none"
          spellCheck={false}
          disabled={disabled}
          aria-invalid={(showConfirmError && touchedConfirm) || undefined}
          aria-describedby={showConfirmError ? confirmErrorId : undefined}
          className={`input mono ${
            showConfirmError && touchedConfirm
              ? "input-error"
              : emailsMatch
              ? "input-success"
              : ""
          }`}
          placeholder="请手动再输入一次相同的 Gmail"
          value={confirmEmail}
          onChange={(event) => {
            setPasteBlockedHint(false);
            onConfirmEmailChange(event.target.value);
          }}
          onPaste={(event) => {
            event.preventDefault();
            setPasteBlockedHint(true);
          }}
          onBlur={() => setTouchedConfirm(true)}
        />
        {pasteBlockedHint ? (
          <p className="field-hint" style={{ color: "var(--warn)" }}>
            为避免复制带入拼写错误，确认框需手动再次输入一遍邮箱。
          </p>
        ) : null}
        {showConfirmError ? (
          <div id={confirmErrorId} className="field-error" role="alert">
            <ShieldAlert size={14} />
            <span>两次输入的 Gmail 不一致，请逐字母检查</span>
          </div>
        ) : null}
        {emailsMatch ? (
          <div className="field-ok">
            <CheckCircle2 size={14} />
            <span>两次输入完全一致，可以提交</span>
          </div>
        ) : null}
      </div>
    </div>
  );
}
