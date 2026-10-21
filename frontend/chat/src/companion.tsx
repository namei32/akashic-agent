import { forwardRef, useCallback, useEffect, useImperativeHandle, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { ArrowDownLeft, ArrowUp, Check, ChevronLeft, Copy, Grip, MessageCircle, Minus, Settings2, Square, X } from "lucide-react";
import { createUuid } from "./browser-uuid";
import { copyText } from "./copy-text";
import { StaticMessageResponse } from "./static-message-response";
import { companionApi, CompanionRequestError, readCompanionRun, type CompanionMessage, type CompanionReference, type CompanionRequest, type CompanionRun, type CompanionState } from "./companion-api";
import "./companion.css";

export interface CompanionHandle { explain(reference: CompanionReference): void }

function stored(key: string): string {
  try { return localStorage.getItem(key) ?? ""; } catch { return ""; }
}
function remember(key: string, value: string) {
  try { localStorage.setItem(key, value); } catch { /* Storage failure must not prevent asking. */ }
}
const clamp = (value: number, low: number, high: number) => Math.max(low, Math.min(value, Math.max(low, high)));

function initialPosition(): { x: number; y: number } {
  try {
    const saved = JSON.parse(stored("akashic.companion.position")) as { x: number; y: number };
    if (Number.isFinite(saved.x) && Number.isFinite(saved.y)) return {
      x: clamp(saved.x * innerWidth, 12, innerWidth - 76), y: clamp(saved.y * innerHeight, 12, innerHeight - 180),
    };
  } catch { /* First visit. */ }
  return { x: Math.max(12, innerWidth - 88), y: Math.max(12, innerHeight - 220) };
}

export const Companion = forwardRef<CompanionHandle, {
  parent: string;
  onAvailable: (available: boolean) => void;
  onQuote: (text: string) => void;
}>(function Companion({ parent, onAvailable, onQuote }, ref) {
  const [available, setAvailable] = useState(false);
  const [open, setOpen] = useState(false);
  const [settings, setSettings] = useState(false);
  const [history, setHistory] = useState<CompanionMessage[]>([]);
  const [reference, setReference] = useState<CompanionReference | null>(null);
  const [preferences, setPreferences] = useState("");
  const [preferenceDraft, setPreferenceDraft] = useState("");
  const [draft, setDraft] = useState(() => stored(`akashic.companion.draft:${parent}`));
  const [live, setLive] = useState<CompanionRun | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [saving, setSaving] = useState(false);
  const [position, setPosition] = useState(initialPosition);
  const [viewport, setViewport] = useState({ width: innerWidth, height: innerHeight });
  const mounted = useRef(true);
  const sceneRevision = useRef(0);
  const stream = useRef<AbortController | null>(null);
  const lastRequest = useRef<CompanionRequest | null>(null);
  const feed = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const follow = useRef(true);
  const drag = useRef<{ x: number; y: number; left: number; top: number; moved: boolean } | null>(null);
  const dragged = useRef(false);
  const busy = live?.status === "running";

  const refresh = useCallback(async () => {
    const revision = sceneRevision.current;
    try {
      const state = await companionApi<CompanionState>(`/state?parent=${encodeURIComponent(parent)}`);
      if (!mounted.current) return null;
      setAvailable(true); onAvailable(true); setHistory(state.history);
      setPreferences(state.preferences.explanation); setPreferenceDraft(state.preferences.explanation);
      if (revision === sceneRevision.current) setReference(state.reference);
      return state;
    } catch (reason) {
      if (!mounted.current) return null;
      const exists = !(reason instanceof CompanionRequestError && reason.status === 404);
      setAvailable(exists); onAvailable(exists);
      if (exists) setError(reason instanceof Error ? reason.message : "读取桌宠失败，请重试。");
      return null;
    }
  }, [parent, onAvailable]);

  const watch = useCallback(async (id: string) => {
    stream.current?.abort();
    const control = new AbortController(); stream.current = control;
    setError("");
    try {
      await readCompanionRun(id, (value) => { if (mounted.current) setLive(value); }, control.signal);
      if (mounted.current) await refresh();
    } catch (reason) {
      if (mounted.current && !control.signal.aborted) setError(reason instanceof Error ? reason.message : "答疑连接中断，请重连。");
    }
  }, [refresh]);

  useEffect(() => {
    mounted.current = true;
    void refresh().then((state) => {
      if (state?.active && mounted.current) { setLive(state.active); void watch(state.active.request_id); }
    });
    return () => { mounted.current = false; stream.current?.abort(); };
  }, [refresh, watch]);

  useEffect(() => { remember(`akashic.companion.draft:${parent}`, draft); }, [draft, parent]);
  useEffect(() => {
    const resize = () => {
      const height = window.visualViewport?.height ?? innerHeight;
      setViewport({ width: innerWidth, height });
      setPosition((p) => ({ x: clamp(p.x, 12, innerWidth - 76), y: clamp(p.y, 12, height - 160) }));
    };
    window.addEventListener("resize", resize); window.visualViewport?.addEventListener("resize", resize);
    return () => { window.removeEventListener("resize", resize); window.visualViewport?.removeEventListener("resize", resize); };
  }, []);
  useEffect(() => {
    if (open && !settings && follow.current && feed.current) feed.current.scrollTop = feed.current.scrollHeight;
  }, [open, settings, history, live]);
  useEffect(() => {
    if (!open) return;
    const close = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !event.isComposing) { event.stopPropagation(); setOpen(false); }
    };
    document.addEventListener("keydown", close, true);
    return () => document.removeEventListener("keydown", close, true);
  }, [open]);

  const changeReference = useCallback((value: CompanionReference | null) => {
    sceneRevision.current += 1;
    setReference(value); setError("");
    void companionApi("/scene", { parent, reference: value }).catch((reason: unknown) => {
      if (mounted.current) setError(reason instanceof Error ? reason.message : "引用保存失败。");
    });
  }, [parent]);

  useImperativeHandle(ref, () => ({ explain(value) {
    changeReference(value); setOpen(true); setSettings(false);
    if (matchMedia("(pointer: fine)").matches) requestAnimationFrame(() => input.current?.focus({ preventScroll: true }));
  } }), [changeReference]);

  async function submit(request?: CompanionRequest) {
    if (busy && !request) return;
    const value = request ?? { request_id: createUuid(), parent, question: draft.trim(), reference };
    if (!value.question) return;
    lastRequest.current = value; follow.current = true; setError(""); setNotice("");
    setLive({ request_id: value.request_id, status: "running", text: "", detail: "正在理解你的问题…" });
    try {
      const result = await companionApi<CompanionRun>("/ask", value);
      if (!mounted.current) return;
      setDraft(""); setLive(result); await refresh();
      if (result.status === "running") await watch(value.request_id);
    } catch (reason) {
      if (mounted.current) setError(reason instanceof Error ? reason.message : "提问失败，请重试。");
    }
  }

  async function stop() {
    if (!live) return;
    try {
      const value = await companionApi<CompanionRun>(`/stop/${encodeURIComponent(live.request_id)}`, {});
      if (mounted.current) { setLive(value); await refresh(); }
    } catch (reason) { setError(reason instanceof Error ? reason.message : "停止失败，请重试。"); }
  }

  async function savePreferences() {
    setSaving(true); setError("");
    try {
      const value = await companionApi<{ explanation: string }>("/preferences", { explanation: preferenceDraft.trim() });
      if (mounted.current) { setPreferences(value.explanation); setNotice("解释偏好已保存，下次提问生效。"); }
    } catch (reason) { setError(reason instanceof Error ? reason.message : "保存失败，请重试。"); }
    finally { if (mounted.current) setSaving(false); }
  }

  async function copy(value: string) {
    try { await copyText(value); setNotice("已复制解释。"); }
    catch { setError("复制失败，可以选中文字复制。"); }
  }

  if (!available) return null;
  const panelWidth = Math.min(380, viewport.width - 24);
  const panelHeight = Math.min(540, viewport.height - 32);
  const panelPosition = { left: clamp(position.x + 62 - panelWidth, 12, viewport.width - panelWidth - 12),
    top: clamp(position.y - panelHeight - 12, 12, viewport.height - panelHeight - 12), width: panelWidth, height: panelHeight };
  const floating = live && !history.some((m) => m.id === `${live.request_id}:answer`) ? live.text : "";
  const completed = live?.status === "complete" && !open;

  return createPortal(<div className="companion-root">
    <button type="button" className={`companion-launcher ${busy ? "is-thinking" : ""} ${completed ? "has-answer" : ""}`}
      style={{ left: position.x, top: position.y }} aria-label={open ? "收起随问桌宠" : "打开随问桌宠"}
      aria-expanded={open} aria-controls="companion-dialog" title="点击随时问，拖动调整位置"
      onPointerDown={(event) => {
        if (event.button !== 0) return;
        event.currentTarget.setPointerCapture(event.pointerId);
        drag.current = { x: event.clientX, y: event.clientY, left: position.x, top: position.y, moved: false };
      }}
      onPointerMove={(event) => {
        const start = drag.current;
        if (!start) return;
        const dx = event.clientX - start.x, dy = event.clientY - start.y;
        start.moved ||= Math.abs(dx) + Math.abs(dy) > 5;
        if (start.moved) {
          const composerTop = document.querySelector(".composer-wrap")?.getBoundingClientRect().top ?? viewport.height - 100;
          setPosition({ x: clamp(start.left + dx, 12, viewport.width - 76), y: clamp(start.top + dy, 12, composerTop - 80) });
        }
      }}
      onPointerUp={() => {
        dragged.current = Boolean(drag.current?.moved); drag.current = null;
        if (dragged.current) remember("akashic.companion.position", JSON.stringify({ x: position.x / innerWidth, y: position.y / innerHeight }));
      }}
      onPointerCancel={() => { drag.current = null; }}
      onClick={() => { if (dragged.current) { dragged.current = false; return; } setOpen((value) => !value); setNotice(""); }}>
      <span className="companion-creature" aria-hidden="true"><i className="companion-ear left" /><i className="companion-ear right" />
        <span className="companion-face"><i /><i /></span><span className="companion-mouth" /></span>
      <span className="companion-caption">{busy ? "在想了" : completed ? "讲完啦" : "随问"}</span>
    </button>
    {open ? <section id="companion-dialog" className="companion-dialog" role="dialog" aria-label="随问桌宠" style={panelPosition}>
      <header className="companion-header">
        <div><strong>随问</strong><span>{settings ? "按你喜欢的方式解释" : "随手问，不用翻回去"}</span></div>
        <button type="button" aria-label={settings ? "返回答疑" : "解释偏好"} onClick={() => { setSettings((v) => !v); setNotice(""); }}>
          {settings ? <ChevronLeft size={17} /> : <Settings2 size={17} />}</button>
        <button type="button" aria-label="收起桌宠小窗" onClick={() => setOpen(false)}><Minus size={19} /></button>
      </header>
      {settings ? <div className="companion-settings">
        <label htmlFor="companion-preferences">你希望我怎样解释？</label>
        <p>只记住你明确设置的偏好，随时可以修改或清空。</p>
        <textarea id="companion-preferences" value={preferenceDraft} maxLength={500} rows={5}
          placeholder="例如：先用生活例子解释，再给代码；回答简短一些。" onChange={(event) => setPreferenceDraft(event.target.value)} />
        <button className="companion-primary" type="button" disabled={saving || preferences === preferenceDraft.trim()} onClick={() => void savePreferences()}>
          <Check size={16} />{saving ? "正在保存…" : "保存偏好"}</button>
        <div className="companion-memory-note"><strong>我会记住什么</strong><p>当前阅读引用、这段答疑里的追问，以及你设置的解释偏好。</p>
          <p>答疑单独保存，默认不参与长期记忆学习。</p></div>
      </div> : <>
        {reference ? <div className="companion-reference"><MessageCircle size={15} /><div><span>正在解释这段内容</span><p>{reference.text}</p></div>
          <button type="button" aria-label="清除桌宠引用" onClick={() => changeReference(null)}><X size={16} /></button></div> : null}
        <div ref={feed} className="companion-feed" role="log" aria-label="桌宠答疑记录"
          onScroll={() => { if (feed.current) follow.current = feed.current.scrollHeight - feed.current.scrollTop - feed.current.clientHeight < 60; }}>
          {!history.length && !live ? <div className="companion-empty"><span className="companion-empty-mark"><MessageCircle size={25} /></span>
            <strong>哪里还没弄明白？</strong><p>选中一段文字，点“问桌宠”。<br />也可以直接在下面问我。</p></div> : null}
          {history.map((message) => <article key={message.id} className={`companion-message is-${message.role}`}>
            <span className="companion-speaker">{message.role === "user" ? "你" : "随问"}{message.partial ? " · 未完成" : ""}</span>
            {message.role === "assistant" ? <StaticMessageResponse>{message.text}</StaticMessageResponse> : <p>{message.text}</p>}
            {message.role === "assistant" ? <div className="companion-message-actions">
              <button type="button" aria-label="复制桌宠解释" onClick={() => void copy(message.text)}><Copy size={14} />复制</button>
              <button type="button" aria-label="将桌宠解释引用到主输入框" onClick={() => { onQuote(`> 桌宠解释\n\n${message.text}\n\n`); setNotice("已放入主聊天输入框，尚未发送。"); }}><ArrowDownLeft size={14} />引用到主聊天</button>
            </div> : null}
          </article>)}
          {floating ? <article className="companion-message is-assistant is-streaming"><span className="companion-speaker">随问</span><p>{floating}</p></article> : null}
          {busy ? <p className="companion-progress" role="status">{live?.detail || "正在解释…"}</p> : null}
          {live && live.status !== "running" && live.status !== "complete" ? <div className="companion-interrupted" role="status"><p>{live.detail}</p>
            <button type="button" onClick={() => { const q = lastRequest.current?.question ?? [...history].reverse().find((m) => m.role === "user")?.text; if (q) void submit({ request_id: createUuid(), parent, question: q, reference }); }}>重新解释</button></div> : null}
        </div>
        <form className="companion-composer" onSubmit={(event) => { event.preventDefault(); void submit(); }}>
          <textarea ref={input} aria-label="问桌宠" placeholder={reference ? "这段哪里没懂，随时问…" : "问个小问题…"}
            value={draft} maxLength={2000} rows={2} onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void submit(); } }} />
          <div><span><Grip size={12} />{reference ? "已带上原文" : "独立答疑"}</span>
            {busy ? <button type="button" aria-label="停止桌宠解释" onClick={() => void stop()}><Square size={15} /></button>
              : <button className="companion-primary" type="submit" disabled={!draft.trim()} aria-label="发送桌宠问题"><ArrowUp size={18} /></button>}</div>
        </form>
      </>}
      {error ? <div className="companion-error" role="alert"><span>{error}</span><button type="button" onClick={() => {
        if (lastRequest.current && live?.status === "running") void submit(lastRequest.current);
        else if (live?.status === "running") void watch(live.request_id);
        else void refresh().then((state) => { if (state?.active) void watch(state.active.request_id); });
      }}>重连</button></div> : null}
      {notice ? <p className="companion-notice" role="status">{notice}</p> : null}
    </section> : null}
  </div>, document.body);
});
