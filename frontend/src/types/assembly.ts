// Assembly API types (backend/frootio/routers/assembly.py).

export interface AssemblySettings {
  music_track_id: number | null;
  music_offset_s: number;
  music_volume: number;
  music_duck_db: number;
  fx_volume: number;
  captions_on: boolean;
}

export interface MusicTrack {
  id: number;
  name: string;
  path: string;
  url: string;
  duration_s: number | null;
  archived: boolean;
}

export interface CaptionWord {
  word: string;
  start: number;
  end: number;
  line: number;
}

export type CaptionStatus = "ready" | "pending" | "missing" | "failed" | "unavailable";

export interface CaptionState {
  lines: string[];
  status: CaptionStatus;
  method: "aligned" | "fallback" | "mixed" | null;
  words: CaptionWord[];
  error: string | null;
  job_id: number | null;
}

export interface AssemblyTake {
  id: number;
  status: string;
  video_url: string | null;
  vocals_url: string | null;
  poster_url: string | null;
  peaks_url: string | null;
  duration_s: number | null;
  trim_in_s: number;
  trim_out_s: number | null;
  trim_out_set: boolean;
  seed: number | null;
  audio_muted: boolean;
  overdubs: Overdub[];
}

export interface Overdub {
  id: number;
  take_id: number;
  character_id: number;
  character_name: string | null;
  text: string;
  exaggeration: number;
  offset_s: number;
  volume: number;
  replace_voice: boolean;
  status: "queued" | "done" | "failed";
  error: string | null;
  audio_url: string | null;
  duration_s: number | null;
  cost_usd: number | null;
  estimated_cost_usd: number;
}

export interface AssemblyItem {
  snippet_id: number;
  index: number;
  script_slice: string;
  take: AssemblyTake | null;
  problems: string[];
  captions: CaptionState;
}

export interface PreviewState {
  path: string | null;
  url: string | null;
  rendered_at: string | null;
  fresh: boolean;
  job_id: number | null;
}

export interface ExportRecord {
  id: number;
  episode_id: number;
  path: string | null;
  url: string | null;
  captions_on: boolean;
  status: "queued" | "running" | "done" | "failed";
  error: string | null;
  job_id: number | null;
  settings: { duration_s?: number };
  created_at: string;
  updated_at: string;
}

export interface AssemblyState {
  episode: { id: number; number: number; title: string; series_id: number };
  settings: AssemblySettings;
  music_tracks: MusicTrack[];
  items: AssemblyItem[];
  total_duration_s: number;
  preview: PreviewState;
  exports: ExportRecord[];
}

export interface PrepareResult {
  items: { snippet_id: number; index: number; take_id: number | null; problems: string[] }[];
  missing_selected_takes: number[];
  missing_stems: number[];
}

export interface Peaks {
  per_second: number;
  duration: number;
  peaks: number[];
}

/** `assembly` server event payload. */
export interface AssemblyEvent {
  type: "assembly";
  episode_id: number;
  kind: "settings" | "prepare" | "trim" | "align" | "preview" | "export";
  status?: "queued" | "running" | "done" | "failed";
  progress?: number;
  error?: string;
  export_id?: number;
  job_id?: number;
}
