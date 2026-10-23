import { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import type { WebHostContextV1 } from "@akashic/web-ui-v1";
import "./style.css";

const tokenFields = ["input_tokens", "output_tokens", "cached_input_tokens", "cache_write_input_tokens", "reasoning_output_tokens"] as const;
type TokenField = typeof tokenFields[number];
type Tokens = Record<TokenField, number | null>;
type Totals = Tokens & Record<`${TokenField}_known_calls`, number> & {calls: number};
type Call = Tokens & {id: string; model: string; started_at: string; state: "started" | "success" | "error";
  attempt: number | null; coverage: "exact" | "partial" | "unavailable" | null; first_token_ms: number | null; duration_ms: number | null};
type Report = {
  start: string; end: string; bucket_seconds: number; next_cursor: string | null;
  models: {model: string; calls: number}[]; trend: (Totals & {bucket: string})[]; calls: Call[];
  summary: Totals & {success: number; errors: number; unsettled: number; retries: number; attempt_known_calls: number; exact_calls: number;
    first_token_ms: number | null; first_token_known_calls: number; duration_ms: number | null;
    tokens_per_second: number | null; cache_hit_rate: number | null; cache_known_calls: number};
};
type Selection = {hours: number; model: string; end: number; cursors: (string | null)[]};
const labels: Record<TokenField, string> = {input_tokens: "输入", output_tokens: "输出", cached_input_tokens: "缓存读取",
  cache_write_input_tokens: "缓存写入", reasoning_output_tokens: "推理"};
const number = (value: number | null, digits = 0) => value === null ? "—" : value.toLocaleString("zh-CN", {maximumFractionDigits: digits});
const seconds = (value: number | null) => value === null ? "—" : `${number(value / 1000, 2)} s`;
const date = (value: string) => new Date(value).toLocaleString("zh-CN", {month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false});
const rate = (value: number | null) => value === null ? "—" : `${number(value * 100, 1)}%`;

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
  const [loaded, setLoaded] = useState<{key: string; report: Report} | null>(null);
  const [models, setModels] = useState<Report["models"]>([]);
  const [error, setError] = useState("");
  const key = JSON.stringify(selection);
  const report = loaded?.key === key ? loaded.report : null;
  useEffect(() => {
    const controller = new AbortController();
    setError("");
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
      if (!controller.signal.aborted) { setLoaded({key, report: body}); setModels(body.models); }
    }
    void load().catch(reason => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : String(reason)); });
    return () => controller.abort();
  }, [ctx, key, selection]);
  const refresh = () => setSelection(value => ({...value, end: Date.now(), cursors: [null]}));
  return <main className="model-usage">
    <div className="usage-shell">
      <header><p className="usage-eyebrow">调用统计</p><h1>模型用量</h1>
        <p>查看 token 消耗与响应速度，了解哪些数据已经统计、哪些仍然未知。</p></header>
      <div className="usage-filters">
        <label>时间范围<select value={selection.hours} onChange={event => setSelection({...selection, hours: Number(event.target.value), end: Date.now(), cursors: [null]})}>
          <option value={24}>过去 24 小时</option><option value={168}>过去 7 天</option><option value={720}>过去 30 天</option>
        </select></label>
        <label>模型<select value={selection.model} onChange={event => setSelection({...selection, model: event.target.value, cursors: [null]})}>
          <option value="">全部模型</option>
          {selection.model && !models.some(item => item.model === selection.model) && <option>{selection.model}</option>}
          {models.filter(item => item.model).map(item => <option key={item.model} value={item.model}>{item.model}</option>)}
        </select></label>
        <button type="button" onClick={refresh}>刷新</button>
      </div>
      {error ? <p className="usage-error" role="alert">{error} <button type="button" onClick={refresh}>重试</button></p>
        : !report ? <p role="status" className="usage-empty">正在读取调用记录…</p> : <>
          <p className="usage-caption">{date(report.start)} — {date(report.end)} · 按调用开始时间统计 · 时间使用当前设备时区</p>
          <Overview totals={report.summary} />
          {report.summary.calls === 0 ? <p className="usage-empty">这段时间还没有调用记录。可以换个时间范围，或在模型调用后刷新。</p> : <>
            <section><div className="usage-section-heading"><h2>用量走势</h2><span>已知输入 / 输出 · {report.bucket_seconds === 3600 ? "每小时" : "每天"}</span></div>
              <Trend report={report} /></section>
            <section><div className="usage-section-heading"><h2>调用明细</h2><span>共 {number(report.summary.calls)} 次尝试</span></div>
              <div className="usage-table-scroll" tabIndex={0} role="region" aria-label="调用明细，可横向滚动">
                <table><thead><tr><th>开始时间 / 模型</th><th>结果</th><th>输入</th><th>输出</th><th>总耗时</th><th>更多</th></tr></thead>
                  <tbody>{report.calls.map(call => <CallRow key={call.id} call={call} />)}</tbody></table>
              </div>
              <nav className="usage-pagination" aria-label="调用分页">
                <button type="button" disabled={selection.cursors.length === 1} onClick={() => setSelection({...selection, cursors: selection.cursors.slice(0, -1)})}>上一页</button>
                <span>第 {selection.cursors.length} 页 · 每页最多 50 条</span>
                <button type="button" disabled={!report.next_cursor} onClick={() => setSelection({...selection, cursors: [...selection.cursors, report.next_cursor]})}>下一页</button>
              </nav>
            </section>
          </>}
        </>}
      <footer className="usage-notes"><strong>统计口径</strong>
        <p>覆盖当前实例已入账的聊天模型调用，包含重试；向量模型调用尚未纳入。未知字段显示为「—」，汇总只累计已知数值。</p>
        <p>端到端吞吐 = 成功调用的已知输出 token 总量 ÷ 对应调用总耗时，包含首段前的等待，属于客户端观察值。首段可能是文字或思考；缓存与推理是分项，不再累加到输入、输出总数。</p>
      </footer>
    </div>
  </main>;
}

