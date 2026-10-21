export interface CompanionReference { message_id: string | null; text: string }
export interface CompanionMessage { id: string; role: "user" | "assistant"; text: string; partial: boolean }
export interface CompanionRun { request_id: string; status: "running" | "complete" | "failed" | "interrupted"; text: string; detail: string }
export interface CompanionState {
  history: CompanionMessage[];
  preferences: { explanation: string };
  reference: CompanionReference | null;
  active: CompanionRun | null;
}
export interface CompanionRequest { request_id: string; parent: string; question: string; reference: CompanionReference | null }

export class CompanionRequestError extends Error {
  constructor(message: string, readonly status: number) { super(message); }
}

export async function companionApi<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(`/api/companion${path}`, {
    method: body === undefined ? "GET" : "POST", credentials: "same-origin", cache: "no-store",
    headers: body === undefined ? {} : { "Content-Type": "application/json", "X-Akashic-CSRF": "1" },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  const value: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = value && typeof value === "object" && "detail" in value ? value.detail : "";
    throw new CompanionRequestError(typeof detail === "string" && detail ? detail
      : response.status === 401 ? "登录已失效，请刷新页面重新登录。" : "暂时连不上桌宠，请重试。", response.status);
  }
  return value as T;
}

/** HTTP 流只读取已接纳的答疑；重新连接不重新提交模型调用。 */
export async function readCompanionRun(id: string, receive: (run: CompanionRun) => void, signal: AbortSignal): Promise<void> {
  const response = await fetch(`/api/companion/events/${encodeURIComponent(id)}`, { signal, credentials: "same-origin", cache: "no-store" });
  if (!response.ok || !response.body) throw new Error("答疑连接中断，请点击重连继续查看。");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let settled = false;
  try {
    while (true) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";
      for (const line of lines) {
        if (!line.trim()) continue;
        const run = JSON.parse(line) as CompanionRun;
        receive(run);
        settled ||= run.status !== "running";
      }
      if (done) break;
    }
    if (!settled) throw new Error("答疑连接中断，请点击重连继续查看。");
  } finally {
    reader.releaseLock();
  }
}
