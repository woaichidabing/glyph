import { useEffect, useRef, useState } from "react";

type Props = {
  value: string;
  onChange: (v: string) => void;
  onSubmit: (v: string) => void;
  suggestions: string[];
  hot?: string[];
  autoFocus?: boolean;
  placeholder?: string;
};

/** 查词框：输入即联想，↑↓ 选、回车查、Esc 收；空态聚焦给热门词。 */
export function SearchBox({
  value,
  onChange,
  onSubmit,
  suggestions,
  hot = [],
  autoFocus,
  placeholder = "输入单词或短语，即输即查",
}: Props) {
  const [focused, setFocused] = useState(false);
  const [hotArmed, setHotArmed] = useState(false);
  const [idx, setIdx] = useState(-1);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (autoFocus) inputRef.current?.focus();
  }, [autoFocus]);

  useEffect(() => setIdx(-1), [value]);

  const list = value.trim() ? suggestions : [];
  // 输入联想随时给；空态热词只在用户主动点按输入框后展开，避免一进页面就挡住空态提示
  const showPanel = focused && (list.length > 0 || (hotArmed && !value.trim() && hot.length > 0));

  const commit = (w: string) => {
    onChange(w);
    onSubmit(w);
    setFocused(false);
    inputRef.current?.blur();
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setIdx((i) => Math.min(i + 1, list.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setIdx((i) => Math.max(i - 1, -1));
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (e.nativeEvent.isComposing) return; // 中文输入法组词时不触发
      commit(idx >= 0 && list[idx] ? list[idx] : value);
    } else if (e.key === "Escape") {
      setFocused(false);
      inputRef.current?.blur();
    }
  };

  return (
    <div className="dict-search-wrap">
      <div className={"cbox dict-box" + (focused ? " focus" : "")}>
        <span className="dict-glass" aria-hidden>
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg>
        </span>
        <input
          ref={inputRef}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          onFocus={() => setFocused(true)}
          onPointerDown={() => setHotArmed(true)}
          onBlur={() => window.setTimeout(() => setFocused(false), 120)}
          onKeyDown={onKeyDown}
          placeholder={placeholder}
          aria-label="查词"
        />
        {value ? (
          <button type="button" className="dict-clear" aria-label="清空"
            onClick={() => { onChange(""); inputRef.current?.focus(); }}>
            ✕
          </button>
        ) : null}
      </div>

      {showPanel ? (
        <div className="dict-suggest glass">
          {list.length > 0 ? (
            list.map((w, i) => (
              <button key={w} type="button"
                className={"dict-sg-item" + (i === idx ? " on" : "")}
                onMouseDown={(e) => { e.preventDefault(); commit(w); }}>
                {w}
              </button>
            ))
          ) : (
            <div className="dict-hotpanel">
              <div className="dict-hotlabel">常查的词</div>
              <div className="hot-row">
                {hot.map((w) => (
                  <button key={w} type="button" className="hot-chip"
                    onMouseDown={(e) => { e.preventDefault(); commit(w); }}>
                    {w}
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>
      ) : null}
    </div>
  );
}