function Overview({totals}: {totals: Report["summary"]}) {
  const fields: TokenField[] = ["input_tokens", "output_tokens", "cached_input_tokens", "cache_write_input_tokens", "reasoning_output_tokens"];
  return <>
    <div className="usage-summary" aria-label="调用概况"><strong>{number(totals.calls)} 次调用</strong>
      <span>{number(totals.success)} 成功 · {number(totals.errors)} 失败 · {number(totals.unsettled)} 未结算 · 已知重试 {number(totals.retries)} 次
        {totals.attempt_known_calls < totals.calls && ` · ${number(totals.calls - totals.attempt_known_calls)} 次缺少尝试序号`}</span>
    </div>
    <dl className="usage-metrics">{fields.map(field => <div key={field}>
      <dt>{labels[field]} token{totals[`${field}_known_calls`] < totals.calls ? "（已知）" : ""}</dt>
      <dd>{number(totals[field])}</dd><small>{totals[`${field}_known_calls`]} / {totals.calls} 次调用有数据</small>
    </div>)}</dl>
    <dl className="usage-performance">
      <div><dt>平均首段响应</dt><dd>{seconds(totals.first_token_ms)}</dd><small>{totals.first_token_known_calls} 次有计时</small></div>
      <div><dt>平均总耗时</dt><dd>{seconds(totals.duration_ms)}</dd></div>
      <div><dt>端到端吞吐</dt><dd>{number(totals.tokens_per_second, 1)} <small>tok/s</small></dd></div>
      <div><dt>缓存命中率</dt><dd>{rate(totals.cache_hit_rate)}</dd><small>{totals.cache_known_calls} 次有输入与缓存数据</small></div>
    </dl>
  </>;
}

function CallRow({call}: {call: Call}) {
  const [expanded, setExpanded] = useState(false);
  const coverage = call.coverage === "exact" ? "输入 / 输出已统计" : call.coverage === "partial" ? "部分用量未知" : "用量未知";
  const speed = call.state === "success" && call.duration_ms !== null && call.duration_ms > 0 && call.output_tokens !== null
    ? call.output_tokens * 1000 / call.duration_ms : null;
  const detail = [["调用 ID", call.id], ["尝试序号", number(call.attempt === null ? null : call.attempt + 1)],
    ["首段响应", seconds(call.first_token_ms)], ["端到端吞吐", `${number(speed, 1)} tok/s`],
    ...tokenFields.slice(2).map(field => [labels[field], number(call[field])])];
  return <><tr><td><time dateTime={call.started_at}>{date(call.started_at)}</time><strong className="usage-model">{call.model || "未知模型"}</strong></td>
    <td><span className={call.state === "error" ? "usage-error" : ""}>{({started: "未结算", success: "成功", error: "失败"})[call.state]}</span><small>{coverage}</small></td>
    <td>{number(call.input_tokens)}</td><td>{number(call.output_tokens)}</td><td>{seconds(call.duration_ms)}</td>
    <td><button type="button" className="usage-expand" aria-expanded={expanded} aria-controls={`usage-call-${call.id}`}
      onClick={() => setExpanded(!expanded)}>{expanded ? "收起" : "展开"}</button></td></tr>
    {expanded && <tr><td colSpan={6}><dl id={`usage-call-${call.id}`} className="usage-call-detail">
      {detail.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}
    </dl></td></tr>}</>;
}

function Trend({report}: {report: Report}) {
  const step = report.bucket_seconds * 1000;
  const buckets: {stamp: number; input: number | null; output: number | null; calls: number}[] = [];
  const known = new Map(report.trend.map(item => [Date.parse(item.bucket), item]));
  for (let stamp = Math.floor(Date.parse(report.start) / step) * step; stamp < Date.parse(report.end); stamp += step) {
    const value = known.get(stamp);
    buckets.push({stamp, input: value ? value.input_tokens : 0, output: value ? value.output_tokens : 0, calls: value?.calls ?? 0});
  }
  const max = Math.max(1, ...buckets.flatMap(item => [item.input ?? 0, item.output ?? 0]));
  const width = 960 / buckets.length;
  return <figure className="usage-trend"><svg viewBox="0 0 960 140" preserveAspectRatio="none" role="img" aria-label="已知输入与输出 token 用量走势；未知数值不绘制柱高">
    <line x1="0" x2="960" y1="139" y2="139" />
    {buckets.map((item, index) => <g key={item.stamp}>
      <title>{date(new Date(item.stamp).toISOString())} · {item.calls} 次调用 · 已知输入 {number(item.input)} / 输出 {number(item.output)}</title>
      {[item.input, item.output].map((value, series) => <rect key={series} className={`usage-series-${series}`} x={index * width + width * (.15 + series * .35)}
        y={139 - (value ?? 0) / max * 130} width={width * .3} height={(value ?? 0) / max * 130} />)}
    </g>)}
  </svg><figcaption><span>{date(report.start)}</span><span className="usage-legend"><i /> 输入 <i /> 输出</span><span>{date(report.end)}</span></figcaption></figure>;
}
