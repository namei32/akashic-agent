import { useEffect, useState } from "react";
import { registerForm, Field, type FormProps } from "../../shared/src/configuration";
import type { WebHostContextV1 } from "@akashic/web-ui-v1";

type Player = {riot_id: string; platform: string};
type Report = {id: string; created_at: string; body: string | null; error: string | null; published: number};
type Live = {collector_status: string; collector_error: string | null; observed_at: string | null;
  error: string; reports: Report[]; players: {riot_id: string; status: string; error: string | null}[]};

function Fields({values, change, status, ctx}: FormProps & {ctx: WebHostContextV1}) {
  const [live, setLive] = useState<Live | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [requestId, setRequestId] = useState<string | null>(null);
  const [notice, setNotice] = useState("");
  async function load() {
    const response = await ctx.http.request("/api/dashboard/lol_tracker/status");
    if (!response.ok) throw new Error("读取战绩状态失败，请刷新页面重试");
    setLive(await response.json() as Live);
  }
  useEffect(() => {
    let closed = false;
    const refresh = () => { if (!closed) void load().catch(e => setError(String(e.message ?? e))); };
    refresh(); const timer = window.setInterval(refresh, 15000);
    return () => { closed = true; window.clearInterval(timer); };
  }, []);
  const players = Array.isArray(values.players) ? values.players as Player[] : [];
  function edit(index: number, patch: Partial<Player>) {
    change("players", players.map((p, i) => i === index ? {...p, ...patch} : p));
  }
  async function report() {
    const id = requestId ?? crypto.randomUUID(); setRequestId(id); setBusy(true); setError(""); setNotice("");
    try {
      const response = await ctx.http.request("/api/dashboard/lol_tracker/report", {method: "POST",
        headers: {"Content-Type": "application/json"}, body: JSON.stringify({request_id: id, days: 7})});
      const body = await response.json() as {detail?: string; report_id?: string};
      if (!response.ok) throw new Error(body.detail ?? "报告生成失败");
      setNotice("报告已提交给 Wake；实际送达可在“主动消息”对话与 Wake 检查中查看。");
      setRequestId(null); await load();
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  }
  const collector = {ok: "采集正常", partial: "部分玩家查询失败", offline: "客户端离线或不可用", disabled: "采集已关闭", waiting: "等待首次采集", stale: "采集已延迟，当前使用缓存"};
  return <>
    <p className="config-hint">读取 Windows 上已登录的英雄联盟客户端，查询该区服的关注玩家。每日分析通过 Wake 发往已配置的对话。</p>
    <fieldset><legend>关注玩家</legend>{players.map((player, index) => <div key={index}>
      <label className="config-field"><span>玩家 {index+1}</span><input required aria-label={`玩家 ${index+1}`} value={player.riot_id}
        placeholder="namei#15100" onChange={e => edit(index, {riot_id: e.target.value})} /></label>
      <label className="config-field"><span>区服代号</span><input required aria-label={`区服 ${index+1}`} value={player.platform}
        onChange={e => edit(index, {platform: e.target.value.toUpperCase()})} /><small>艾欧尼亚为 HN1；需要客户端登录对应区服。</small></label>
      <button type="button" className="config-secondary" onClick={() => change("players", players.filter((_, i) => i !== index))}>移除玩家 {index+1}</button>
    </div>)}<button type="button" className="config-secondary" disabled={players.length >= 10}
      onClick={() => change("players", [...players, {riot_id: "", platform: "HN1"}])}>添加关注玩家</button></fieldset>
    <label className="config-field"><span>每天发送时间</span><input type="time" required value={String(values.daily_time ?? "09:00")}
      onChange={e => change("daily_time", e.target.value)} /></label>
    <label className="config-field"><span>汇总范围</span><select value={String(values.window ?? "previous_day")} onChange={e => change("window", e.target.value)}>
      <option value="previous_day">前一个自然日</option><option value="rolling_day">截至发送时间的过去 24 小时</option></select></label>
    <label className="config-checkbox"><input type="checkbox" checked={values.analysis_enabled !== false}
      onChange={e => change("analysis_enabled", e.target.checked)} />附带 AI 赛后分析</label>
    <details><summary>高级设置</summary>
      <Field label="时区" name="timezone" value={values.timezone} change={change} />
      <Field label="Windows 采集目录（WSL 路径）" name="bridge_directory" value={values.bridge_directory} change={change} />
    </details>
    <section><h2>采集与报告</h2>
      <p role="status">{live ? collector[live.collector_status as keyof typeof collector] ?? live.collector_status : "正在读取…"}
        {live?.observed_at && ` · 最近采集 ${new Date(live.observed_at).toLocaleString("zh-CN", {timeZone: "Asia/Shanghai"})}`}</p>
      <p className="config-hint">客户端关闭后使用已采集记录，报告会标明覆盖情况。训练、短局与重开单列，不计入普通胜率。</p>
      {live?.players.filter(p => p.status !== "ok").map(p => <p key={p.riot_id}>{p.riot_id}：{p.error ?? "查询未完成"}</p>)}
      <button type="button" className="config-secondary" disabled={busy || !status.ready} onClick={() => void report()}>
        {busy ? "正在生成并提交…" : requestId ? "核对并继续上次报告" : "立即分析近 7 天并推送"}</button>
      {notice && <p role="status">{notice}</p>}{error && <p role="alert">{error}</p>}
      {live?.reports.map(r => <details key={r.id}><summary>{new Date(r.created_at).toLocaleString("zh-CN", {timeZone:"Asia/Shanghai"})} · {r.published ? "已提交 Wake" : "待提交"}</summary>
        <p style={{whiteSpace:"pre-wrap", overflowWrap:"anywhere"}}>{r.body ?? "报告正在生成"}</p>{r.error && <p>分析状态：{r.error}</p>}</details>)}
    </section>
  </>;
}

export function activate(ctx: WebHostContextV1) {
  return registerForm(ctx, {id: "lol_tracker", title: "英雄联盟战绩", description: "关注玩家、查询战绩和每日赛后分析。",
    fields: props => <Fields {...props} ctx={ctx} />});
}
