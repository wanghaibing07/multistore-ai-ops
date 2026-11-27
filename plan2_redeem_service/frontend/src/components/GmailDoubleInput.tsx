import { useId, useState } from "react";
import { validateGmailInput } from "../utils/validation";
import { CastoriceIcon } from "./CastoriceIcon";

interface GmailDoubleInputProps {
  email: string;
  onEmailChange: (value: string) => void;
  confirmEmail?: string;
  onConfirmEmailChange?: (value: string) => void;
  primaryLabel?: string;
  confirmLabel?: string;
  warningText?: string;
  helperText?: string;
  placeholder?: string;
  disabled?: boolean;
}

export function GmailDoubleInput({
  email,
  onEmailChange,
  primaryLabel = "你的邮箱",
  warningText = "提交就占名额，填错或受限换号需补 49.9 元，提交前请核对。",
  helperText,
  placeholder = "",
  disabled = false,
}: GmailDoubleInputProps) {
  const [touchedPrimary, setTouchedPrimary] = useState(false);
  const baseId = useId();
  const primaryId = `${baseId}-primary`;
  const helperId = `${baseId}-helper`;
  const primaryErrorId = `${baseId}-primary-error`;

  const check = validateGmailInput(email);
  const hasPrimary = email.trim().length > 0;
  const showPrimaryError = hasPrimary && !check.valid && (touchedPrimary || Boolean(check.suggestion));
  const describedBy = [helperText ? helperId : null, showPrimaryError ? primaryErrorId : null]
    .filter(Boolean)
    .join(" ");

  return (
    <div className="stack">
      <div className="callout callout-warning callout-strong">
        <CastoriceIcon name="alert" size={19} className="callout-icon" />
        <div>{warningText}</div>
      </div>

      <div className="field-group">
        <label className="field-label" htmlFor={primaryId}>
          <span>{primaryLabel}</span>
        </label>
        {helperText ? (
          <p id={helperId} className="field-helper">
            {helperText}
          </p>
        ) : null}
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
          aria-describedby={describedBy || undefined}
          className={`input mono ${
            showPrimaryError
              ? "input-error"
              : check.valid
              ? "input-success"
              : ""
          }`}
          placeholder={placeholder || undefined}
          value={email}
          onChange={(event) => onEmailChange(event.target.value)}
          onBlur={() => setTouchedPrimary(true)}
        />
        {showPrimaryError ? (
          <div id={primaryErrorId} className="field-error" role="alert">
            <CastoriceIcon name="alert" size={15} />
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
            <CastoriceIcon name="check" size={15} />
            <span>格式没问题</span>
          </div>
        ) : null}
      </div>
    </div>
  );
}

export const GmailSingleInput = GmailDoubleInput;
