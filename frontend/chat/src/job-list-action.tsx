import { useEffect, useId, useMemo, useRef, useState } from "react";
import { ChevronDown, Square } from "lucide-react";
import "./job-list-action.css";

export type JobStatus = "running" | "stopping" | "completed" | "failed" | "timeout" | "killed" | "unknown";

type SubagentProgress = {
  steps: number;
  actions: { at: number; label: string }[];
  report: "inline" | "waiting" | "delivered" | "failed";
  report_message_id?: string | null;
};

/** 后台任务只读镜像的一行；时间为 epoch 毫秒。 */
export interface JobView {
  id: string;
  kind: string;
  label: string;
  status: JobStatus;
  detail: string;
  started_at: number;
  finished_at: number | null;
  stoppable: boolean;
  subagent?: SubagentProgress;
}

export interface SessionJobs {
  sessionId: string;
  jobs: JobView[];
}

const STATUS_LABEL: Record<JobStatus, string> = {
  running: "运行中",
  stopping: "正在停止",
  completed: "已完成",
  failed: "失败",
  timeout: "超时",
  killed: "已停止",
  unknown: "状态未知",
};

const REPORT_LABEL: Record<SubagentProgress["report"], string> = {
  inline: "随当前回复返回",
  waiting: "等待回报",
  delivered: "已回报",
  failed: "回报失败",
};

function isSubagentProgress(value: unknown): value is SubagentProgress {
  if (typeof value !== "object" || value === null) return false;
  const progress = value as Record<string, unknown>;
  return typeof progress.steps === "number" && Number.isSafeInteger(progress.steps) && progress.steps >= 0
    && typeof progress.report === "string" && Object.hasOwn(REPORT_LABEL, progress.report)
    && (progress.report_message_id == null || typeof progress.report_message_id === "string")
    && Array.isArray(progress.actions) && progress.actions.every((action: unknown) =>
      typeof action === "object" && action !== null && "at" in action
      && Number.isSafeInteger(action.at) && "label" in action && typeof action.label === "string");
}

const isJob = (value: unknown): value is JobView => {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return false;
  const row = value as Record<string, unknown>;
  return typeof row.id === "string" && row.id.length > 0
    && typeof row.kind === "string" && typeof row.label === "string" && typeof row.detail === "string"
    && typeof row.status === "string" && Object.hasOwn(STATUS_LABEL, row.status)
    && Number.isSafeInteger(row.started_at)
    && (row.finished_at === null || Number.isSafeInteger(row.finished_at))
    && typeof row.stoppable === "boolean"
    && (row.subagent === undefined || isSubagentProgress(row.subagent));
};

/** 只在 WebSocket 边界检查后台任务快照；其他消息继续交给原协议解析器。 */
export function readJobsFrame(value: unknown): SessionJobs | null {
  if (typeof value !== "object" || value === null || !("type" in value) || value.type !== "session.jobs") return null;
  const row = value as Record<string, unknown>;
  if (typeof row.session_id !== "string" || !Array.isArray(row.jobs) || !row.jobs.every(isJob)) {
    throw new Error("后台任务快照无效");
  }
  return { sessionId: row.session_id, jobs: row.jobs };
}

export function readJobStopResult(value: unknown): { requestId: string; accepted: boolean } | null {
  if (typeof value !== "object" || value === null || !("type" in value) || value.type !== "job.stop.result") return null;
  const frame = value as Record<string, unknown>;
  if (typeof frame.request_id !== "string" || typeof frame.accepted !== "boolean") return null;
  return { requestId: frame.request_id, accepted: frame.accepted };
}

function formatDuration(elapsedMs: number): string {
  const total = Math.max(0, Math.floor(elapsedMs / 1000));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor(total / 60) % 60;
  if (hours > 0) return `${hours} 小时 ${minutes} 分`;
  if (minutes > 0) return `${minutes} 分 ${total % 60} 秒`;
  return `${total} 秒`;
}

const isLive = (job: JobView) => job.status === "running" || job.status === "stopping";

// 运行中按开始时间升序，已结束按结束时间倒序。
function ordered(jobs: readonly JobView[]): JobView[] {
  return [...jobs].sort((left, right) => {
    const live = isLive(left);
    if (live !== isLive(right)) return live ? -1 : 1;
    if (live) return left.started_at - right.started_at;
    return (right.finished_at ?? right.started_at) - (left.finished_at ?? left.started_at);
  });
}

type StopPhase = "idle" | "armed" | "pending" | "failed";

