import type { FormProps } from "../../shared/src/configuration";

type Policy = {
  enabled: boolean; revision: number; excluded: string[]; preferred: string[];
  fixed_session: string | null; window_days: number; max_sessions: number;
};
const defaults: Policy = {enabled: false, revision: 0, excluded: [], preferred: [], fixed_session: null, window_days: 7, max_sessions: 3};

export function ActiveSourcesFields({values, change, status}: FormProps) {
  const policy = {...defaults, ...values.active_sources as Partial<Policy>};
  const original = {...defaults, ...status.values.active_sources as Partial<Policy>};
  const target = values.delivery as {channel?: string; recipient?: string; session_id?: string} | null;
  const supported = target?.channel === "akashic" && !!target.recipient && target.session_id === `akashic:${target.recipient}`;
  const update = (patch: Partial<Policy>) => change("active_sources", {
    ...policy, ...patch, revision: Math.max(original.revision + 1, policy.revision),
  });
  const options = new Map(status.targets?.filter(item => item.channel === "akashic")
    .map(item => [item.session_id, item.label]));
  for (const identity of [...policy.preferred, ...policy.excluded, ...(policy.fixed_session ? [policy.fixed_session] : [])]) {
    if (!options.has(identity)) options.set(identity, `${identity}（当前未列出，仍保留设置）`);
  }
  return <section aria-label="主动交流的会话来源">
    <label className="config-toggle">
      <span><strong>按用户活跃度自动选择会话</strong><small>默认关闭。仅影响取材，不改变接收消息的目标。</small></span>
      <input type="checkbox" role="switch" checked={policy.enabled} disabled={!supported && !policy.enabled}
        onChange={event => update({enabled: event.target.checked})} />
    </label>
    <p className="config-hint">开启后，允许将同一私人 Web 范围内的其他会话近期内容交给模型；材料可能保存在内部执行记录中。
      只统计你的真实发言，助手和后台消息不增加活跃度。不跨项目或渠道，也不代表每次都会联系。</p>
    {!supported && <p role="status" className="config-hint">自动取材仅支持原生私人 Web 目标；请先选择此类目标。
      {policy.enabled && "如需改用其他渠道，请先关闭自动取材再保存。"}</p>}
    {policy.enabled && <>
      <p className="config-hint">关闭并保存后恢复仅从目标会话取材；不会关闭主动联系、删除记录或撤回已开始的发送。
        使用长期记忆仍需要单独授权。</p>
      <details><summary>来源范围与优先级</summary>
        <label className="config-field"><span>活跃统计窗口（天）</span>
          <input type="number" min="1" max="30" step="1" required value={Number.isFinite(policy.window_days) ? policy.window_days : ""}
            onChange={event => update({window_days: event.target.valueAsNumber})} /></label>
        <label className="config-field"><span>每次最多取材会话数（不含固定会话）</span>
          <input type="number" min="1" max="3" step="1" required value={Number.isFinite(policy.max_sessions) ? policy.max_sessions : ""}
            onChange={event => update({max_sessions: event.target.valueAsNumber})} /></label>
        <label className="config-field"><span>固定取材会话（另计 1 个）</span>
          <select value={policy.fixed_session ?? ""} onChange={event => update({fixed_session: event.target.value || null})}>
            <option value="">不固定</option>
            {[...options].map(([id, label]) => <option key={id} value={id}>{label}</option>)}
          </select></label>
        <p>固定会话仍须有窗口内的用户发言；排除和隐私限制优先。固定取材不会改变投递目标。</p>
        <p>优先会话也必须满足身份、范围和活跃条件；排除会话不会参与自动取材。</p>
        {[...options].map(([identity, label]) => <label className="config-field" key={identity}>
          <span>{label}</span><select aria-label={`${label}的取材方式`}
            value={policy.excluded.includes(identity) ? "excluded" : policy.preferred.includes(identity) ? "preferred" : "auto"}
            onChange={event => update({
              excluded: [...policy.excluded.filter(item => item !== identity), ...(event.target.value === "excluded" ? [identity] : [])],
              preferred: [...policy.preferred.filter(item => item !== identity), ...(event.target.value === "preferred" ? [identity] : [])],
            })}>
            <option value="auto">按活跃度自动选择</option><option value="preferred">优先考虑</option><option value="excluded">排除</option>
          </select></label>)}
      </details>
    </>}
    <small>修改后需点击“保存配置”，并以生效回执为准。</small>
  </section>;
}
