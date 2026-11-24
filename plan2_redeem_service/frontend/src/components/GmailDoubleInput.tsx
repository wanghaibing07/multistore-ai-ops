import { useId, useState } from "react";
import { AlertTriangle, CheckCircle2, ShieldAlert } from "lucide-react";
import { validateGmailInput } from "../utils/validation";

interface GmailDoubleInputProps {
  email: string;
  onEmailChange: (value: string) => void;
  confirmEmail?: string;
  onConfirmEmailChange?: (value: string) => void;
  primaryLabel?: string;
  confirmLabel?: string;
  warningText?: string;
  placeholder?: string;
  disabled?: boolean;
}

export function GmailDoubleInput({
  email,
  onEmailChange,
  primaryLabel = "你的 Gmail 邮箱",
  warningText = "提交后如因买家原因换邮箱需要39.9手续费，点提交前仔细核对一遍拼写。",
  placeholder = "比如：yourname@gmail.com",
  disabled = false,
}: GmailDoubleInputProps) {
  const [touchedPrimary, setTouchedPrimary] = useState(false);
  const baseId = useId();
  const primaryId = `${baseId}-primary`;
  const primaryErrorId = `${baseId}-primary-error`;

  const check = validateGmailInput(email);
  const hasPrimary = email.trim().length > 0;
  const showPrimaryError = hasPrimary && !check.valid && (touchedPrimary || Boolean(check.suggestion));

  return (
    <div className="stack">
      <div className="callout callout-warning callout-strong">
        <AlertTriangle size={18} className="callout-icon" />
        <div>{warningText}</div>
      </div>

      <div className="field-group">
        <label className="field-label" htmlFor={primaryId}>
          <span>{primaryLabel}</span>
          <span className="field-hint">只支持 @gmail.com</span>
        </label>
        <input
          id={primaryId}
          type="email"
          inputMode="email"
          enterKeyHint="done"
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
          placeholder={placeholder}
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
                一键改成 {check.suggestion}
              </button>
            ) : null}
          </div>
        ) : null}
        {check.valid ? (
          <div className="field-ok">
            <CheckCircle2 size={14} />
            <span>格式没问题，核对一遍就能提交</span>
          </div>
        ) : null}
      </div>
    </div>
  );
}

export const GmailSingleInput = GmailDoubleInput;
