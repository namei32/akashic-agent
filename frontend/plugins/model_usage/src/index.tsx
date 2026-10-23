import { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import type { WebHostContextV1 } from "@akashic/web-ui-v1";
import { Calls } from "./calls";
import { Overview } from "./overview";
import { Trend } from "./trend";
import { date, number, type Report } from "./view";
import "./style.css";

type Selection = {hours: number; model: string; end: number; cursors: (string | null)[]};
type Snapshot = {selection: Selection; report: Report};

export function activate(ctx: WebHostContextV1) {
  return ctx.ui.inject("shell.settings.v1", mount => mount.register({
    id: "model-usage", label: "模型用量", route: "model-usage", order: 32,
    iconSvg: '<svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M4 3v17h17M8 15v-4m5 4V7m5 8V4"/></svg>',
    render(host) {
      const root = createRoot(host);
      root.render(<Usage ctx={ctx} />);
      return () => root.unmount();
    },
  }));
}

function Usage({ctx}: {ctx: WebHostContextV1}) {
  const [selection, setSelection] = useState<Selection>(() => ({hours: 24, model: "", end: Date.now(), cursors: [null]}));
  const [loaded, setLoaded] = useState<Snapshot | null>(null);
  const [request, setRequest] = useState({key: "", loading: true, error: ""});
  const key = JSON.stringify(selection);
  const busy = request.key !== key || request.loading;
  const error = request.key === key ? request.error : "";
  const report = loaded?.report;
  const models = report?.models ?? [];
  useEffect(() => {
    const controller = new AbortController();
    setRequest({key, loading: true, error: ""});
    const params = new URLSearchParams({start: new Date(selection.end - selection.hours * 3600000).toISOString(), end: new Date(selection.end).toISOString()});
    if (selection.model) params.set("model", selection.model);
    const cursor = selection.cursors.at(-1);
    if (cursor) params.set("cursor", cursor);
    async function load() {
      const response = await ctx.http.request(`/api/dashboard/model-usage?${params}`, {signal: controller.signal});
      if (!response.ok) {
        const body = await response.json().catch(() => null) as {detail?: unknown} | null;
        throw new Error(typeof body?.detail === "string" ? body.detail : `请求失败（${response.status}）`);
      }
      const body = await response.json() as Report;
      if (!controller.signal.aborted) {
        setLoaded({selection, report: body});
        setRequest({key, loading: false, error: ""});
      }
    }
    void load().catch(reason => {
      if (!controller.signal.aborted) setRequest({key, loading: false, error: reason instanceof Error ? reason.message : String(reason)});
    });
    return () => controller.abort();
  }, [ctx, key, selection]);
  const refresh = () => { if (!busy) setSelection(value => ({...value, end: Date.now(), cursors: [null]})); };
  function page(direction: "previous" | "next") {
    if (busy || !loaded) return;
    const previous = loaded.selection;
    if (direction === "previous" && previous.cursors.length > 1) setSelection({...previous, cursors: previous.cursors.slice(0, -1)});
    if (direction === "next" && loaded.report.next_cursor) setSelection({...previous, cursors: [...previous.cursors, loaded.report.next_cursor]});
  }
  return <main className="model-usage" aria-label="模型用量统计">
    <div className="usage-filters">
      <div className="usage-periods" role="group" aria-label="时间范围">{[[24, "24 小时"], [168, "7 天"], [720, "30 天"]].map(([hours, label]) =>
        <button type="button" key={hours} aria-pressed={selection.hours === hours}
          onClick={() => { if (selection.hours !== hours) setSelection({...selection, hours: Number(hours), end: Date.now(), cursors: [null]}); }}>{label}</button>)}</div>
      <select aria-label="模型" value={selection.model} onChange={event => setSelection({...selection, model: event.target.value, cursors: [null]})}>
        <option value="">全部模型</option>
        {selection.model && !models.some(item => item.model === selection.model) && <option>{selection.model}</option>}
        {models.filter(item => item.model).map(item => <option key={item.model} value={item.model}>{item.model}</option>)}
      </select>
      <button type="button" aria-disabled={busy} onClick={refresh}>刷新</button>
    </div>
    <div className="usage-read-status" role="status">
      <span>{loaded ? `${date(loaded.report.start)} — ${date(loaded.report.end)}` : "\u00a0"}</span>
      <span>{busy ? loaded ? "正在更新，暂显示上次结果…" : "正在读取调用记录…"
        : error ? loaded ? "已保留上次结果" : "\u00a0" : "按调用开始时间 · 设备本地时区"}</span>
    </div>
    {error && <p className="usage-error" role="alert">{error}{loaded && "。以下保留上次读取结果。"}
      <button type="button" onClick={() => setSelection(value => ({...value}))}>重试</button></p>}
    {loaded && report ? <div className="usage-results" aria-busy={busy}>
      <Overview totals={report.summary} />
      <section className="usage-chart-section"><Trend report={report} /></section>
      <section className="usage-calls-section"><div className="usage-section-heading"><h2>调用明细</h2><span>{number(report.summary.calls)} 次尝试</span></div>
        <Calls calls={report.calls} pageKey={JSON.stringify(loaded.selection)} />
        <nav className="usage-pagination" aria-label="调用分页">
          <button type="button" aria-disabled={busy || loaded.selection.cursors.length === 1} onClick={() => page("previous")}>上一页</button>
          <span>第 {loaded.selection.cursors.length} 页 · 最多 50 条</span>
          <button type="button" aria-disabled={busy || !report.next_cursor} onClick={() => page("next")}>下一页</button>
        </nav>
      </section>
    </div> : !error && <div className="usage-skeleton" aria-hidden="true"><div /><div /><div /><div /></div>}
    <details className="usage-notes"><summary>统计口径</summary>
      <p>覆盖当前实例已入账的聊天模型调用，包含重试；向量模型调用尚未纳入。未知字段显示为「—」，汇总只累计已知数值。点击缩写数字可看完整值。</p>
      <p>端到端吞吐 = 成功调用的已知输出 token 总量 ÷ 对应调用总耗时，包含首段前的等待。首段可能是文字或思考；缓存与推理是分项，不再累加到输入、输出总数。</p>
      <p>按调用开始时间统计；时间显示为设备本地时区，日趋势按 UTC 分桶。</p>
    </details>
  </main>;
}
