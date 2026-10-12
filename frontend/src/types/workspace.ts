// Types for the episode workspace API (scripts, snippets, prompt parts, splits).

import type { EpisodeSpend } from "../api";

export interface CastMember {
  id: number;
  name: string;
  description: string;
  archived: boolean;
  image_path: string | null;
  has_voice: boolean;
}

export interface LocationOption {
  id: number;
  name: string;
  description: string;
  archived: boolean;
  ref_image_count: number;
}

export interface Episode {
  id: number;
  series_id: number;
  number: number;
  title: string;
  default_location_id: number | null;
  take_count: number;
  spend_cap_usd: number;
  series: { id: number; name: string; slug: string; style_preamble: string };
  cast: CastMember[];
  locations: LocationOption[];
  spend: EpisodeSpend;
  openrouter_model: string;
}

export interface LintWarning {
  line: number;
  kind: "noncanonical" | "unresolved";
  message: string;
}

export interface LintReport {
  warnings: LintWarning[];
  estimate_s: number;
  dialogue_s: number;
  prose_s: number;
  dialogue_lines: number;
}

export interface ScriptVersion extends LintReport {
  id: number | null;
  version: number | null;
  source: string | null;
  created_at: string | null;
  text: string;
  created?: boolean;
}

export interface ScriptVersionMeta {
  id: number;
  version: number;
  source: string;
  created_at: string;
  chars: number;
}

export interface Snippet {
  id: number;
  episode_id: number;
  position: number;
  archived: boolean;
  archived_at: string | null;
  origin: "split0" | "split2" | "manual" | "copy";
  body: string;
  tail: string;
  script_slice: string;
  location_id: number | null;
  location_name: string | null;
  target_duration_s: number;
  continue_from_previous: boolean;
  pinned_seed: number | null;
  warnings: string[];
  selected_take_id: number | null;
  cast: { id: number; name: string; archived: boolean }[];
  estimate_s: number | null;
  take_count: number;
  is_first: boolean;
  stale: boolean;
  blocked_reason: string | null;
  prompt_errors: string[];
}

export interface Board {
  active: Snippet[];
  archived: Snippet[];
  estimated_total_s: number;
  length_warning: string | null;
}

export type PartName = "style" | "legend" | "location" | "body" | "tail";
export const PART_NAMES: PartName[] = ["style", "legend", "location", "body", "tail"];
export const DERIVED_PARTS: PartName[] = ["style", "legend", "location"];

export interface PromptPart {
  text: string;
  overridden: boolean;
  source_changed: boolean;
}

export interface RefItem {
  kind: "image" | "audio";
  path: string;
  sha: string;
  label: string;
}

export interface BuiltPrompt {
  parts: Record<PartName, PromptPart>;
  joined: string;
  char_count: number;
  max_chars: number;
  refs: RefItem[];
  errors: string[];
}

export interface ProposedSnippet {
  script_slice: string;
  body: string;
  cast: { id: number; name: string }[];
  location: { id: number; name: string } | null;
  target_duration_s: number;
  estimate_s: number;
  warnings: string[];
}

export interface SplitProposal {
  id: number;
  episode_id: number;
  script_version_id: number | null;
  script_version: number | null;
  level: 0 | 2;
  model: string | null;
  result: ProposedSnippet[];
  estimated_total_s: number | null;
  warnings: string[];
  cost_usd: number;
  status: "pending" | "accepted" | "rejected" | "failed";
  error: string | null;
  job_status: string | null;
  running: boolean;
  created_at: string;
}

export interface Level0Result {
  level: 0;
  proposal_id: number;
  snippet_ids: number[];
  warnings: string[];
  script_estimate_s: number;
  estimated_total_s: number;
  length_warning: string | null;
}
