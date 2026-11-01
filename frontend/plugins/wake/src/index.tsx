import { registerForm, Field } from "../../shared/src/configuration";
import { createRoot } from "react-dom/client";
import type { WebHostContextV1, WebUiDisposer } from "@akashic/web-ui-v1";
import { InitiativePage } from "./initiative";

export function activate(ctx: WebHostContextV1): WebUiDisposer {
  const configuration = registerForm(ctx, {id: "wake", title: "主动联系设置", description: "在合适时机主动向你发送消息，只推送到已配置的对话渠道。", fields: ({values, change, status}) => <>
      <label className="config-field"><span>接收消息的目标对话</span><select required value={values.delivery ? JSON.stringify(values.delivery) : ""} onChange={event => change("delivery", event.target.value ? JSON.parse(event.target.value) : null)}>
        <option value="">请选择接收渠道</option>{status.targets?.map(({label, ...target}) => <option key={JSON.stringify(target)} value={JSON.stringify(target)}>{label}</option>)}
      </select><small>仅列出已连接的渠道与对话。保存后不会自动发送测试消息。</small></label>
      <details><summary>高级选项</summary><Field label="时区" name="timezone" value={values.timezone} change={change} hint="默认使用系统本地时区" /></details>
    </>});
  const initiative = ctx.ui.inject("shell.settings.v1", mount => mount.register({
    id: "drift-initiative", label: "主动联系", route: "drift-initiative", order: 40,
    iconSvg: '<svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M4 5h16v11H8l-4 3zM8 9h8M8 12h5"/></svg>',
    render(host) {
      const root = createRoot(host);
      root.render(<InitiativePage ctx={ctx} />);
      return () => root.unmount();
    },
  }));
  return () => { initiative(); configuration(); };
}
