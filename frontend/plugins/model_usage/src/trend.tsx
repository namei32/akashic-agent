import {date, number, type Report} from "./view";

export function Trend({report}: {report: Report}) {
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
