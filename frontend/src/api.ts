// Typed fetch client for the frootio API, plus the SSE hook.
//
// Query-key convention: the first element names the resource, matching the
// server event types it is invalidated by ("jobs", "takes", "snippets",
// "spend", "assembly", ...), e.g. ["snippets", episodeId], ["takes", snippetId].

import { useEffect, useRef } from "react";
import { useQueryClient, type QueryKey } from "@tanstack/react-query";

export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;

  constructor(status: number, detail: string) {
    super(`${status}: ${detail}`);
    this.status = status;
    this.detail = detail;
  }
}

type Method = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";

async function request<T>(method: Method, path: string, body?: unknown): Promise<T> {
  const init: RequestInit = { method };
  if (body instanceof FormData) {
    init.body = body;
  } else if (body !== undefined) {
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(body);
  }
  const res = await fetch(`/api${path}`, init);
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const data: unknown = await res.json();
      if (data && typeof data === "object" && "detail" in data) {
        const d = data.detail;
        detail = typeof d === "string" ? d : JSON.stringify(d);
      }
    } catch {
      // non-JSON error body: keep statusText
    }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const api = {
  get: <T>(path: string) => request<T>("GET", path),
  post: <T>(path: string, body?: unknown) => request<T>("POST", path, body),
  put: <T>(path: string, body?: unknown) => request<T>("PUT", path, body),
  patch: <T>(path: string, body?: unknown) => request<T>("PATCH", path, body),
  delete: <T>(path: string) => request<T>("DELETE", path),
};

/** URL for a media-root-relative path stored in the DB. */
export function mediaUrl(relPath: string): string {
  return "/media/" + relPath.split("/").map(encodeURIComponent).join("/");
}

// --- shared response types ----------------------------------------------------

export interface Health {
  ok: boolean;
  schema_version: number;
  media_root: string;
  db_path: string;
  fal_configured: boolean;
  openrouter_configured: boolean;
  openrouter_model: string;
  jobs: Record<string, number>;
}

export type SpendKind = "video" | "image" | "tts" | "llm";
export type SpendByKind = Record<SpendKind, number>;

export interface EpisodeSpend {
  id: number;
  series_id: number;
  number: number;
  title: string;
  spend_cap_usd: number;
  total_usd: number;
  video_usd: number;
  by_kind: SpendByKind;
}

export interface SeriesSpend {
  id: number;
  name: string;
  slug: string;
  total_usd: number;
  by_kind: SpendByKind;
  episodes: EpisodeSpend[];
}

export interface SpendTotals {
  total_usd: number;
  by_kind: SpendByKind;
  unattributed_usd: number;
  series: SeriesSpend[];
}

export function formatUsd(amount: number): string {
  return `$${amount.toFixed(2)}`;
}

// --- server events (SSE) ------------------------------------------------------

export type ServerEventType = "job" | "take" | "snippet" | "spend" | "assembly" | "library";

export interface ServerEvent {
  type: ServerEventType | (string & {});
  [key: string]: unknown;
}

/** Query-key prefixes invalidated by each event type. */
const EVENT_INVALIDATIONS: Record<string, QueryKey[]> = {
  job: [["jobs"], ["health"]],
  take: [["takes"], ["snippets"]],
  snippet: [["snippets"]],
  spend: [["spend"]],
  assembly: [["assembly"]],
  // a character/location gained generated images or a voice sample
  library: [["characters"], ["locations"], ["series"]],
};

type Listener = (event: ServerEvent) => void;
const listeners = new Set<Listener>();

/**
 * Open the single `/api/events` stream (call once, at the app root). Each
 * event invalidates its query keys and is passed to `useServerEvent` listeners.
 * After a reconnect every query is invalidated, since events may have been missed.
 */
export function useEvents(): void {
  const queryClient = useQueryClient();
  useEffect(() => {
    const source = new EventSource("/api/events");
    let opened = false;
    source.onopen = () => {
      if (opened) void queryClient.invalidateQueries();
      opened = true;
    };
    source.onmessage = (message: MessageEvent<string>) => {
      let event: ServerEvent;
      try {
        event = JSON.parse(message.data) as ServerEvent;
      } catch {
        return;
      }
      for (const queryKey of EVENT_INVALIDATIONS[event.type] ?? []) {
        void queryClient.invalidateQueries({ queryKey });
      }
      for (const listener of listeners) listener(event);
    };
    return () => source.close();
  }, [queryClient]);
}

/** Subscribe a component to raw server events (e.g. live job progress). */
export function useServerEvent(handler: Listener): void {
  const ref = useRef(handler);
  ref.current = handler;
  useEffect(() => {
    const listener: Listener = (event) => ref.current(event);
    listeners.add(listener);
    return () => {
      listeners.delete(listener);
    };
  }, []);
}
