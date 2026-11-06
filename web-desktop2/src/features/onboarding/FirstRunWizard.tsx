/* ============================================================
 * 首启动向导（照 DSH 的三段式版式）：
 *   只配我们内置的千问档，不让用户选供应商；
 *   跳过 = 他自己有安排，以后不再打扰（他可以去设置里配）。
 * ============================================================ */
import { useCallback, useEffect, useRef, useState } from "react";
import "./onboarding.css";
import { activateProvider, listProviders, saveProvider } from "../modelSettings/providersRepo";

/** 跳过过就记住，下次不再弹（用户明确说"不用管"） */
const SKIP_KEY = "glyph.onboarding.skipped.v1";

function validateKey(raw: string): string | null {
  const value = raw.trim();
  if (!value) return "请填写 API Key。";
  if (/^[A-Za-z_][A-Za-z0-9_]*\s*=/.test(value)) {
    return "这里是 Key 本身，不要带引号、空格，也不要带「变量名=」这种写法。";
  }
  if (/[\s"']/.test(value)) return "这里是 Key 本身，不要带引号或空格。";
  return null;
}

const FOCUSABLE = 'button:not([disabled]), input, select, textarea, a[href], [tabindex]:not([tabindex="-1"])';

export default function FirstRunWizard({ onDone }: { onDone?: () => void }) {
  const [open, setOpen] = useState(false);
  const [step, setStep] = useState<"welcome" | "key">("welcome");
  const [askSkip, setAskSkip] = useState(false);
  const [key, setKey] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const screenRef = useRef<HTMLDivElement | null>(null);
  const skipRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    let alive = true;
    if (import.meta.env.DEV && new URLSearchParams(location.search).get("onboarding") === "1") {
      setOpen(true);
      return;
    }
    if (localStorage.getItem(SKIP_KEY)) return;
    void listProviders()
      .then((data) => {
        if (!alive) return;
        if (data.providers.some((p) => p.keyConfigured)) return;
        setOpen(true);
      })
      .catch(() => { if (alive) setOpen(true); });
    return () => { alive = false; };
  }, []);

  useEffect(() => {
    if (!open) return;
    (askSkip ? skipRef.current : screenRef.current)?.focus();
  }, [open, step, askSkip]);

  useEffect(() => {
    if (!open) return;
    const onKey = (ev: KeyboardEvent) => {
      if (ev.key !== "Escape") return;
      ev.preventDefault();
      setAskSkip((v) => !v);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open]);

  const trapTab = useCallback((ev: React.KeyboardEvent) => {
    if (ev.key !== "Tab") return;
    const scope = askSkip ? skipRef.current : screenRef.current;
    if (!scope) return;
    const items = [...scope.querySelectorAll<HTMLElement>(FOCUSABLE)].filter((el) => el.offsetParent !== null);
    if (!items.length) return;
    const first = items[0];
    const last = items[items.length - 1];
    if (ev.shiftKey && document.activeElement === first) { ev.preventDefault(); last.focus(); }
    else if (!ev.shiftKey && document.activeElement === last) { ev.preventDefault(); first.focus(); }
  }, [askSkip]);

  const save = async () => {
    const bad = validateKey(key);
    if (bad) { setError(bad); return; }
    setBusy(true);
    setError(null);
    try {
      const data = await listProviders();
      // 只补钥匙，不动内置档的协议 / 地址 / 模型清单（那些是它本来就配好的）
      const builtin = data.providers.find((p) => p.kind === "builtin") ?? data.providers[0];
      if (!builtin) throw new Error("没有找到内置的千问档，请到「设置 → 模型」里手动添加。");
      await saveProvider({
        revision: data.revision,
        id: builtin.id,
        name: builtin.name,
        kind: builtin.kind,
        api: builtin.api,
        baseUrl: builtin.baseURL,
        apiKey: key.trim(),
        models: builtin.models,
        roles: builtin.roles,
      });
      await activateProvider(builtin.id);
      setOpen(false);
      onDone?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : "没能保存，请再试一次。");
    } finally {
      setBusy(false);
    }
  };

  const skipForGood = () => {
    localStorage.setItem(SKIP_KEY, "1");
    setAskSkip(false);
    setOpen(false);
  };

  if (!open) return null;

  return (
    <div
      className="ob-root"
      role="presentation"
      onClick={(ev) => { if (ev.target === ev.currentTarget) setAskSkip(true); }}
    >
      <div ref={screenRef} tabIndex={-1} className="ob-welcome" role="dialog" aria-modal="true" aria-labelledby="ob-title" onKeyDown={trapTab}>
        <div className="ob-brand" aria-label="Glyph">
          <img src="/icon.png" alt="" width={36} height={36} />
          <span>Glyph</span>
        </div>

        {step === "welcome" ? (
          <div className="ob-tagline">
            <h1 id="ob-title">欢迎使用 <em>Glyph</em></h1>
            <p>Glyph 在你自己的电脑上运行，批改、讲解、规划都在本地完成。<br />先接上一个模型，就能开始用。</p>
          </div>
        ) : (
          <form
            className="ob-key-form"
            noValidate
            onSubmit={(ev) => { ev.preventDefault(); void save(); }}
            aria-busy={busy}
          >
            <header className="ob-key-heading">
              <h1 id="ob-title">添加一个 API Key 开始使用</h1>
              <p>填千问（DashScope）的 Key 就能开始用。</p>
            </header>

            <div className="ob-field">
              <label className="ob-visually-hidden" htmlFor="ob-key">API Key</label>
              <input
                id="ob-key"
                className="ob-input"
                type="password"
                autoComplete="new-password"
                autoCapitalize="off"
                spellCheck={false}
                required
                aria-describedby="ob-key-error"
                aria-invalid={error ? "true" : undefined}
                placeholder="粘贴千问 API Key"
                value={key}
                onChange={(e) => { setKey(e.target.value); setError(null); }}
                disabled={busy}
              />
              <p id="ob-key-error" className="ob-key-error" role="alert" hidden={!error}>{error}</p>
            </div>

            <p className="ob-key-note">
              想用别家的模型？点「跳过」，之后到「设置 → 模型」里自己配。
            </p>
          </form>
        )}

        <div className="ob-actions">
          {step === "welcome" ? (
            <>
              <button className="ob-primary" type="button" onClick={() => setStep("key")}>开始设置</button>
              <button className="ob-back" type="button" onClick={() => setAskSkip(true)}>跳过</button>
            </>
          ) : (
            <>
              <button className="ob-primary" type="button" onClick={() => void save()} disabled={busy}>
                {busy ? "正在保存…" : "保存并继续"}
              </button>
              <button className="ob-secondary" type="button" onClick={() => setAskSkip(true)} disabled={busy}>稍后设置</button>
              <button className="ob-back" type="button" onClick={() => setStep("welcome")}>返回</button>
            </>
          )}
        </div>
      </div>

      {askSkip ? (
        <div className="ob-mask" role="presentation" onClick={(ev) => { if (ev.target === ev.currentTarget) setAskSkip(false); }}>
          <div ref={skipRef} tabIndex={-1} className="ob-dialog" role="alertdialog" aria-modal="true" aria-labelledby="ob-skip-title" onKeyDown={trapTab}>
            <h3 id="ob-skip-title">跳过设置？</h3>
            <p>跳过之后不会再自动提醒；想用的时候到「设置 → 模型」里配一个就行。</p>
            <div className="ob-dialog-actions">
              <button className="ob-secondary" type="button" onClick={skipForGood}>仍然跳过</button>
              <button className="ob-primary" type="button" autoFocus onClick={() => setAskSkip(false)}>继续设置</button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
