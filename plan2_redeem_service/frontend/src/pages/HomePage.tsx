import { useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ArrowRight, KeyRound } from "lucide-react";
import { BuyerWallpaper } from "../components/BuyerWallpaper";
import { isImeComposing } from "../utils/ime";
import { extractRedeemCode } from "../utils/validation";

export function HomePage() {
  const [rawInput, setRawInput] = useState("");
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const navigate = useNavigate();

  const submitLookup = () => {
    const extracted = extractRedeemCode(rawInput);
    if (!extracted || extracted.length < 4) {
      setError("请输入有效的兑换码，或者直接粘贴客服发给你的提货链接");
      inputRef.current?.focus();
      return;
    }
    setError(null);
    navigate(`/r/${encodeURIComponent(extracted)}`);
  };

  const handleSubmit = (event: FormEvent) => {
    event.preventDefault();
    submitLookup();
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key !== "Enter") return;
    if (isImeComposing(event)) {
      event.preventDefault();
    }
  };

  return (
    <div className="zzz-home">
      <BuyerWallpaper variant="hero" />

      <div className="zzz-shell">
        <header className="zzz-topbar">
          <Link to="/" className="zzz-brand">
            <img
              src="/assets/generated/sticker-butterfly-emblem.png"
              alt=""
              aria-hidden="true"
              className="zzz-brand-emblem"
            />
            <span className="zzz-brand-copy">
              <span className="zzz-brand-title">Google AI Ultra 20x</span>
              <span className="zzz-brand-sub">自助提货</span>
            </span>
          </Link>
        </header>

        <main className="zzz-stage">
          <div className="zzz-hero-stack">
            <section className="zzz-glass-card" aria-labelledby="home-hero-heading">
              <div className="zzz-heading-block">
                <h1 id="home-hero-heading" className="zzz-hero-title">
                  Google AI Ultra 20x
                </h1>
                <p className="zzz-hero-subhead">输入兑换码，立即提货或查进度</p>
                <p className="zzz-hero-lead">
                  粘贴客服发给你的兑换码或提货链接，系统会自动识别。
                </p>
              </div>

              <form onSubmit={handleSubmit} className="zzz-form" noValidate>
                <label htmlFor="redeem-code-input" className="zzz-field-label">
                  <KeyRound size={15} aria-hidden="true" />
                  <span>兑换码或提货链接</span>
                </label>

                <div className="zzz-input-row">
                  <input
                    ref={inputRef}
                    id="redeem-code-input"
                    type="text"
                    enterKeyHint="go"
                    autoComplete="off"
                    spellCheck={false}
                    aria-invalid={Boolean(error) || undefined}
                    aria-describedby={error ? "redeem-code-error" : undefined}
                    className={`zzz-input mono ${error ? "is-error" : ""}`}
                    placeholder="例如：U20S-8K3M-9P2Q 或直接粘贴链接"
                    value={rawInput}
                    onKeyDown={handleKeyDown}
                    onChange={(event) => {
                      setRawInput(event.target.value);
                      if (error) setError(null);
                    }}
                  />

                  <button type="submit" className="zzz-submit-btn">
                    <span>查进度 / 去提货</span>
                    <ArrowRight size={17} aria-hidden="true" />
                  </button>
                </div>

                {error ? (
                  <div id="redeem-code-error" className="zzz-field-error" role="alert">
                    {error}
                  </div>
                ) : null}
              </form>

              <p className="zzz-card-note">提货后可收藏专属链接，随时回来查进度</p>
            </section>
          </div>
        </main>
      </div>
    </div>
  );
}
