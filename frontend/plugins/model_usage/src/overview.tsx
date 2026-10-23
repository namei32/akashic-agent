import { compact, labels, number, rate, seconds, tokenFields, type Report } from "./view";

/** 原始整数始终可读；缩写仅改变显示，触屏和键盘也能展开完整值。 */
export function ExactNumber({value, label}: {value: number | null; label: string}) {
  if (value === null) return <>—</>;
  return <details className="usage-exact" onBlur={event => {
    if (!event.currentTarget.contains(event.relatedTarget)) event.currentTarget.open = false;
  }} onKeyDown={event => { if (event.key === "Escape") { event.currentTarget.open = false; event.stopPropagation(); } }}>
    <summary title="查看完整数值" aria-label={`${label} ${number(value)}，查看完整数值`}>{compact(value)}</summary>
    <span>{number(value)} <small>tokens</small></span>
  </details>;
}

export function Overview({totals}: {totals: Report["summary"]}) {
  return <>
    <dl className="usage-primary" aria-label="核心指标">
      {(["input_tokens", "output_tokens"] as const).map(field => <div key={field}>
        <dt>{labels[field]} <small>tokens</small></dt>
        <dd><ExactNumber value={totals[field]} label={labels[field] + " token"} /></dd>
        <small>已知 {number(totals[`${field}_known_calls`])} / {number(totals.calls)} 次</small>
      </div>)}
      <div><dt>端到端吞吐</dt><dd>{number(totals.tokens_per_second, 1)} <small>tok/s</small></dd><small>含首段前等待</small></div>
      <div><dt>首段响应</dt><dd>{seconds(totals.first_token_ms)}</dd><small>平均 · {number(totals.first_token_known_calls)} 次有计时</small></div>
    </dl>
    <div className="usage-summary" aria-label="调用概况">
      <strong>{number(totals.calls)} 次调用</strong><span>{number(totals.success)} 成功</span>
      <span className={totals.errors ? "usage-warning" : ""}>{number(totals.errors)} 失败</span>
      <span>{number(totals.unsettled)} 未结算</span><span>已知重试 {number(totals.retries)} 次</span>
      {totals.attempt_known_calls < totals.calls && <span>{number(totals.calls - totals.attempt_known_calls)} 次缺少尝试序号</span>}
    </div>
    <details className="usage-secondary">
      <summary><span>缓存与推理</span><span>缓存命中 {rate(totals.cache_hit_rate)}</span></summary>
      <dl>{tokenFields.slice(2).map(field => <div key={field}><dt>{labels[field]} token</dt>
        <dd>{number(totals[field])}</dd><small>已知 {number(totals[`${field}_known_calls`])} / {number(totals.calls)} 次</small></div>)}
        <div><dt>平均总耗时</dt><dd>{seconds(totals.duration_ms)}</dd></div></dl>
      <p>缓存命中率按 token 加权，覆盖 {number(totals.cache_known_calls)} 次输入与缓存同时已知的调用。</p>
    </details>
  </>;
}
