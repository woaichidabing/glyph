import { useStore } from "../../store";
import type {
  Density,
  EnglishFont,
  LineHeight,
  LineWidth,
  MarkStyle,
  TextSize,
  ThemeMode,
} from "../../store";

const THEME_OPTIONS: { id: ThemeMode; label: string }[] = [
  { id: "light", label: "浅色" },
  { id: "dark", label: "深色" },
  { id: "auto", label: "跟随" },
];

const FONT_OPTIONS: { id: EnglishFont; label: string }[] = [
  { id: "serif", label: "衬线" },
  { id: "sans", label: "无衬线" },
];

const TEXT_SIZE_OPTIONS: { id: TextSize; label: string }[] = [
  { id: "sm", label: "小" },
  { id: "md", label: "中" },
  { id: "lg", label: "大" },
];

const WIDTH_OPTIONS: { id: LineWidth; label: string }[] = [
  { id: "narrow", label: "窄" },
  { id: "standard", label: "标准" },
  { id: "wide", label: "宽" },
];

const LEADING_OPTIONS: { id: LineHeight; label: string }[] = [
  { id: "compact", label: "紧凑" },
  { id: "comfortable", label: "舒适" },
  { id: "loose", label: "宽松" },
];

const MARK_OPTIONS: { id: MarkStyle; label: string }[] = [
  { id: "underline", label: "下划线" },
  { id: "highlight", label: "浅色底" },
  { id: "high", label: "高对比" },
];

const DENSITY_OPTIONS: { id: Density; label: string }[] = [
  { id: "comfortable", label: "舒适" },
  { id: "compact", label: "紧凑" },
];

function SegRow<T extends string>(props: {
  title: string;
  desc: string;
  value: T;
  options: { id: T; label: string }[];
  onChange: (id: T) => void;
  ariaLabel: string;
}) {
  return (
    <div className="ap-row">
      <div className="ap-copy"><div className="ap-title">{props.title}</div><div className="ap-desc">{props.desc}</div></div>
      <div className="ap-control" role="group" aria-label={props.ariaLabel}>
        <div className="ap-seg">
          {props.options.map((o) => (
            <button
              key={o.id}
              type="button"
              className={props.value === o.id ? "on" : ""}
              aria-pressed={props.value === o.id}
              onClick={() => props.onChange(o.id)}
            >{o.label}</button>
          ))}
        </div>
      </div>
    </div>
  );
}

export default function AppearancePane() {
  const appearance = useStore((s) => s.appearance);
  const setAppearance = useStore((s) => s.setAppearance);

  return (
    <div className="ap">
      <div className="ap-rows">
        <SegRow
          title="外观主题"
          desc="浅色、深色，或跟随系统。"
          value={appearance.mode}
          options={THEME_OPTIONS}
          onChange={(mode) => setAppearance({ mode })}
          ariaLabel="外观主题"
        />
        <SegRow
          title="英文字体"
          desc="只影响英文正文，不影响按钮和菜单。"
          value={appearance.englishFont}
          options={FONT_OPTIONS}
          onChange={(englishFont) => setAppearance({ englishFont })}
          ariaLabel="英文字体"
        />
        <SegRow
          title="消息字号"
          desc="只影响消息正文和试读条。"
          value={appearance.textSize}
          options={TEXT_SIZE_OPTIONS}
          onChange={(textSize) => setAppearance({ textSize })}
          ariaLabel="消息字号"
        />
        <SegRow
          title="英文行宽"
          desc="每行最多放多少字符，影响长句换行。"
          value={appearance.lineWidth}
          options={WIDTH_OPTIONS}
          onChange={(lineWidth) => setAppearance({ lineWidth })}
          ariaLabel="英文行宽"
        />
        <SegRow
          title="行距"
          desc="字号够大但行距太挤时，调这里。"
          value={appearance.lineHeight}
          options={LEADING_OPTIONS}
          onChange={(lineHeight) => setAppearance({ lineHeight })}
          ariaLabel="行距"
        />
        <SegRow
          title="批改标记"
          desc="红色下划线、浅色底，或高对比。"
          value={appearance.markStyle}
          options={MARK_OPTIONS}
          onChange={(markStyle) => setAppearance({ markStyle })}
          ariaLabel="批改标记"
        />
        <div className="ap-row">
          <div className="ap-copy"><div className="ap-title">界面密度</div><div className="ap-desc">紧凑会把间距收一档，一屏多放些内容。</div></div>
          <div className="ap-control">
            <div className="ap-seg">
              {DENSITY_OPTIONS.map((o) => (
                <button
                  key={o.id}
                  type="button"
                  className={appearance.density === o.id ? "on" : ""}
                  aria-pressed={appearance.density === o.id}
                  onClick={() => setAppearance({ density: o.id })}
                >{o.label}</button>
              ))}
            </div>
          </div>
        </div>
        <div className="ap-row">
          <div className="ap-copy"><div className="ap-title">动画效果</div><div className="ap-desc">关掉后，弹窗和按钮的过渡会瞬间完成，界面更安静。</div></div>
          <div className="ap-control">
            <button
              type="button"
              role="switch"
              aria-checked={!appearance.reduceMotion}
              aria-label="动画效果"
              className={"ap-switch" + (appearance.reduceMotion ? "" : " on")}
              onClick={() => setAppearance({ reduceMotion: !appearance.reduceMotion })}
            />
          </div>
        </div>
      </div>

      <div className="ap-specimen-head"><span>试读</span><span>跟随当前设置</span></div>
      <p
        className={"ap-specimen" + " ap-font-" + appearance.englishFont + " ap-width-" + appearance.lineWidth
          + " ap-leading-" + appearance.lineHeight + " ap-size-" + appearance.textSize
          + " ap-mark-" + appearance.markStyle}
      >
        Hi Sam, I <span className="mk-bad">recieve</span> your message yesterday. Just following up on the draft we discussed — no rush at all.
      </p>
      <div className="ap-specimen-meta">
        <span>{appearance.englishFont === "serif" ? "衬线字体" : "无衬线字体"}</span>
        <span>{appearance.textSize === "sm" ? "小号字" : appearance.textSize === "lg" ? "大号字" : "中号字"}</span>
        <span>{appearance.lineWidth === "narrow" ? "窄行宽" : appearance.lineWidth === "wide" ? "宽行宽" : "标准行宽"}</span>
      </div>
    </div>
  );
}
