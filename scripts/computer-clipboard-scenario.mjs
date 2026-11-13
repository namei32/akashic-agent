// Real Chromium + the pinned, built Selkies client + a local WebSocket protocol peer.
// No personal profile, OS clipboard reads, or running Computer instance is involved.
// SELKIES_BUNDLE=/.../dist/selkies-core.js CHROMIUM_PATH=/.../chrome node scripts/computer-clipboard-scenario.mjs
import assert from "node:assert/strict";
import { readFile, mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createServer } from "node:http";
import { once } from "node:events";
import { chromium } from "playwright-core";
import { WebSocketServer } from "ws";

if (!process.env.SELKIES_BUNDLE) throw new Error("Set SELKIES_BUNDLE to the pinned, built Computer client");
const source = (await readFile(process.env.SELKIES_BUNDLE, "utf8"))
  .replaceAll("window.location.origin", "window.origin");
const server = createServer((request, response) => {
  response.setHeader("content-type", request.url === "/client.js" ? "text/javascript" : "text/html");
  response.end(request.url === "/client.js" ? source : '<!doctype html><title>Clipboard protocol scenario</title>');
});
const sockets = new WebSocketServer({ server });
let peer;
const wire = [];
sockets.on("connection", socket => {
  peer = socket;
  let initialized = false;
  socket.on("message", data => {
    const text = data.toString();
    wire.push(text);
    if (!initialized && text.startsWith("SETTINGS,")) {
      initialized = true;
      socket.send("MODE websockets");
      socket.send(JSON.stringify({ type: "server_settings", settings: {
        clipboard_enabled: { value: true, locked: true },
        clipboard_in_enabled: { value: true, locked: true },
        audio_enabled: { value: false, locked: true },
        microphone_enabled: { value: false, locked: true },
      } }));
    }
  });
});
server.listen(0, "127.0.0.1");
await once(server, "listening");
const origin = `http://127.0.0.1:${server.address().port}`;
const home = await mkdtemp(join(tmpdir(), "clipboard-browser-home-"));
let browser;
try {
  browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH, headless: true,
    env: { ...process.env, HOME: home, XDG_CONFIG_HOME: home, XDG_CACHE_HOME: home } });
  const page = await browser.newPage();
  page.on("pageerror", error => console.error("Browser error:", error.message));
  await page.goto(origin);
  await page.evaluate(({ origin }) => {
    window.receipts = [];
    window.roles = [];
    window.addEventListener("message", event => {
      if (event.source !== window.client?.contentWindow || event.origin !== location.origin) return;
      if (event.data?.type === "computerClipboardSent") window.receipts.push(event.data);
      if (event.data?.type === "clientRoleUpdate") window.roles.push(event.data.role);
    });
    const frame = document.createElement("iframe");
    window.client = frame;
    frame.srcdoc = `<!doctype html><div id="app"></div><script>
      window.__SELKIES_STREAMING_MODE__ = 'websockets';
      window.__SELKIES_WEBSOCKET_URL__ = ${JSON.stringify(origin.replace("http:", "ws:") + "/stream")};
      window.__SELKIES_STORAGE_URL__ = ${JSON.stringify(origin + "/stream")};
      window.addEventListener('message', event => {
        if (event.source === window && event.data?.type === 'clientRoleUpdate')
          parent.postMessage(event.data, window.origin);
      });
      </script><script type="module" src="${origin}/client.js"></script>`;
    document.body.append(frame);
  }, { origin });
  await page.waitForFunction(() => window.client.contentWindow.webrtcInput
    && window.client.contentWindow.clipboard_enabled === true, { timeout: 15000 });
  const send = (requestId, text) => page.evaluate(({ requestId, text }) => {
    window.client.contentWindow.postMessage({ type: "clipboardUpdateFromUI", requestId, text }, location.origin);
  }, { requestId, text });
  const receipt = async id => {
    await page.waitForFunction(id => window.receipts.some(item => item.id === id), id, { timeout: 2500 });
    return page.evaluate(id => window.receipts.find(item => item.id === id), id);
  };
  for (const [id, text] of [[1, "clipboard-example"], [2, "中文测试\n第二行"], [3, "中文测试\n第二行"]]) {
    await send(id, text);
    assert.equal((await receipt(id)).error, "");
  }
  assert.ok(wire.some(text => text.startsWith("cw,")), "Actual WebSocket clipboard frame must be sent");
  console.log("PASS: English, Chinese, multiline and repeated text receive successful send receipts");

  peer.send(JSON.stringify({ type: "server_settings", settings: {
    clipboard_enabled: { value: false, locked: true },
  } }));
  await page.waitForFunction(() => window.client.contentWindow.clipboard_enabled === false);
  const beforeDisabled = wire.filter(text => text.startsWith("cw,")).length;
  await send(4, "must-not-be-sent");
  assert.match((await receipt(4)).error, /turned off/);
  assert.equal(wire.filter(text => text.startsWith("cw,")).length, beforeDisabled);
  console.log("PASS: server policy refusal returns an error without sending clipboard data");

  peer.send('AUTH_SUCCESS,{"role":"viewer","slot":null}');
  await page.waitForFunction(() => window.roles.includes("viewer"));
  const beforeReadonly = wire.filter(text => text.startsWith("cw,")).length;
  await send(5, "readonly-must-not-be-sent");
  assert.match((await receipt(5)).error, /只读/);
  assert.equal(wire.filter(text => text.startsWith("cw,")).length, beforeReadonly);
  assert.equal(await page.evaluate(() => window.receipts.filter(item => item.id === 5).length), 1);
  console.log("PASS: viewer refusal receives exactly one explicit error instead of timing out");

  await page.evaluate(() => {
    const sibling = document.createElement("iframe");
    sibling.srcdoc = `<script>parent.client.contentWindow.postMessage({type:'clipboardUpdateFromUI',requestId:99,text:'wrong source'}, origin)</script>`;
    document.body.append(sibling);
  });
  await page.waitForTimeout(150);
  assert.equal(await page.evaluate(() => window.receipts.some(item => item.id === 99)), false);
  console.log("PASS: another same-origin frame cannot request clipboard writes");
} finally {
  await browser?.close();
  for (const socket of sockets.clients) socket.terminate();
  await new Promise(resolve => sockets.close(resolve));
  await new Promise(resolve => server.close(resolve));
  await rm(home, { recursive: true, force: true });
}
