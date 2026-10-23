export const tokenFields = ["input_tokens", "output_tokens", "cached_input_tokens", "cache_write_input_tokens", "reasoning_output_tokens"] as const;
export type TokenField = typeof tokenFields[number];
type Tokens = Record<TokenField, number | null>;
export type Totals = Tokens & Record<`${TokenField}_known_calls`, number> & {calls: number};
export type Call = Tokens & {id: string; model: string; started_at: string; state: "started" | "success" | "error";
  attempt: number | null; coverage: "exact" | "partial" | "unavailable" | null; first_token_ms: number | null; duration_ms: number | null};
export type Report = {
  start: string; end: string; bucket_seconds: number; next_cursor: string | null;
  models: {model: string; calls: number}[]; trend: (Totals & {bucket: string})[]; calls: Call[];
  summary: Totals & {success: number; errors: number; unsettled: number; retries: number; attempt_known_calls: number; exact_calls: number;
    first_token_ms: number | null; first_token_known_calls: number; duration_ms: number | null;
    tokens_per_second: number | null; cache_hit_rate: number | null; cache_known_calls: number};
};

export const labels: Record<TokenField, string> = {input_tokens: "输入", output_tokens: "输出", cached_input_tokens: "缓存读取",
  cache_write_input_tokens: "缓存写入", reasoning_output_tokens: "推理"};
export const number = (value: number | null, digits = 0) => value === null ? "—" : value.toLocaleString("zh-CN", {maximumFractionDigits: digits});
const compactFormat = new Intl.NumberFormat("en", {notation: "compact", maximumFractionDigits: 1});
export const compact = (value: number | null) => value === null ? "—" : compactFormat.format(value);
export const seconds = (value: number | null) => value === null ? "—" : `${number(value / 1000, 2)} s`;
export const date = (value: string) => new Date(value).toLocaleString("zh-CN", {month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false});
export const rate = (value: number | null) => value === null ? "—" : `${number(value * 100, 1)}%`;