// 两击停止：首击武装、3 秒内再点确认；受理后保持等待，直到 session.jobs 帧把行改成"正在停止"而移除按钮。
function StopButton({ job, onStop }: { job: JobView; onStop: (jobId: string) => Promise<boolean> }) {
  const [phase, setPhase] = useState<StopPhase>("idle");
  useEffect(() => {
    if (phase !== "armed" && phase !== "failed") return;
    const timer = window.setTimeout(() => setPhase("idle"), phase === "armed" ? 3000 : 4000);
    return () => window.clearTimeout(timer);
  }, [phase]);
  const label = phase === "armed" ? "再点一次确认停止" : phase === "failed" ? "停止失败，任务可能已结束" : `停止任务 ${job.label}`;
  return <button type="button" className="job-list__stop" data-phase={phase} disabled={phase === "pending"}
    aria-label={label} title={label}
    onClick={() => {
      if (phase !== "armed") {
        setPhase("armed");
        return;
      }
      setPhase("pending");
      void onStop(job.id).then((accepted) => { if (!accepted) setPhase("failed"); });
    }}>
    <Square size={10} aria-hidden="true" />
    {phase === "armed" ? <span>确认停止</span> : null}
  </button>;
}

const actionTime = new Intl.DateTimeFormat(undefined, {
  hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
});

function JobRow({ job, now, onStop, expanded, onToggle, openReport }: {
  job: JobView;
  now: number;
  onStop: (jobId: string) => Promise<boolean>;
  expanded: boolean;
  onToggle: () => void;
  openReport?: () => void;
}) {
  const detailId = useId();
  const running = isLive(job);
  const duration = formatDuration((running ? now : job.finished_at ?? job.started_at) - job.started_at);
  // 正常完成只说状态；失败、超时、停止时附上 detail 便于判断原因。
  const note = job.status === "completed" || !job.detail ? STATUS_LABEL[job.status] : `${STATUS_LABEL[job.status]} · ${job.detail}`;
  const stoppable = job.status === "running" && job.stoppable;
  const progress = job.subagent;
  const summary = progress
    ? [STATUS_LABEL[job.status], `${progress.steps} 步`, progress.report === "failed" ? REPORT_LABEL.failed : null,
      job.detail || progress.actions[0]?.label].filter(Boolean).join(" · ")
    : note;
  const report = progress && (running && progress.report === "waiting" ? "执行结束后回报" : REPORT_LABEL[progress.report]);
  return <li className="job-list__row" data-status={job.status} data-stoppable={stoppable || undefined}>
    <span className="job-list__dot" data-status={job.status} aria-hidden="true" />
    {progress
      ? <button type="button" className="job-list__expand" aria-expanded={expanded} aria-controls={expanded ? detailId : undefined}
          onClick={onToggle}><span className="job-list__label" title={job.label}>{job.label}</span><ChevronDown size={12} aria-hidden="true" /></button>
      : <span className="job-list__label" title={job.label}>{job.label}</span>}
    <span className="job-list__duration" title={running ? `已运行 ${duration}` : `耗时 ${duration}`}>{duration}</span>
    <span className="job-list__meta" title={summary}>{job.kind} · {summary}</span>
    {stoppable ? <StopButton job={job} onStop={onStop} /> : null}
    {progress && expanded ? <div id={detailId} className="job-list__subagent">
      <dl>
        <div><dt>执行状态</dt><dd>{STATUS_LABEL[job.status]}</dd></div>
        <div><dt>主会话回报</dt><dd>{report}</dd></div>
      </dl>
      {job.detail ? <p className="job-list__reason">{job.detail}</p> : null}
      {progress.actions.length ? <>
        <p>最近动作</p>
        <ol>{progress.actions.map((action, index) => <li key={`${action.at}:${index}`}>
          <time dateTime={new Date(action.at).toISOString()}>{actionTime.format(action.at)}</time><span>{action.label}</span>
        </li>)}</ol>
      </> : null}
      {openReport ? <button type="button" className="job-list__report" onClick={openReport}>查看主会话回报</button> : null}
    </div> : null}
  </li>;
}

function subagentNeedsAttention(job: JobView): boolean {
  return job.subagent !== undefined && (job.status === "failed" || job.subagent.report === "failed");
}

