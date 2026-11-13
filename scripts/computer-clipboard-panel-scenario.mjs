// Real Computer panel, srcdoc client, Worker and WebSocket protocol integration.
// This peer observes wire ordering; it does NOT implement the remote OS clipboard.
import assert from "node:assert/strict";
import { readFile, mkdtemp, rm } from "node:fs/promises";
import { createServer } from "node:http";
import { once } from "node:events";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { build } from "esbuild";
import { chromium } from "playwright-core";
import { WebSocketServer } from "ws";

const source = await readFile(process.env.SELKIES_BUNDLE, "utf8");
const panel = (await build({ entryPoints: ["plugins/computer/web/index.tsx"],
  bundle: true, format: "esm", write: false, loader: { ".css": "empty" } })).outputFiles[0].text;
const home = await mkdtemp(join(tmpdir(), "clipboard-panel-home-"));
const state = { type: "state", noticeId: 0, active: false, turns: [], control: false,
  browser: { state: "ready", error: "" }, targets: ["a", "b"].map(id => ({ id, title: id,
    url: "about:blank", anonymous: true, agent: false, turn: null, front: id === "a" })) };
let eventPeer, streamPeer, jpeg;
let sequence = 0;
const wire = [];
const server = createServer(async (request, response) => {
  const path = request.url;
  response.setHeader("content-type", path.endsWith(".js") ? "text/javascript" : "application/json");
  if (path === "/panel.js") return response.end(panel);
  if (path === "/api/dashboard/computer/stream-client") return response.end(source);
  if (path === "/api/dashboard/computer/activity") return response.end(JSON.stringify(state));
  if (path === "/api/dashboard/computer/show") {
    let raw = "";
    for await (const part of request) raw += part;
    const { target } = JSON.parse(raw);
    state.targets.forEach(item => { item.front = item.id === target; });
    eventPeer.send(JSON.stringify(state));
    return response.end("{}");
  }
  response.setHeader("content-type", "text/html");
  response.end(`<!doctype html><style>iframe{width:640px;height:360px} [hidden]{display:none!important}</style>
    <main id="host"></main><script type="module">
    import {activate} from '/panel.js';
    activate({http:{request:(path,init)=>fetch(path,init),webSocketUrl:path=>location.origin.replace('http:','ws:')+path},
      ui:{inject:(_id,hook)=>hook({register:entry=>entry.render(document.querySelector('#host'),null,
        {active:true,onActiveChange:()=>()=>{},requestAttention:()=>{}})})}});
    </script>`);
});
const sockets = new WebSocketServer({ server });
sockets.on("connection", (socket, request) => {
  if (request.url === "/api/dashboard/computer/events") {
    eventPeer = socket;
    socket.send(JSON.stringify(state));
    return;
  }
  streamPeer = socket;
  let initialized = false;
  socket.on("message", data => {
    const text = data.toString();
    wire.push(text);
    if (text === "_gz,1") socket.send("_gz,1");
    if (!initialized && text.startsWith("SETTINGS,")) {
      initialized = true;
      socket.send("MODE websockets");
      socket.send(JSON.stringify({ type: "server_settings", settings: {
        encoder: { value: "jpeg", locked: true }, clipboard_enabled: { value: true, locked: true },
        clipboard_in_enabled: { value: true, locked: true }, audio_enabled: { value: false, locked: true },
        video_on_start: { value: true, locked: true }, audio_on_start: { value: false, locked: true },
      } }));
      socket.send(JSON.stringify({ type: "stream_resolution", width: 640, height: 360 }));
    }
  });
  const frames = setInterval(() => {
    if (!initialized || !jpeg || socket.readyState !== 1) return;
    const header = Buffer.alloc(6);
    header[0] = 3;
    header.writeUInt16BE(++sequence % 65536, 2);
    socket.send(Buffer.concat([header, jpeg]));
  }, 60);
  socket.on("close", () => clearInterval(frames));
});
server.listen(0, "127.0.0.1");
await once(server, "listening");
let browser;
try {
  browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH, headless: true,
    env: { ...process.env, HOME: home, XDG_CONFIG_HOME: home, XDG_CACHE_HOME: home } });
  const page = await browser.newPage({ viewport: { width: 800, height: 650 } });
  page.on("pageerror", error => console.error("Browser error:", error.message));
  await page.setViewportSize({ width: 640, height: 360 });
  jpeg = await page.screenshot({ type: "jpeg" });
  await page.setViewportSize({ width: 800, height: 650 });
  // Register before the real panel so receipts can be delayed at its message boundary.
  await page.addInitScript(() => {
    window.heldReceipts = [];
    window.holdReceipts = false;
    window.addEventListener('message', event => {
      if (event.data?.type === 'computerClipboardSent' && window.holdReceipts) {
        window.heldReceipts.push(event.data);
        event.stopImmediatePropagation();
      }
    }, true);
  });
  await page.goto(`http://127.0.0.1:${server.address().port}`);
  await page.waitForFunction(() => document.querySelector("iframe")?.contentWindow?.fps > 0);
  await page.getByRole("button", { name: "剪贴板", exact: true }).click();
  await page.getByRole("textbox", { name: "剪贴板文字" }).waitFor({ timeout: 4000 }).catch(async error => {
    console.error(await page.evaluate(() => ({ panel: document.body.innerText,
      frames: [...document.querySelector('iframe').contentDocument.querySelectorAll('canvas,video')]
        .map(node => ({ id: node.id, display: getComputedStyle(node).display, width: node.width, height: node.height })),
      input: Boolean(document.querySelector('iframe').contentWindow.webrtcInput) })));
    throw error;
  });
  const send = page.getByRole("button", { name: "粘贴到浏览器", exact: true });
  for (const text of ["panel-example", "中文\n多行", "中文\n多行"]) {
    const before = wire.length;
    await page.getByRole("textbox", { name: "剪贴板文字" }).fill(text);
    await send.click();
    await page.waitForFunction(() => document.querySelector(".computer-clipboard-status")?.textContent === "已粘贴");
    await page.waitForTimeout(100);
    const sent = wire.slice(before);
    assert.ok(sent.includes("kd,118"), "Actual Ctrl+V must reach the WebSocket peer");
    const clipboard = sent.findIndex(item => item.startsWith("cw,"));
    assert.ok(clipboard === -1 || clipboard < sent.indexOf("kd,118"), "text must precede paste key");
  }
  console.log("PASS: real panel English/Chinese/multiline/repeated paste sends ordered wire frames");

  const hold = () => page.evaluate(() => { window.heldReceipts = []; window.holdReceipts = true; });
  const flush = () => page.evaluate(() => {
    window.holdReceipts = false;
    const frame = document.querySelector('iframe').contentWindow;
    for (const receipt of window.heldReceipts) frame.eval(`parent.postMessage(${JSON.stringify(receipt)}, origin)`);
  });
  const receiptHeld = () => page.waitForFunction(() => window.heldReceipts.length > 0);
  await hold();
  const beforePermission = wire.length;
  await page.getByRole('textbox', { name: '剪贴板文字' }).fill('permission-change');
  await send.click();
  await receiptHeld();
  state.active = true;
  eventPeer.send(JSON.stringify(state));
  await page.waitForFunction(() => document.querySelector('.computer-clipboard-status').textContent.includes('权限已变化'));
  state.active = false;
  eventPeer.send(JSON.stringify(state));
  await page.getByRole('textbox', { name: '剪贴板文字' }).waitFor();
  await send.click();
  await page.waitForFunction(() => document.querySelector('.computer-clipboard-status').textContent.includes('尚未结束'));
  await flush();
  await page.waitForTimeout(100);
  assert.ok(!wire.slice(beforePermission).includes('kd,118'));
  console.log('PASS: permission loss/recovery cancels paste; pending transfer excludes a second request');

  await hold();
  await send.click();
  await receiptHeld();
  const beforeDuplicate = wire.filter(item => item === 'kd,118').length;
  await flush();
  await page.waitForFunction(() => document.querySelector('.computer-clipboard-status').textContent === '已粘贴');
  await flush();
  await page.waitForTimeout(100);
  assert.equal(wire.filter(item => item === 'kd,118').length, beforeDuplicate + 1);
  console.log('PASS: duplicate receipts produce exactly one paste');

  const beforeSwitch = wire.length;
  await page.getByRole("textbox", { name: "剪贴板文字" }).fill("target-change");
  await page.evaluate(() => {
    [...document.querySelectorAll("button")].find(node => node.textContent === "粘贴到浏览器").click();
    document.querySelectorAll(".computer-tab")[1].click();
  });
  await page.waitForFunction(() => document.querySelector(".computer-clipboard-status")?.textContent.includes("目标标签已变化"));
  await page.waitForTimeout(250);
  assert.ok(!wire.slice(beforeSwitch).includes("kd,118"));
  console.log("PASS: target switch cancels the in-flight paste before Ctrl+V");

  await page.getByRole("textbox", { name: "剪贴板文字" }).fill("disconnect-change");
  await send.click({ trial: true });
  await page.evaluate(() => {
    [...document.querySelectorAll("button")].find(node => node.textContent === "粘贴到浏览器").click();
    document.querySelector("iframe").contentWindow.selkiesTransport.close();
  });
  await page.waitForFunction(() => document.querySelector(".computer-clipboard-status")?.textContent.includes("关闭"));
  console.log("PASS: disconnect rejects an in-flight request");

  await page.getByRole('textbox', { name: '剪贴板文字' }).waitFor();
  await hold();
  await page.getByRole('textbox', { name: '剪贴板文字' }).fill('timeout-no-retry');
  const beforeTimeout = wire.length;
  await send.click();
  await receiptHeld();
  await page.waitForFunction(() => document.querySelector('.computer-clipboard-status').textContent.includes('结果未知'));
  assert.equal(await page.locator('iframe').count(), 0);
  assert.ok(!wire.slice(beforeTimeout).includes('kd,118'));
  assert.deepEqual(wire.slice(beforeTimeout).filter(item => item.startsWith('cw,')),
    [`cw,${Buffer.from('timeout-no-retry').toString('base64')}`]);
  console.log('PASS: missing receipt closes the connection with unknown-result error, no paste or retry');
} finally {
  await browser?.close();
  for (const socket of sockets.clients) socket.terminate();
  await new Promise(resolve => sockets.close(resolve));
  await new Promise(resolve => server.close(resolve));
  await rm(home, { recursive: true, force: true });
}
