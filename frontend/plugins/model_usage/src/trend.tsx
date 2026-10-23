import { useEffect, useRef, useState, type KeyboardEvent, type PointerEvent } from "react";
import { compact, date, labels, number, type Report } from "./view";

type Series = "input_tokens" | "output_tokens";
type Bucket = {stamp: number; value: number | null; calls: number; known: number};

export function Trend({report}: {report: Report}) {
  const [series, setSeries] = useState<Series>("input_tokens");
  const [active, setActive] = useState<{key: string; index: number} | null>(null);
  const [announcement, setAnnouncement] = useState("");
  const [width, setWidth] = useState(540);
  const svg = useRef<SVGSVGElement>(null);
  useEffect(() => {
    if (!svg.current) return;
    const observer = new ResizeObserver(([entry]) => setWidth(Math.max(220, Math.round(entry.contentRect.width))));
    observer.observe(svg.current);
    return () => observer.disconnect();
  }, []);
  const step = report.bucket_seconds * 1000;
  const key = `${report.start}/${report.end}/${series}`;
  const byTime = new Map(report.trend.map(item => [Date.parse(item.bucket), item]));
  const buckets: Bucket[] = [];
  for (let stamp = Math.floor(Date.parse(report.start) / step) * step; stamp < Date.parse(report.end); stamp += step) {
    const row = byTime.get(stamp);
    buckets.push({stamp, value: row ? row[series] : 0, calls: row?.calls ?? 0, known: row?.[`${series}_known_calls`] ?? 0});
  }
  const selected = active?.key === key ? active.index : null;
  const bucket = selected === null ? null : buckets[selected];
  const maximum = Math.max(2, ...buckets.map(item => item.value ?? 0));
  const scale = 10 ** Math.floor(Math.log10(maximum));
  const ceiling = [1, 2, 5, 10].find(value => value * scale >= maximum)! * scale;
  const left = 42, right = width - 8, bottom = 142, top = 10;
  const column = (right - left) / buckets.length;
  const known = buckets.some(item => item.calls > 0 && item.value !== null);
  const unknown = buckets.some(item => item.value === null);
  const interval = (item: Bucket) => {
    const start = Math.max(item.stamp, Date.parse(report.start));
    const end = Math.min(item.stamp + step, Date.parse(report.end));
    return `${date(new Date(start).toISOString())} — ${date(new Date(end).toISOString())}`;
  };
  const description = (item: Bucket) => `${interval(item)}，${labels[series]} ${number(item.value)} token，${item.known} / ${item.calls} 次调用有数据`;
  function point(event: PointerEvent<SVGSVGElement>) {
    const bounds = event.currentTarget.getBoundingClientRect();
    const x = (event.clientX - bounds.left) * width / bounds.width;
    const index = Math.max(0, Math.min(buckets.length - 1, Math.floor((x - left) / column)));
    setActive(previous => previous?.key === key && previous.index === index ? previous : {key, index});
  }
  function keyboard(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === "Escape" && selected !== null) {
      event.preventDefault(); event.stopPropagation(); setActive(null); setAnnouncement(""); return;
    }
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const current = selected ?? (event.key === "ArrowLeft" ? buckets.length : -1);
    const index = event.key === "Home" ? 0 : event.key === "End" ? buckets.length - 1
      : Math.max(0, Math.min(buckets.length - 1, current + (event.key === "ArrowLeft" ? -1 : 1)));
    setActive({key, index}); setAnnouncement(description(buckets[index]));
  }
  const tickIndices = [...new Set([0, Math.floor((buckets.length - 1) / 2), buckets.length - 1])];
  return <>
    <div className="usage-section-heading"><h2>用量走势</h2>
      <div className="usage-chart-switch" role="group" aria-label="趋势指标">{(["input_tokens", "output_tokens"] as const).map(field =>
        <button type="button" key={field} aria-pressed={series === field} onClick={() => { setSeries(field); setActive(null); setAnnouncement(""); }}>{labels[field]}</button>)}</div>
    </div>
    <div className={`usage-chart ${series === "input_tokens" ? "is-input" : "is-output"}`}>
      <div className="usage-chart-readout">
        <span>{bucket ? date(new Date(Math.max(bucket.stamp, Date.parse(report.start))).toISOString()) : `已知${labels[series]} · ${report.bucket_seconds === 3600 ? "每小时" : "每天"}`}</span>
        <strong title={number(bucket ? bucket.value : report.summary[series])}>{compact(bucket ? bucket.value : report.summary[series])} <small>tokens</small></strong>
        <small>{bucket ? `${bucket.known} / ${bucket.calls} 次调用有数据${bucket.value === null ? " · 用量未知" : ""}`
          : `${number(report.summary[`${series}_known_calls`])} / ${number(report.summary.calls)} 次调用有数据`}</small>
      </div>
      <div className="usage-chart-interactive" tabIndex={0} role="group" aria-label={`${labels[series]}趋势。左右方向键选择时段，Home 和 End 到首尾，Escape 清除选择。`}
        onKeyDown={keyboard} onBlur={() => setActive(null)} onPointerLeave={event => {
          if (event.pointerType === "mouse" && !event.currentTarget.contains(event.currentTarget.ownerDocument.activeElement)) setActive(null);
        }}>
        <svg ref={svg} viewBox={`0 0 ${width} 174`} role="img" aria-label={`${labels[series]} token 趋势`} onPointerMove={point} onPointerDown={point}>
          {[0, ceiling / 2, ceiling].map(value => {
            const y = bottom - value / ceiling * (bottom - top);
            return <g key={value}><line x1={left} x2={right} y1={y} y2={y} className="usage-chart-grid" />
              {known && <text x={left - 8} y={y + 4} textAnchor="end">{compact(value)}</text>}</g>;
          })}
          {buckets.map((item, index) => {
            const height = (item.value ?? 0) / ceiling * (bottom - top);
            return <g key={item.stamp}><title>{description(item)}</title>
              {selected === index && <rect className="usage-chart-selection" x={left + index * column} y={top} width={column} height={bottom - top} />}
              <rect className={`usage-chart-bar${item.value === null ? " is-unknown" : ""}`} x={left + (index + .17) * column}
                y={item.value === null ? bottom - 5 : bottom - height} width={column * .66} height={item.value === null ? 5 : height} />
            </g>;
          })}
          {!known && <text className="usage-chart-empty" x={(left + right) / 2} y={80} textAnchor="middle">暂无已知{labels[series]}用量</text>}
          {tickIndices.map(index => <text key={index} x={left + (index + .5) * column} y={165}
            textAnchor={index === 0 ? "start" : index === buckets.length - 1 ? "end" : "middle"}>
            {new Date(buckets[index].stamp).toLocaleString("zh-CN", report.bucket_seconds === 3600
              ? {hour: "2-digit", minute: "2-digit", hour12: false} : {month: "numeric", day: "numeric"})}
          </text>)}
        </svg>
      </div>
      <p className="usage-chart-hint">点选或悬停查看数值{unknown ? " · 虚线：未知" : ""}</p>
      <span className="usage-sr-only" role="status">{announcement}</span>
    </div>
  </>;
}
