import { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import type { WebHostContextV1 } from "@akashic/web-ui-v1";
import { request, RequestError } from "../../shared/src/configuration";
import "./style.css";

type Status = {
  initialized: boolean; mode: "active" | "staging"; paused: boolean; auto_learn: boolean; control_version: number;
  learning: Record<string, number>; memories: Record<string, number>; kinds: Record<string, number>;
  current_source: string | null; failures: Record<string, string>; retry_pending: number;
  last_learned_at: string | null; last_scan_at: string | null;
  progress: {eligible: number; applied: number; pending: number; failed: number} | null;
};
type Action = "pause" | "resume" | "retry";
type Pending = {action: Action; request_id: string; expected_version: number};
type Result = Status & {control_receipt: {version: number; requested?: number; ineligible?: number}};
const endpoint = "/api/dashboard/default-memory";
const storageKey = "default-memory:pending-control";
const time = (value: string | null) => value ? new Date(value).toLocaleString("zh-CN", {hour12: false}) : "暂无记录";
const count = (value: number | undefined) => (value ?? 0).toLocaleString("zh-CN");
const failureLabel = (reason: string) => /validation error|json_invalid|Invalid JSON/i.test(reason)
  ? "模型返回的记忆未通过校验。" : reason.split("\n")[0];

function saved(): Pending | null {
  try {
    const raw = sessionStorage.getItem(storageKey);
    if (!raw) return null;
    const value = JSON.parse(raw) as Partial<Pending>;
    if (["pause", "resume", "retry"].includes(value.action ?? "") && typeof value.request_id === "string"
        && typeof value.expected_version === "number" && Number.isInteger(value.expected_version))
      return value as Pending;
  } catch { /* A damaged browser draft never authorizes a new control. */ }
  return null;
}

export function activate(ctx: WebHostContextV1) {
  return ctx.ui.inject("shell.settings.v1", mount => mount.register({
    id: "default-memory", label: "长期记忆", route: "default-memory", order: 35,
    iconSvg: '<svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M5 4h14v16H5zM8 8h8M8 12h8M8 16h5"/></svg>',
    render(host) {
      const root = createRoot(host);
      root.render(<MemoryStatus ctx={ctx} />);
      return () => root.unmount();
    },
  }));
}

function MemoryStatus({ctx}: {ctx: WebHostContextV1}) {
  const [data, setData] = useState<Status | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState<Pending | null>(saved);
  const [stale, setStale] = useState(true);
  const root = useRef<HTMLElement>(null);
  const alive = useRef(true), visible = useRef(false), writing = useRef(false);
  const sequence = useRef(0);
  const pendingRef = useRef(pending); pendingRef.current = pending;

  const load = async () => {
    if (writing.current) return;
    const serial = ++sequence.current;
    try {
      const value = await request<Status>(ctx, endpoint);
      if (!alive.current || serial !== sequence.current) return;
      setData(value); setStale(false); setError("");
    } catch (reason) {
      if (!alive.current || serial !== sequence.current) return;
      setStale(true); setError(reason instanceof Error ? reason.message : String(reason));
    }
  };
  useEffect(() => {
    alive.current = true;
    void load();
    const observer = new IntersectionObserver(entries => {
      visible.current = entries[0].isIntersecting;
      if (visible.current) void load();
    });
    observer.observe(root.current!);
    const refresh = () => { if (visible.current && document.visibilityState === "visible") void load(); };
    const timer = window.setInterval(refresh, 5000);
    document.addEventListener("visibilitychange", refresh);
    window.addEventListener("focus", refresh);
    return () => {
      alive.current = false; sequence.current += 1; observer.disconnect(); window.clearInterval(timer);
      document.removeEventListener("visibilitychange", refresh); window.removeEventListener("focus", refresh);
    };
  }, [ctx]);

  const submit = async (operation: Pending) => {
    if (writing.current) return;
    writing.current = true; sequence.current += 1;
    sessionStorage.setItem(storageKey, JSON.stringify(operation));
    pendingRef.current = operation; setPending(operation); setBusy(true); setError(""); setNotice("");
    try {
      const value = await request<Result>(ctx, endpoint + "/control", {
        method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(operation),
      });
      sessionStorage.removeItem(storageKey); pendingRef.current = null;
      if (!alive.current) return;
      setPending(null); setData(value); setStale(false);
      setNotice(operation.action === "retry"
        ? `已受理 ${value.control_receipt.requested ?? 0} 个失败轮次的重试。${value.control_receipt.ineligible ? `另有 ${value.control_receipt.ineligible} 项不符合当前学习条件。` : ""}`
        : value.paused ? "暂停已保存，已开始的轮次完成后停止；已有记忆仍可召回。" : "恢复已保存，有待处理轮次时将继续学习。");
    } catch (reason) {
      const rejected = reason instanceof RequestError && [400, 401, 403, 409, 422].includes(reason.status);
      if (rejected) { sessionStorage.removeItem(storageKey); pendingRef.current = null; }
      if (!alive.current) return;
      if (rejected) { setPending(null); setNotice("操作未执行：" + (reason instanceof Error ? reason.message : String(reason))); }
      setStale(true);
      setError(reason instanceof Error ? reason.message : String(reason));
      if (!rejected) setNotice("操作结果尚未确认。确认结果会重发同一请求，不重复创建操作。");
    } finally {
      writing.current = false;
      if (alive.current) { setBusy(false); void load(); }
    }
  };
  const act = (action: Action) => {
    if (!data?.initialized || pendingRef.current || stale) return;
    void submit({action, request_id: crypto.randomUUID(), expected_version: data.control_version});
  };
  const failures = Object.entries(data?.failures ?? {});
  const state = data?.paused ? data.current_source ? "正在暂停" : "已暂停"
    : data?.current_source ? "正在学习" : data?.auto_learn ? "自动学习已开启" : "手动学习模式";

  return <main className="memory-status" ref={root}>
    <div className="memory-status-shell">
      <header><p>Default Memory</p><h1>长期记忆</h1><p>查看学习进度与运行状态，管理暂停、恢复和失败重试。</p></header>
      <div className="memory-actions">
        <button type="button" disabled={!data?.initialized || stale || busy || !!pending} onClick={() => act(data?.paused ? "resume" : "pause")}>{data?.paused ? "恢复学习" : "暂停学习"}</button>
        <button type="button" disabled={!data?.initialized || stale || busy || !!pending || data.paused || failures.length === 0} onClick={() => act("retry")}>重试失败项{failures.length > 0 ? `（${failures.length}）` : ""}</button>
        <button type="button" disabled={busy} onClick={() => void load()}>刷新状态</button>
      </div>
      {error && <p className="memory-error" role="alert">{error}{data && "；下方保留上次读取的状态。"}</p>}
      {pending && <p className="memory-notice" role="status">有一项操作待确认。<button type="button" disabled={busy} onClick={() => void submit(pending)}>确认操作结果</button></p>}
      {notice && <p className="memory-notice" role="status">{notice}</p>}
      {!data?.initialized ? <p className="memory-empty" role="status">{data ? "记忆库尚未初始化，暂时不能操作学习状态。" : "正在读取记忆状态…"}</p> : <>
        <section aria-label="学习状态">
          <h2>{state}</h2>
          <p>{data.mode === "active" ? "已接入对话召回" : "准备模式，尚未接入对话召回"} · {data.auto_learn ? "符合条件的新对话会自动学习" : "自动学习未开启，显式重试仍可处理失败轮次"}</p>
          {data.paused && <p>暂停不会删除记忆，也不会停止已有记忆的召回。重试失败项前请先恢复学习。</p>}
          {data.current_source && <details><summary>正在处理一个轮次</summary><code>{data.current_source}</code></details>}
          {data.retry_pending > 0 && <p>已有 {count(data.retry_pending)} 个轮次等待重试。</p>}
        </section>
        <dl className="memory-metrics">
          <div><dt>有效记忆</dt><dd>{count(data.memories.active)}</dd></div>
          <div><dt>累计完成轮次</dt><dd>{count(data.learning.applied)}</dd></div>
          <div><dt>待处理轮次</dt><dd>{data.progress ? count(data.progress.pending) : "待扫描"}</dd></div>
          <div><dt>失败轮次</dt><dd>{count(failures.length)}</dd></div>
        </dl>
        <section><h2>学习进度</h2>
          {data.progress ? <p>最近一次扫描发现 {count(data.progress.eligible)} 个符合条件的完整轮次，其中已完成 {count(data.progress.applied)} 个，待处理 {count(data.progress.pending)} 个，失败 {count(data.progress.failed)} 个。</p>
            : <p>尚未扫描历史，待处理数量未知。页面刷新只读取状态，不触发历史补学。</p>}
          <dl className="memory-details">
            <div><dt>最近扫描</dt><dd>{time(data.last_scan_at)}</dd></div>
            <div><dt>最近完成学习</dt><dd>{time(data.last_learned_at)}{!data.last_learned_at && "（旧轮次未记录完成时间）"}</dd></div>
            <div><dt>记忆分类</dt><dd>偏好 {count(data.kinds.preference)} · 资料 {count(data.kinds.profile)} · 流程 {count(data.kinds.procedure)} · 事件 {count(data.kinds.event)}</dd></div>
            <div><dt>已失效记忆</dt><dd>{count(data.memories.superseded)} 条，保留记录</dd></div>
          </dl>
        </section>
        <section><h2>失败记录</h2>
          {failures.length === 0 ? <p>当前没有待重试的失败项。</p> : <ul className="memory-failures">
            {failures.map(([key, reason], i) => <li key={key}><p>{failureLabel(reason)}</p><details><summary>查看失败详情 {i + 1}</summary><p>{reason}</p><code>{key}</code></details></li>)}
          </ul>}
        </section>
        <aside className="memory-notes"><p>原始对话由聊天记录保留。当前页面只管理学习状态；失效记忆不会在这里物理删除。</p><p>Akasha 仍提供项目记忆策略和兴趣评分，Default Memory 负责结构化记忆学习与召回。</p></aside>
      </>}
    </div>
  </main>;
}
