import { useEffect, useRef, useState } from "react";
import { useFocusTrap } from "../../hooks/useFocusTrap";
import { clearMemory, getMemoryCount, getTrashStats, purgeTrash } from "./memoryApi";
import {
  changeStorageRoot,
  getStorage,
  migrateStorage,
  openStorageFolder,
  type StorageInfo,
} from "./storageApi";

function fmtBytes(n: number): string {
  if (!Number.isFinite(n) || n <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return (i === 0 ? String(Math.round(v)) : v.toFixed(v >= 10 ? 0 : 1)) + " " + units[i];
}

export default function StoragePane() {
  const [info, setInfo] = useState<StorageInfo | null>(null);
  const [busy, setBusy] = useState<"load" | "open" | "migrate" | "save" | null>("load");
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const [dialog, setDialog] = useState(false);
  const [target, setTarget] = useState("");
  const [migrateOnChange, setMigrateOnChange] = useState(true);
  const [memCount, setMemCount] = useState(0);
  const [memDialog, setMemDialog] = useState(false);
  const [trash, setTrash] = useState({ count: 0, bytes: 0 });
  const [trashDialog, setTrashDialog] = useState(false);
  const memDialogRef = useRef<HTMLDivElement | null>(null);
  const trashDialogRef = useRef<HTMLDivElement | null>(null);
  useFocusTrap(memDialogRef, memDialog);
  useFocusTrap(trashDialogRef, trashDialog);

  const load = async () => {
    setBusy("load");
    setMsg(null);
    try {
      const next = await getStorage();
      setInfo(next);
      setMemCount(await getMemoryCount().catch(() => 0));
      setTrash(await getTrashStats().catch(() => ({ count: 0, bytes: 0 })));
    } catch (e) {
      setMsg({ kind: "err", text: e instanceof Error ? e.message : "读取存储信息失败。" });
    } finally {
      setBusy(null);
    }
  };

  useEffect(() => { void load(); }, []);

  // Esc 关掉弹窗（和项目里其它弹窗一致）
  useEffect(() => {
    if (!dialog && !memDialog && !trashDialog) return;
    const onKey = (ev: KeyboardEvent) => {
      if (ev.key !== "Escape") return;
      ev.preventDefault();
      setDialog(false);
      setMemDialog(false);
      setTrashDialog(false);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [dialog, memDialog, trashDialog]);

  const open = async () => {
    setBusy("open");
    setMsg(null);
    try {
      await openStorageFolder();
    } catch (e) {
      setMsg({ kind: "err", text: e instanceof Error ? e.message : "打开文件夹失败。" });
    } finally { setBusy(null); }
  };

  const migrate = async () => {
    setBusy("migrate");
    setMsg(null);
    try {
      const next = await migrateStorage(info?.legacy_root);
      setInfo(next);
      setMsg({ kind: "ok", text: "已复制 " + (next.copied_files ?? 0) + " 个文件。旧目录没有删除。" });
    } catch (e) {
      setMsg({ kind: "err", text: e instanceof Error ? e.message : "迁移失败。" });
    } finally { setBusy(null); }
  };

  const moveToDefault = async () => {
    setBusy("save");
    setMsg(null);
    try {
      const next = await changeStorageRoot(info!.default_root, true);
      setInfo(next);
      setMsg({ kind: "ok", text: "已迁到默认位置：" + next.root + "。旧目录没有删除。" });
    } catch (e) {
      setMsg({ kind: "err", text: e instanceof Error ? e.message : "迁移失败。" });
    } finally { setBusy(null); }
  };

  const saveRoot = async () => {
    const path = target.trim();
    if (!path) {
      setMsg({ kind: "err", text: "请填写数据目录路径。" });
      return;
    }
    setBusy("save");
    setMsg(null);
    try {
      const next = await changeStorageRoot(path, migrateOnChange);
      setInfo(next);
      setDialog(false);
      setMsg({ kind: "ok", text: "已切换到 " + next.root + "。新数据写入新目录，旧目录没有删除；建议重启应用让日志和已打开的会话完全切换。" });
    } catch (e) {
      setMsg({ kind: "err", text: e instanceof Error ? e.message : "切换失败。" });
    } finally { setBusy(null); }
  };

  if (!info) {
    return (
      <div className="stg">
        <div className="stg-loading">{busy === "load" ? "正在读取存储信息…" : "存储信息不可用"}</div>
        {msg && <div className={"set-msg " + msg.kind}>{msg.text}</div>}
      </div>
    );
  }

  const isLegacy = info.root === info.legacy_root;
  const isDefault = info.root === info.default_root;

  return (
    <div className="stg">
      <div className="stg-hero">
        <div className="stg-path-top">
          <div className="stg-eyebrow">当前数据位置</div>
          <span className="stg-badge">{isLegacy ? "旧目录" : isDefault ? "默认" : "自定义"}</span>
        </div>
        <div className="stg-path" title={info.root}>{info.root}</div>
        <p className="stg-note">会话、记忆、生词本、背词进度、计划，都在这个文件夹里。</p>
        <div className="stg-actions">
          <button className="stg-btn primary" type="button" onClick={() => {
            setTarget(info.root === info.legacy_root ? info.default_root : info.root);
            setMigrateOnChange(true);
            setDialog(true);
          }}>更改位置</button>
          <button className="stg-btn" type="button" onClick={() => void open()} disabled={busy !== null}>
            {busy === "open" ? "打开中…" : "打开文件夹"}
          </button>
        </div>
      </div>

      {isLegacy && (
        <div className="stg-stale">
          <span className="stg-stale-dot" />
          <div><b>当前还在用旧目录。</b> 新安装默认使用“文档 / Glyph”，可以一键迁过去。</div>
          <button type="button" onClick={() => void moveToDefault()} disabled={busy !== null}>迁到默认位置</button>
        </div>
      )}

      <div className="stg-stats">
        <div className="stg-stat"><span>占用空间</span><b>{fmtBytes(info.bytes)}</b></div>
        <div className="stg-stat"><span>会话数量</span><b>{info.sessions} 个</b></div>
        <div className="stg-stat"><span>目录状态</span><b>{info.writable ? "可用" : "不可写"}</b></div>
      </div>

      <div className="stg-section">
        <div className="stg-sec-head"><b>记忆</b><span>只影响 Agent 记住的内容，不动会话和学习记录</span></div>
        <div className="stg-row">
          <div><b>记忆</b><p>{memCount} 条 · Agent 记住的偏好与错题</p></div>
          <button className="stg-btn small danger" type="button" onClick={() => setMemDialog(true)} disabled={busy !== null || memCount === 0}>
            清空记忆
          </button>
        </div>
        <div className="stg-row">
          <div>
            <b>记忆回收站</b>
            <p>
              {trash.count > 0
                ? "已清空 " + trash.count + " 条 · 仍占 " + fmtBytes(trash.bytes)
                : "回收站是空的"}
            </p>
          </div>
          <button className="stg-btn small" type="button" onClick={() => setTrashDialog(true)} disabled={busy !== null || trash.count === 0}>
            清理回收站
          </button>
        </div>
        <p className="stg-note">回收站里超过 30 天的记忆会自动永久删除；这里也可以提前清。</p>
      </div>

      <div className="stg-section">
        <div className="stg-sec-head"><b>维护</b><span>不会动学习记录</span></div>
        {window.glyphDesktop ? (
          <div className="stg-row">
            <div><b>运行日志</b><p>出问题时，把这个文件夹里的日志发给我们。</p></div>
            <button
              className="stg-btn small"
              type="button"
              onClick={() => { void window.glyphDesktop?.openLogs(); }}
            >
              打开日志文件夹
            </button>
          </div>
        ) : null}
        <div className="stg-row">
          <div><b>旧数据迁移</b><p>从旧目录复制到当前目录，校验后也不删除旧数据。</p></div>
          <button className="stg-btn small" type="button" onClick={() => void migrate()} disabled={busy !== null}>
            {busy === "migrate" ? "迁移中…" : "开始"}
          </button>
        </div>
      </div>

      <div className="stg-footnote">
        换位置时先复制，不删旧目录；确认新位置可用后，再手动清理旧位置。
        当前目录{info.writable ? "可写" : "不可写，请换一个目录"}。
      </div>

      {msg && <div className={"set-msg " + msg.kind}>{msg.text}</div>}

      {memDialog && (
        <div className="stg-mask" onMouseDown={(e) => { if (e.target === e.currentTarget) setMemDialog(false); }}>
          <div className="stg-dialog" role="dialog" aria-modal="true" aria-label="清空记忆" ref={memDialogRef}>
            <div className="stg-dialog-head">
              <h3>清空 Agent 的记忆？</h3>
              <button type="button" className="stg-x" onClick={() => setMemDialog(false)}>×</button>
            </div>
            <p className="stg-dialog-sub">下面这些会被移走：</p>
            <div className="stg-mem-list">
              <div><span>记忆条数</span><b>{memCount} 条</b></div>
              <div><span>位置</span><b title={info.root}>{info.root + (info.root.includes("\\") ? "\\memory" : "/memory")}</b></div>
            </div>
            <p className="stg-dialog-sub">移到回收站（.trash），以后可以手动找回；Agent 之后不再记得你的偏好和错题。</p>
            <div className="stg-dialog-actions">
              <button className="stg-btn" type="button" autoFocus data-autofocus onClick={() => setMemDialog(false)}>取消</button>
              <button
                className="stg-btn danger"
                type="button"
                disabled={busy !== null}
                onClick={async () => {
                  setBusy("save");
                  try {
                    const removed = await clearMemory();
                    setMemCount(await getMemoryCount().catch(() => 0));
                    setMemDialog(false);
                    setMsg({ kind: "ok", text: "已清空，" + removed + " 条记忆移到了回收站。" });
                  } catch (e) {
                    setMsg({ kind: "err", text: e instanceof Error ? e.message : "清空失败。" });
                  } finally {
                    setBusy(null);
                  }
                }}
              >
                清空记忆
              </button>
            </div>
          </div>
        </div>
      )}

      {trashDialog && (
        <div className="stg-mask" onMouseDown={(e) => { if (e.target === e.currentTarget) setTrashDialog(false); }}>
          <div className="stg-dialog" role="dialog" aria-modal="true" aria-label="清理记忆回收站" ref={trashDialogRef}>
            <div className="stg-dialog-head">
              <h3>永久删除这 {trash.count} 条记忆？</h3>
              <button type="button" className="stg-x" onClick={() => setTrashDialog(false)}>×</button>
            </div>
            <p className="stg-dialog-sub">它们已经不在 Agent 的记忆里，只占着硬盘。</p>
            <div className="stg-mem-list">
              <div><span>条数</span><b>{trash.count} 条</b></div>
              <div><span>占用</span><b>{fmtBytes(trash.bytes)}</b></div>
              <div><span>位置</span><b title={info.root}>{info.root + (info.root.includes("\\") ? "\\memory\\.trash" : "/memory/.trash")}</b></div>
            </div>
            <p className="stg-dialog-sub" style={{ color: "var(--sem-red)" }}>删除后无法找回。</p>
            <div className="stg-dialog-actions">
              <button className="stg-btn" type="button" autoFocus data-autofocus onClick={() => setTrashDialog(false)}>取消</button>
              <button
                className="stg-btn danger"
                type="button"
                disabled={busy !== null}
                onClick={async () => {
                  setBusy("save");
                  try {
                    const out = await purgeTrash();
                    setTrash(await getTrashStats().catch(() => ({ count: 0, bytes: 0 })));
                    setTrashDialog(false);
                    setMsg({ kind: "ok", text: "已永久删除 " + out.removed + " 个文件，释放 " + fmtBytes(out.freed) + "。" });
                  } catch (e) {
                    setMsg({ kind: "err", text: e instanceof Error ? e.message : "清理失败。" });
                  } finally {
                    setBusy(null);
                  }
                }}
              >
                永久删除
              </button>
            </div>
          </div>
        </div>
      )}

      {dialog && (
        <div className="stg-mask" onMouseDown={(e) => { if (e.target === e.currentTarget) setDialog(false); }}>
          <div className="stg-dialog" role="dialog" aria-modal="true" aria-label="更改数据位置">
            <div className="stg-dialog-head">
              <h3>更改数据位置</h3>
              <button type="button" className="stg-x" onClick={() => setDialog(false)}>×</button>
            </div>
            <p className="stg-dialog-sub">选一个文件夹存放学习数据。路径要写绝对路径，例如 D:\GlyphData。</p>
            <label className="stg-label" htmlFor="storage-target">新位置</label>
            <input
              id="storage-target"
              className="stg-input"
              value={target}
              onChange={(e) => setTarget(e.target.value)}
              placeholder="D:\GlyphData"
              autoFocus
            />
            <label className="stg-check">
              <input type="checkbox" checked={migrateOnChange} onChange={(e) => setMigrateOnChange(e.target.checked)} />
              <span>把现有数据复制到新位置（旧目录不删除）</span>
            </label>
            <div className="stg-dialog-actions">
              <button className="stg-btn" type="button" onClick={() => setDialog(false)}>取消</button>
              <button className="stg-btn primary" type="button" onClick={() => void saveRoot()} disabled={busy !== null}>
                {busy === "save" ? "保存中…" : "保存并切换"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
