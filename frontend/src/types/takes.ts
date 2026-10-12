// Types for the generate/takes API (backend: routers/generate.py, routers/takes.py).

export type TakeStatus =
  | "queued"
  | "running"
  | "processing"
  | "done"
  | "failed"
  | "cancelled"
  | "download_failed"
  | "processing_failed";

export interface StartFrame {
  take_id: number | null;
  time_s: number | null;
  path: string;
  url: string;
}

export interface Take {
  id: number;
  snippet_id: number;
  episode_id: number;
  status: TakeStatus;
  error: string | null;
  hidden: boolean;
  selected: boolean;
  slow: boolean;
  batch_id: string | null;
  retry_of_take_id: number | null;
  seed: number | null;
  seed_requested: number | null;
  requested_duration_s: number;
  estimated_cost_usd: number;
  cost_usd: number | null;
  fal_request_id: string | null;
  created_at: string;
  submitted_at: string | null;
  completed_at: string | null;
  video_url: string | null;
  poster_url: string | null;
  vocals_url: string | null;
  fx_url: string | null;
  video_duration_s: number | null;
  width: number | null;
  height: number | null;
  trim_in_s: number;
  trim_out_s: number | null;
  start_frame: StartFrame | null;
  can_cancel: boolean;
  can_retry: boolean;
  can_select: boolean;
}

export interface TakeList {
  snippet_id: number;
  selected_take_id: number | null;
  hidden_count: number;
  takes: Take[];
}

export type PartName = "style" | "legend" | "location" | "body" | "tail";
export const PART_NAMES: PartName[] = ["style", "legend", "location", "body", "tail"];

export interface TakeRef {
  kind: "image" | "audio";
  path: string;
  sha: string;
  label: string;
  url?: string;
  tokens?: number;
}

export interface TakePrompt {
  take_id: number;
  snippet_id: number;
  parts: Record<PartName, { text: string; overridden: boolean }>;
  joined: string;
  char_count: number;
  refs: TakeRef[];
  ref_tokens: number | null;
  start_frame: StartFrame | null;
  seed: number | null;
  seed_requested: number | null;
  requested_duration_s: number;
}

export interface CapStatus {
  cap_usd: number;
  video_spend_usd: number;
  in_flight_usd: number;
  new_usd: number;
  projected_usd: number;
  exceeded: boolean;
}

export interface GenerateEstimate {
  snippet_id: number;
  n: number;
  default_n: number;
  duration_s: number;
  ref_tokens: number;
  refs: TakeRef[];
  per_take_usd: number;
  total_usd: number;
  cap: CapStatus;
  cap_error: string | null;
  blocked: string | null;
  pinned_seed: number | null;
  start_frame: { take_id: number; time_s: number | null } | null;
}
