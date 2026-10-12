// Types for the library API (series, characters, locations, music, import).

import type { SeriesSpend } from "../api";

export interface SeriesRef {
  id: number;
  name: string;
  slug: string;
}

export type ImageSource = "upload" | "generated" | "import";

export interface LibraryImage {
  id: number;
  path: string;
  sha256: string;
  width: number | null;
  height: number | null;
  source: ImageSource;
  gen_prompt: string | null;
  gen_model: string | null;
  gen_input_paths: string[];
  gen_request_id: string | null;
  hidden: number;
  created_at: string;
  ref_order?: number | null;
}

export type VoiceSource = "upload" | "take_clip" | "chatterbox" | "import";

export interface VoiceSample {
  id: number;
  character_id: number;
  path: string;
  sha256: string;
  duration_s: number;
  source: VoiceSource;
  source_take_id: number | null;
  clip_start_s: number | null;
  clip_end_s: number | null;
  tts_voice: string | null;
  tts_text: string | null;
  created_at: string;
}

export interface Character {
  id: number;
  name: string;
  slug: string;
  description: string;
  main_image_id: number | null;
  voice_id: number | null;
  archived: boolean;
  created_at: string;
  updated_at: string;
  main_image: LibraryImage | null;
  voice: VoiceSample | null;
  aliases: string[];
  series: SeriesRef[];
}

export interface CharacterDetail extends Character {
  images: LibraryImage[];
  voices: VoiceSample[];
  spend_usd: number;
}

export interface Location {
  id: number;
  name: string;
  slug: string;
  description: string;
  archived: boolean;
  created_at: string;
  updated_at: string;
  ref_images: LibraryImage[];
  series: SeriesRef[];
}

export interface LocationDetail extends Location {
  images: LibraryImage[];
  spend_usd: number;
}

export interface MusicTrack {
  id: number;
  series_id: number;
  name: string;
  path: string;
  sha256: string | null;
  duration_s: number | null;
  created_at: string;
}

export interface EpisodeSummary {
  id: number;
  series_id: number;
  number: number;
  title: string;
  default_location_id: number | null;
  default_location_name: string | null;
  take_count: number;
  spend_cap_usd: number;
  archived: boolean;
  script_versions: number;
  snippets: number;
  spend_usd: number;
}

export interface SeriesSummary {
  id: number;
  name: string;
  slug: string;
  style_preamble: string;
  archived: boolean;
  episode_count: number;
  cast_count: number;
  location_count: number;
  spend_usd: number;
}

export interface SeriesDetail extends SeriesSummary {
  episodes: EpisodeSummary[];
  cast: Character[];
  locations: Location[];
  music: MusicTrack[];
  spend: SeriesSpend;
}

export type JobStatus = "queued" | "running" | "done" | "failed" | "cancelled";

export interface GenJob {
  id: number;
  kind: "image_gen" | "tts_gen";
  status: JobStatus;
  error: string | null;
  payload: Record<string, unknown>;
  result: Record<string, unknown> | null;
  created_at: string;
}

export interface Queued {
  job_id: number;
  estimated_cost_usd: number;
}

export interface ClipSource {
  path: string;
  duration_s: number;
}

export interface VoiceIngest {
  needs_clip: boolean;
  voice_id?: number;
  source?: ClipSource;
  character: CharacterDetail;
}

export interface Peaks {
  path: string;
  duration_s: number;
  peaks: number[];
}

export interface VocalTake {
  id: number;
  snippet_id: number;
  episode_id: number;
  vocals_path: string;
  poster_path: string | null;
  video_duration_s: number | null;
  episode_number: number;
  series_name: string;
  created_at: string;
}

export interface GenerationOptions {
  voices: string[];
  usd_per_1k_chars: number;
  nano_banana_usd_per_image: number;
  aspect_ratios: string[];
  max_images: number;
  min_s: number;
  max_s: number;
}

export interface ImportStatus {
  source: string;
  source_exists: boolean;
  series_slug: string;
  imported_series_id: number | null;
}

export interface ImportSummary {
  series_id: number;
  series_slug: string;
  source: string;
  characters: {
    id: number;
    name: string;
    slug: string;
    main_image: string;
    extra_images: number;
    skipped_duplicate_poses: number;
    voice: string | null;
    aliases: string[];
  }[];
  episodes: { id: number; number: number; script_versions: string[] }[];
  music: { id: number; name: string; path: string; source: string }[];
}
