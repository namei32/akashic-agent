export function activate(ctx) {
  return ctx.ui.inject("shell.pages.v1", (mount) => mount.register({
    id: "runtime",
    label: "知识与运行",
    iconSvg: '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-book-open-text" aria-hidden="true"><path d="M12 5v16"></path><path d="M16 13h2"></path><path d="M16 9h2"></path><path d="M20.001 19A2 2 0 0022 17V5a2 2 0 00-1.999-2L16 3.002A5 5 0 0012 5a5 5 0 00-4-2H4a2 2 0 00-2 2v12a2 2 0 001.999 2H8a5 5 0 014 2 5 5 0 014-2z"></path><path d="M6 13h2"></path><path d="M6 9h2"></path></svg>',
    route: "runtime",
    order: 25,
    render(host) {
      return mountRuntime(host);
    },
  }));
}

function mountRuntime(host) {
  const abort = new AbortController();
  let detailAbort;
  let selectedView = "documents";
  let refreshing = false;
  let closed = false;
  const data = { documents: null, mcp: null, jobs: null };
  const errors = {};
  const views = { documents: "文档", mcp: "MCP 与 Skills", jobs: "定时任务" };
  const root = document.createElement("section");
  root.className = "runtime-dashboard";
  root.innerHTML = `<header class="runtime-dashboard__topbar"><div><h1>知识与运行</h1><p>当前电脑的只读运行目录</p></div><button type="button" data-refresh>刷新</button></header>
    <section class="runtime-dashboard__metrics" aria-label="运行摘要"></section>
    <nav class="runtime-dashboard__tabs" role="tablist" aria-label="知识与运行目录"></nav>
    <div class="runtime-dashboard__notice-slot"></div>
    <section class="runtime-directory"><div class="runtime-directory__list"></div><article class="runtime-detail"><header class="runtime-detail__header"><h2>选择一个项目</h2></header><div class="runtime-detail__scroll"></div></article></section>`;
  host.replaceChildren(root);
  const node = (selector) => root.querySelector(selector);
  const element = (tag, text, className = "") => {
    const value = document.createElement(tag);
    value.textContent = text;
    value.className = className;
    return value;
  };
  const request = async (path, signal = abort.signal) => {
    const response = await fetch(path, { signal });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || `读取失败：HTTP ${response.status}`);
    return payload;
  };
  const clearDetail = () => {
    detailAbort?.abort();
    node(".runtime-detail__header").replaceChildren(element("h2", "选择一个项目"));
    node(".runtime-detail__scroll").replaceChildren(element("p", "请从左侧选择内容。", "runtime-detail__loading"));
  };
  const showDetail = async (item) => {
    detailAbort?.abort();
    const controller = new AbortController();
    detailAbort = controller;
    node(".runtime-detail__header").replaceChildren(element("h2", item.title));
    node(".runtime-detail__scroll").replaceChildren(element("p", "正在读取…", "runtime-detail__loading"));
    try {
      const value = await request(item.path, controller.signal);
      if (closed || controller.signal.aborted) return;
      node(".runtime-detail__scroll").replaceChildren(element("pre", value.markdown || "当前没有内容。", "runtime-detail__text"));
    } catch (error) {
      if (closed || controller.signal.aborted) return;
      node(".runtime-detail__scroll").replaceChildren(element("p", error.message, "runtime-dashboard__error"));
    }
  };
  const draw = () => {
    if (closed) return;
    const metrics = node(".runtime-dashboard__metrics");
    metrics.replaceChildren();
    for (const [view, title, count] of [
      ["documents", "核心文档", data.documents?.items?.length],
      ["mcp", `MCP 服务 · ${data.mcp?.skills?.length ?? "—"} Skills`, data.mcp?.mcp_servers?.length],
      ["jobs", "已启用定时任务", data.jobs?.items?.filter((job) => job.enabled).length],
    ]) {
      const metric = element("div", "", `runtime-metric ${view}`);
      metric.append(element("small", title), element("strong", count === undefined ? errors[view] ? "不可用" : "—" : String(count)));
      metrics.append(metric);
    }
    const tabs = node(".runtime-dashboard__tabs");
    tabs.replaceChildren();
    for (const [view, label] of Object.entries(views)) {
      const tab = element("button", label);
      tab.type = "button";
      tab.setAttribute("role", "tab");
      tab.setAttribute("aria-selected", String(view === selectedView));
      tab.onclick = () => { selectedView = view; clearDetail(); draw(); };
      tabs.append(tab);
    }
    const notice = node(".runtime-dashboard__notice-slot");
    notice.replaceChildren();
    if (errors[selectedView]) {
      const error = element("div", errors[selectedView], "runtime-dashboard__error");
      error.setAttribute("role", "alert");
      notice.append(error);
    }
    const listing = node(".runtime-directory__list");
    listing.replaceChildren(element("h2", views[selectedView]));
    let items = [];
    if (selectedView === "documents") items = (data.documents?.items || []).map((item) => ({
      title: item.title, description: item.description, disabled: !item.available,
      path: `/api/chat/runtime/documents/${encodeURIComponent(item.id)}`,
    }));
    if (selectedView === "mcp") items = (data.mcp?.mcp_servers || []).map((item) => ({
      title: item.name, description: `已注册 · ${item.owner_id} · 点击按需读取工具`,
      path: `/api/chat/runtime/mcp?${new URLSearchParams({ owner_id: item.owner_id, name: item.name })}`,
    }));
    if (selectedView === "jobs") items = (data.jobs?.items || []).map((item) => ({
      title: item.name || "未命名任务", description: `${item.enabled ? "启用" : "停用"} · ${item.timezone}`,
      path: `/api/chat/runtime/jobs/${encodeURIComponent(item.id)}`,
    }));
    for (const item of items) {
      const button = element("button", "", "runtime-directory__item");
      button.type = "button";
      button.disabled = !!item.disabled;
      const copy = element("span", "", "runtime-directory__item-copy");
      copy.append(element("strong", item.title), element("small", item.description));
      button.append(copy);
      button.onclick = () => void showDetail(item);
      listing.append(button);
    }
    if (!items.length) listing.append(element("p", errors[selectedView] ? "此目录读取失败，请刷新重试。" : refreshing ? "正在读取…" : "当前目录没有可展示的项目。", "runtime-directory__empty"));
  };
  const refresh = async () => {
    if (refreshing || closed) return;
    refreshing = true;
    node("[data-refresh]").disabled = true;
    clearDetail();
    const paths = { documents: "/api/chat/runtime/documents", mcp: "/api/chat/runtime/capabilities", jobs: "/api/chat/runtime/jobs" };
    await Promise.allSettled(Object.entries(paths).map(async ([view, path]) => {
      try {
        const value = await request(path);
        if (closed) return;
        data[view] = value;
        delete errors[view];
      } catch (error) {
        if (closed) return;
        data[view] = null;
        errors[view] = error.message;
      }
      draw();
    }));
    if (!closed) { refreshing = false; node("[data-refresh]").disabled = false; draw(); }
  };
  node("[data-refresh]").onclick = () => void refresh();
  draw();
  void refresh();
  return () => { closed = true; abort.abort(); detailAbort?.abort(); host.replaceChildren(); };
}
