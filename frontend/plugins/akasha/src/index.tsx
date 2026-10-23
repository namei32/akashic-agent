import { registerForm } from "../../shared/src/configuration";
import type { WebHostContextV1, WebUiDisposer } from "@akashic/web-ui-v1";

// Akasha 的 Web 入口只有设置分节；每轮召回展示由 message_ui.js 经插件界面插槽提供。
export function activate(ctx: WebHostContextV1): WebUiDisposer {
  return registerForm(ctx, {id: "akasha", title: "Akasha 记忆服务", description: "提供情景记忆、项目记忆策略与兴趣评分；当前职责见下方状态。"});
}
