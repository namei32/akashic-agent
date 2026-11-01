import { useEffect, useRef, useState } from "react";
import type { WebHostContextV1 } from "@akashic/web-ui-v1";
import { Confirm, request, RequestError } from "../../shared/src/configuration";
import "./initiative.css";

type Target = { channel: string; recipient: string; session_id: string };
type Producer = { auto_propose: boolean; memory_assisted: boolean; interval_seconds: number; start_hour: number; end_hour: number };
type Snapshot = {
  control: { paused: boolean; control_version: number };
  consent: { enabled: boolean; target: Target | null; consent_version: number };
  config: { input_ref: string; producer: Producer };
  wake: { enabled: boolean | null; ready: boolean; reason: string; values: { delivery: Target | null; timezone: string };
    targets?: (Target & { label: string })[] };
  memory_ready: boolean;
  latest: { outcome: string; fired_at: string; memory_status: string | null; delivery_status: string | null; reason: string | null } | null;
};
type Group = "grant" | "revoke" | "pause" | "resume" | "schedule";
type Pending = { action: "memory-consent" | "pause" | "resume" | "configure"; body: Record<string, unknown> };
type PendingMap = Partial<Record<Group, Pending>>;
const endpoint = "/api/dashboard/wake/initiative";
const groups: Group[] = ["grant", "revoke", "pause", "resume", "schedule"];
const storage = (group: Group) => `wake-initiative:pending:${group}`;
const sameTarget = (a: Target | null, b: Target | null) => !!a && !!b && a.channel === b.channel
  && a.recipient === b.recipient && a.session_id === b.session_id;
const stamp = (value: string) => new Date(value).toLocaleString("zh-CN", {hour12: false});
const outcomes: Record<string, string> = {shared: "已送达", model_skip: "这次没有联系", deferred: "未继续本次联系",
  failed: "处理失败", admission_rejected: "未满足启动条件", checking: "正在检查"};
const deliveries: Record<string, string> = {not_published: "未发布", not_prepared: "已发布，尚未准备送达",
  prepared: "已发布，等待送达", started: "已发布，发送已开始", delivered: "已送达", rejected: "已发布，发送被拒绝",
  failed: "已发布，发送失败", unavailable: "送达状态暂不可用"};
const reasons: Record<string, string> = {no_user_input: "目标对话中没有可用于判断的用户文本。",
  unscoped_context: "有事件材料的适用范围无法确认，本次没有交给模型。",
  source_unavailable: "目标对话不可用。", scoped_history_unavailable: "同会话的送达历史暂不可用。",
  invalidated: "机会、授权或使用的记忆已变化。"};

function saved(): PendingMap {
  const values: PendingMap = {};
  for (const group of groups) {
    try {
      const raw = sessionStorage.getItem(storage(group));
      if (!raw) continue;
      const value = JSON.parse(raw) as Pending;
      const action = group === "grant" || group === "revoke" ? "memory-consent" : group === "schedule" ? "configure" : group;
      if (value && value.action === action && typeof value.body === "object" && value.body !== null
          && typeof value.body.request_id === "string") values[group] = value;
    } catch { /* A damaged browser draft never starts a new operation. */ }
  }
  return values;
}

