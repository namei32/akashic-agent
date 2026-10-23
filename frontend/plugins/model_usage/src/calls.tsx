import { useLayoutEffect, useRef, useState } from "react";
import { compact, date, labels, number, seconds, tokenFields, type Call } from "./view";

/** 同一组调用在窄屏重排为摘要列表，详情和身份只有一个 DOM owner。 */
export function Calls({calls, pageKey}: {calls: Call[]; pageKey: string}) {
  const region = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => { region.current?.scrollTo({top: 0}); }, [pageKey]);
  return <div className="usage-table-scroll" ref={region} tabIndex={0} role="region" aria-label="调用明细，滚动查看">
    <table><thead><tr><th>模型 / 时间</th><th className="usage-numeric">输入</th><th className="usage-numeric">输出</th>
      <th className="usage-numeric">耗时</th><th>结果</th><th><span className="usage-sr-only">调用详情</span></th></tr></thead>
      <tbody>{calls.map(call => <CallRow key={call.id} call={call} />)}
        {!calls.length && <tr><td colSpan={6} className="usage-empty">这段时间没有调用记录。可以更换时间范围或模型。</td></tr>}
      </tbody></table>
  </div>;
}

function CallRow({call}: {call: Call}) {
  const [expanded, setExpanded] = useState(false);
  const coverage = call.coverage === "exact" ? "输入 / 输出已统计" : call.coverage === "partial" ? "部分用量未知" : "用量未知";
  const speed = call.state === "success" && call.duration_ms !== null && call.duration_ms > 0 && call.output_tokens !== null
    ? call.output_tokens * 1000 / call.duration_ms : null;
  const detail = [["调用 ID", call.id], ["尝试序号", number(call.attempt === null ? null : call.attempt + 1)],
    ["首段响应", seconds(call.first_token_ms)], ["端到端吞吐", `${number(speed, 1)} tok/s`],
    ...tokenFields.map(field => [labels[field] + " token", number(call[field])])];
  return <><tr className="usage-call-row">
    <td className="usage-call-name"><strong className="usage-model">{call.model || "未知模型"}</strong><time dateTime={call.started_at}>{date(call.started_at)}</time></td>
    <td className="usage-numeric" data-label="输入" title={number(call.input_tokens)}><span aria-label={`输入 ${number(call.input_tokens)} token`}>{compact(call.input_tokens)}</span></td>
    <td className="usage-numeric" data-label="输出" title={number(call.output_tokens)}><span aria-label={`输出 ${number(call.output_tokens)} token`}>{compact(call.output_tokens)}</span></td>
    <td className="usage-numeric" data-label="耗时">{seconds(call.duration_ms)}</td>
    <td className="usage-call-state"><span className={`usage-state is-${call.state}`}>{({started: "未结算", success: "成功", error: "失败"})[call.state]}</span></td>
    <td className="usage-call-toggle"><button type="button" className="usage-expand" aria-expanded={expanded} aria-controls={`usage-call-${call.id}`}
      aria-label={`${expanded ? "收起" : "展开"} ${call.model} ${date(call.started_at)} 的调用详情`}
      onClick={() => setExpanded(!expanded)}>{expanded ? "收起" : "详情"}</button></td>
  </tr>{expanded && <tr className="usage-detail-row"><td colSpan={6}>
    <p>{coverage}</p><dl id={`usage-call-${call.id}`} className="usage-call-detail">
      {detail.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}
    </dl></td></tr>}</>;
}