// 面板分"进行中 / 已结束"两组：有任务运行时已结束组默认折叠；清空只隐藏本页已有的结束行，之后新结束的任务照常出现。
function JobPanel({ id, live, settled, now, settledOpen, onToggleSettled, onClear, onStop, reportAction }: {
  id: string;
  onStop: (jobId: string) => Promise<boolean>;
  live: readonly JobView[];
  settled: readonly JobView[];
  now: number;
  settledOpen: boolean;
  onToggleSettled: () => void;
  onClear: () => void;
  reportAction: (job: JobView) => (() => void) | undefined;
}) {
  const [selection, setSelection] = useState<string | null>();
  const selected = selection === undefined
    ? (settledOpen ? settled.find(subagentNeedsAttention)?.id : undefined)
      ?? live.find((job) => job.subagent)?.id
      ?? (settledOpen ? settled.find((job) => job.subagent)?.id : undefined)
    : selection;
  const renderRow = (job: JobView) => <JobRow key={job.id} job={job} now={now} onStop={onStop}
    expanded={job.id === selected} onToggle={() => setSelection(job.id === selected ? null : job.id)}
    openReport={reportAction(job)} />;
  return <div id={id} className="job-list__panel" role="group" aria-label="后台任务" data-escape-layer="">
    {live.length ? <>
      <p className="job-list__section" aria-hidden="true">进行中</p>
      <ul>{live.map(renderRow)}</ul>
    </> : null}
    {settled.length ? <>
      <div className="job-list__section">
        <button type="button" className="job-list__fold" aria-expanded={settledOpen} onClick={onToggleSettled}>
          <ChevronDown size={12} aria-hidden="true" />已结束 {settled.length}
        </button>
        <button type="button" className="job-list__clear" onClick={onClear}>清空</button>
      </div>
      {settledOpen ? <ul>{settled.map(renderRow)}</ul> : null}
    </> : null}
  </div>;
}

// 标题行右侧的安静标记：有任务运行时显示呼吸点和数量，只剩已结束任务时退为淡色计数。
export function JobListAction({ jobs, onStop, loadedMessages, onOpenReport }: {
  jobs: readonly JobView[];
  onStop: (jobId: string) => Promise<boolean>;
  loadedMessages: ReadonlySet<string>;
  onOpenReport: (messageId: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const [settledOpen, setSettledOpen] = useState<boolean | undefined>(undefined);
  const [cleared, setCleared] = useState<ReadonlySet<string>>(() => new Set());
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelId = useId();
  const rows = useMemo(() => ordered(jobs), [jobs]);
  const live = useMemo(() => rows.filter(isLive), [rows]);
  const settled = useMemo(() => rows.filter((job) => !isLive(job) && !cleared.has(job.id)), [rows, cleared]);
  // 1. 点击面板外或按 Esc 收起，Esc 把焦点还给触发按钮。
  useEffect(() => {
    if (!open) return;
    const onPointer = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setOpen(false);
      triggerRef.current?.focus();
    };
    document.addEventListener("pointerdown", onPointer);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onPointer);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);
  // 2. 时钟只在面板展开且有运行中任务时走动，收起后不再触发重渲染。
  useEffect(() => {
    if (!open || live.length === 0) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [open, live.length]);
  // 3. 没有可见任务时不占位置，并在渲染期收起面板，免得下一个任务出现时面板自己弹开。
  const visible = live.length + settled.length;
  if (visible === 0) {
    if (open) setOpen(false);
    return null;
  }
  // 4. 没有运行中任务时，圆点沿用最近结束任务的状态：失败、超时、停止在收起时也能被看见。
  const latest = live.length ? "running" : settled[0].status;
  const summary = live.length ? `${live.length} 个任务运行中` : `${visible} 个后台任务`;
  const outcome = latest === "running" || latest === "completed" ? "" : `，最近一个${STATUS_LABEL[latest]}`;
  const showSettled = settledOpen ?? (live.length === 0 || settled.some(subagentNeedsAttention));
  return <div className="job-list" ref={rootRef}>
    <button ref={triggerRef} type="button" className="job-list__trigger" data-live={live.length > 0 || undefined}
      aria-expanded={open} aria-controls={open ? panelId : undefined} aria-label={`后台任务：${summary}${outcome}`}
      onClick={() => { setNow(Date.now()); setOpen((value) => !value); }}>
      <span className="job-list__dot" data-status={latest} aria-hidden="true" />
      <span className="job-list__summary">{summary}</span>
      <span className="job-list__count" aria-hidden="true">{live.length || visible}</span>
    </button>
    {open ? <JobPanel id={panelId} live={live} settled={settled} now={now} onStop={onStop}
      settledOpen={showSettled}
      reportAction={(job) => {
        const messageId = job.subagent?.report_message_id;
        if (job.subagent?.report !== "delivered" || !messageId || !loadedMessages.has(messageId)) return undefined;
        return () => {
          setOpen(false);
          onOpenReport(messageId);
        };
      }}
      onToggleSettled={() => setSettledOpen(!showSettled)}
      onClear={() => {
        setCleared((current) => new Set([...current, ...settled.map((job) => job.id)]));
        if (live.length) triggerRef.current?.focus();
      }} /> : null}
  </div>;
}