export function InitiativePage({ctx}: {ctx: WebHostContextV1}) {
  const [data, setData] = useState<Snapshot | null>(null);
  const [draft, setDraft] = useState<Producer | null>(null);
  const [dirty, setDirty] = useState(false);
  const [stale, setStale] = useState(true);
  const [pending, setPending] = useState<PendingMap>(saved);
  const [busy, setBusy] = useState<Partial<Record<Group, boolean>>>({});
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [leave, setLeave] = useState<(() => void) | null>(null);
  const alive = useRef(true), visible = useRef(false), serial = useRef(0), root = useRef<HTMLElement>(null);
  const dirtyRef = useRef(false), pendingRef = useRef(pending), busyRef = useRef<Partial<Record<Group, boolean>>>({});
  pendingRef.current = pending;

  const load = async () => {
    const current = ++serial.current;
    try {
      const next = await request<Snapshot>(ctx, endpoint);
      if (!alive.current || current !== serial.current) return;
      setData(next); setStale(false); setError("");
      if (!dirtyRef.current && !pendingRef.current.schedule) setDraft(next.config.producer);
    } catch (reason) {
      if (!alive.current || current !== serial.current) return;
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
    window.addEventListener("focus", refresh);
    document.addEventListener("visibilitychange", refresh);
    return () => { alive.current = false; serial.current += 1; observer.disconnect(); window.clearInterval(timer);
      window.removeEventListener("focus", refresh); document.removeEventListener("visibilitychange", refresh); };
  }, [ctx]);
  useEffect(() => {
    if (!dirty) return;
    const unload = (event: BeforeUnloadEvent) => event.preventDefault();
    const navigate = (event: Event) => {
      const {go, reason} = (event as CustomEvent<{go: () => void; reason?: string}>).detail;
      if (reason === "catalog" && pendingRef.current.schedule) return;
      event.preventDefault();
      if (reason === "catalog") return;
      setLeave(() => go);
    };
    window.addEventListener("beforeunload", unload);
    window.addEventListener("akashic:before-navigate", navigate);
    return () => { window.removeEventListener("beforeunload", unload); window.removeEventListener("akashic:before-navigate", navigate); };
  }, [dirty]);

  const updatePending = (group: Group, value: Pending | null) => {
    if (value) sessionStorage.setItem(storage(group), JSON.stringify(value));
    else sessionStorage.removeItem(storage(group));
    const next = {...pendingRef.current};
    if (value) next[group] = value; else delete next[group];
    pendingRef.current = next;
    if (alive.current) setPending(next);
  };
  const submit = async (group: Group, operation: Pending) => {
    if (busyRef.current[group]) return;
    try { updatePending(group, operation); }
    catch { setError("浏览器无法保存操作标识，暂不能安全提交。"); return; }
    busyRef.current[group] = true; setBusy({...busyRef.current}); setError(""); setNotice("");
    try {
      const result = await request<{request_id?: string; state?: string; error?: string}>(ctx, `${endpoint}/${operation.action}`, {
        method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(operation.body),
      });
      if (operation.action === "configure") {
        if (result.request_id !== operation.body.request_id) throw new Error("配置回执身份不一致");
        let state = result.state;
        let detail = result.error;
        for (let attempt = 0; alive.current && !["active", "failed", "superseded"].includes(state ?? "") && attempt < 30; attempt += 1) {
          await new Promise(resolve => window.setTimeout(resolve, 500));
          const receipt = await request<{state: string; error?: string}>(ctx,
            `${endpoint}/config-receipts/${encodeURIComponent(String(operation.body.request_id))}`);
          state = receipt.state; detail = receipt.error;
        }
        if (!alive.current) return;
        if (state === "failed") { updatePending(group, null); throw new RequestError(detail || "配置生效失败", 409); }
        if (state !== "active" && state !== "superseded") throw new Error("配置结果仍在确认中");
        dirtyRef.current = false; setDirty(false);
      }
      updatePending(group, null);
      if (alive.current) setNotice(group === "grant" ? "当前目标的记忆使用授权已保存。"
        : group === "revoke" ? "记忆使用授权已撤销；尚未开始的主动效果会重新核验。"
        : group === "pause" ? "已暂停新的主动联系。" : group === "resume" ? "已恢复主动联系。" : "定时设置已生效。");
    } catch (reason) {
      const rejected = reason instanceof RequestError && [400, 401, 403, 404, 409, 422].includes(reason.status);
      if (rejected) updatePending(group, null);
      if (alive.current) {
        setError(reason instanceof Error ? reason.message : String(reason));
        if (!rejected) setNotice("操作结果尚未确认。可以用原请求再次确认，不会重复创建操作。");
      }
    } finally {
      delete busyRef.current[group];
      if (alive.current) { setBusy({...busyRef.current}); void load(); }
    }
  };
  const create = (group: Group, action: Pending["action"], body: Record<string, unknown>) => {
    if (stale || pendingRef.current[group] || busyRef.current[group]) return;
    void submit(group, {action, body: {...body, request_id: crypto.randomUUID()}});
  };
  const change = (key: keyof Producer, value: boolean | number) => {
    dirtyRef.current = true; setDirty(true); setDraft(previous => previous && {...previous, [key]: value});
  };
  const target = data?.wake.values.delivery ?? null;
  const authorized = !!data?.consent.enabled && sameTarget(data.consent.target, target);
  const targetName = data?.wake.targets?.find(item => sameTarget(item, target))?.label ?? target?.session_id ?? "尚未选择目标";
  const validDraft = !!draft && Number.isInteger(draft.interval_seconds) && draft.interval_seconds >= 60
    && draft.interval_seconds <= 604800 && Number.isInteger(draft.start_hour) && Number.isInteger(draft.end_hour)
    && draft.start_hour >= 0 && draft.start_hour < draft.end_hour && draft.end_hour <= 24;
  const needsConsent = !!draft?.auto_propose && draft.memory_assisted && !authorized;
  const latest = data?.latest;

  return <main className="initiative-page" ref={root}>
    <header><p className="initiative-eyebrow">主动联系</p><h1>让 Akashic 适时先开口</h1>
      <p>它会根据当前对话和你的设置判断是否联系；定时机会不代表一定发送。</p></header>
    {error && <p className="initiative-error" role="alert">{error}</p>}
    {notice && <p className="initiative-notice" role="status">{notice}</p>}
    {!data ? <p role="status">{error ? "主动联系状态暂不可用。" : "正在读取主动联系状态…"}</p> : <>
      <section aria-labelledby="initiative-target"><div className="initiative-section-heading"><div><h2 id="initiative-target">联系目标与记忆授权</h2>
        <p>当前目标：<strong>{targetName}</strong></p></div><span className="initiative-state">{authorized ? "已授权" : "未授权"}</span></div>
        <p>授权后，可将这个对话的近期内容和所属范围内召回的记忆交给模型，判断是否主动联系；材料可能保存在内部执行记录中，消息会发往上面的目标。</p>
        {data.consent.enabled && !authorized && <p className="initiative-hint">已有授权属于其他目标。更换目标不会自动转移授权。</p>}
        {data.wake.enabled !== true && <p className="initiative-hint">请先在功能设置中启用 Wake 并保存接收目标。</p>}
        {data.wake.enabled === true && !data.wake.ready && data.wake.reason && <p className="initiative-hint">{data.wake.reason}</p>}
        {!data.memory_ready && <p className="initiative-hint">记忆联动所需的记忆、范围保护或私人渠道能力尚未全部就绪。</p>}
        <div className="initiative-actions">
          {!authorized && <button type="button" disabled={stale || !target || !data.wake.ready || !!pending.grant || !!busy.grant}
            onClick={() => create("grant", "memory-consent", {enabled: true, target, expected_version: data.consent.consent_version})}>授权当前目标</button>}
          {data.consent.enabled && <button type="button" disabled={stale || !!pending.revoke || !!busy.revoke}
            onClick={() => create("revoke", "memory-consent", {enabled: false, expected_version: data.consent.consent_version})}>撤销记忆授权</button>}
        </div>
      </section>
      <section aria-labelledby="initiative-schedule"><div className="initiative-section-heading"><div><h2 id="initiative-schedule">定时机会</h2>
        <p>按目标对话的时区 {data.wake.values.timezone || "系统时区"} 计算。停机期间错过的机会不会补发。</p></div>
        <span className="initiative-state">{data.control.paused ? "已暂停" : data.config.producer.auto_propose ? "已开启" : "未开启"}</span></div>
        {draft && <div className="initiative-schedule-fields">
          <label className="initiative-check"><input type="checkbox" checked={draft.auto_propose} onChange={event => change("auto_propose", event.target.checked)} />开启定时机会</label>
          <label className="initiative-check"><input type="checkbox" checked={draft.memory_assisted} onChange={event => change("memory_assisted", event.target.checked)} />使用已授权的长期记忆</label>
          <div className="initiative-grid"><label>最短间隔（分钟）<input type="number" min="1" max="10080" step="any" value={Number.isFinite(draft.interval_seconds) ? draft.interval_seconds / 60 : ""}
            onChange={event => change("interval_seconds", Math.round(event.target.valueAsNumber * 60))} /></label>
            <label>开始时（0–23）<input type="number" min="0" max="23" step="1" value={Number.isFinite(draft.start_hour) ? draft.start_hour : ""} onChange={event => change("start_hour", event.target.valueAsNumber)} /></label>
            <label>结束时（1–24）<input type="number" min="1" max="24" step="1" value={Number.isFinite(draft.end_hour) ? draft.end_hour : ""} onChange={event => change("end_hour", event.target.valueAsNumber)} /></label></div>
          {needsConsent && <p className="initiative-hint">先授权当前目标，才能开启使用记忆的定时机会。</p>}
          {!validDraft && <p className="initiative-hint">请填写有效的间隔和起止时间。</p>}
          <div className="initiative-actions"><button type="button" disabled={stale || !dirty || !validDraft || needsConsent || !!pending.schedule || !!busy.schedule}
            onClick={() => create("schedule", "configure", {...draft, expected_input: data.config.input_ref})}>保存定时设置</button>
            <button type="button" disabled={stale || !!pending[data.control.paused ? "resume" : "pause"] || !!busy[data.control.paused ? "resume" : "pause"]}
              onClick={() => create(data.control.paused ? "resume" : "pause", data.control.paused ? "resume" : "pause", {expected_version: data.control.control_version})}>
              {data.control.paused ? "恢复主动联系" : "暂停主动联系"}</button></div>
        </div>}
      </section>
      <section aria-labelledby="initiative-latest"><h2 id="initiative-latest">最近一次主动检查</h2>
        {!latest ? <p>还没有主动联系记录。</p> : <><p><strong>{outcomes[latest.outcome] ?? "状态待确认"}</strong> · {stamp(latest.fired_at)}</p>
          {latest.reason && reasons[latest.reason] && <p>{reasons[latest.reason]}</p>}
          {latest.memory_status === "degraded" && <p>向量检索暂不可用，本次使用关键词召回。</p>}
          {latest.memory_status === "empty" && <p>本次没有召回长期记忆。</p>}
          {latest.delivery_status && deliveries[latest.delivery_status] && <p>{deliveries[latest.delivery_status]}</p>}</>}
        <p className="initiative-footnote">撤销授权不会删除已有对话或已经发布的消息；已开始的发送按实际回执结算。</p>
      </section>
    </>}
    {groups.filter(group => pending[group]).map(group => <p className="initiative-notice" role="status" key={group}>
      有一项{group === "schedule" ? "定时设置" : group === "grant" || group === "revoke" ? "授权" : "暂停控制"}操作待确认。
      <button type="button" disabled={!!busy[group]} onClick={() => { const operation = pending[group]; if (operation) void submit(group, operation); }}>重试并确认</button>
    </p>)}
    <button type="button" onClick={() => void load()}>刷新状态</button>
    {leave && <Confirm title="未保存的修改将丢失" accept={() => { dirtyRef.current = false; setDirty(false); const go = leave; setLeave(null); go(); }}
      cancel={() => setLeave(null)}>当前定时设置尚未保存，离开后将恢复为原有设置。</Confirm>}
  </main>;
}
