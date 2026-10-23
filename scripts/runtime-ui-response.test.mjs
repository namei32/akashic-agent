import assert from "node:assert/strict";
import { setImmediate } from "node:timers/promises";
import test from "node:test";
import { JSDOM } from "jsdom";

const modules = [
  ["source", await import("../frontend/plugins/runtime_ui/src/module.js")],
  ["built", await import("../plugins/runtime_ui/web_module.js")],
];

for (const [label, { activate }] of modules) {
  for (const [name, body, status, expected] of [
    ["plain-text server error", "Internal Server Error", 500, "读取失败：HTTP 500"],
    ["structured server error", JSON.stringify({ detail: "MCP 暂不可用" }), 409, "MCP 暂不可用"],
    ["invalid successful payload", "<html>not a directory response</html>", 200, "运行目录返回了无效数据"],
    ["empty tool catalog", JSON.stringify({ owner_id: "computer@local", tools: [] }), 200, "0 个工具 · computer@local"],
  ]) {
    test(`${label}: ${name}`, async () => {
      const dom = new JSDOM('<main id="host"></main>');
      const previous = globalThis.document;
      globalThis.document = dom.window.document;
      let dispose;
      try {
        activate({
          ui: { inject(_id, mount) { return mount({ register(page) {
            dispose = page.render(dom.window.document.querySelector("#host"));
            return () => {};
          } }); } },
          http: { async request(path) {
            if (path.startsWith("/api/dashboard/runtime/mcp?")) return new Response(body, { status });
            const payload = path.endsWith("/capabilities")
              ? { plugins: [], skills: [], mcp_servers: [{ owner_id: "computer@local", name: "computer" }] }
              : { items: [] };
            return new Response(JSON.stringify(payload));
          } },
        });
        await setImmediate();
        const tab = [...document.querySelectorAll('[role="tab"]')].find(node => node.textContent === "MCP 与 Skills");
        tab.click();
        document.querySelector(".runtime-directory__item").click();
        await setImmediate();
        const detail = document.querySelector(".runtime-detail__scroll").textContent;
        assert.ok(detail.includes(expected), detail);
        assert.ok(!detail.includes("Unexpected token") && !detail.includes("expected pattern"), detail);
      } finally {
        dispose?.();
        globalThis.document = previous;
        dom.window.close();
      }
    });
  }
}
