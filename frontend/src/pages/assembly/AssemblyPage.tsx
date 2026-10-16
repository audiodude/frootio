import { useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, useServerEvent } from "../../api";
import type {
  AssemblyEvent,
  AssemblyItem,
  AssemblySettings,
  AssemblyState,
  MusicTrack,
  PrepareResult,
} from "../../types/assembly";
import type { Episode } from "../../types/workspace";
import EpisodeHeader from "../episodes/EpisodeHeader";
import TakeTrimCard from "./TakeTrimCard";
import "./assembly.css";
import "../episodes/workspace.css";

interface Progress {
  status: "queued" | "running" | "done" | "failed";
  progress: number;
  error?: string;
}

function errorText(err: unknown): string {
  if (err instanceof ApiError) {
    try {
      const d = JSON.parse(err.detail) as { message?: string; problems?: string[] };
      if (d.problems) return `${d.message ?? "not ready"}: ${d.problems.join("; ")}`;
    } catch {
      // plain string detail
    }
    return err.detail;
  }
  return String(err);
}

/**
 * Redesigned Assembly & Mix Screen (NLE Master-Detail Layout):
 * - Unified Episode Header at top (with Refresh Stems + Export MP4 popover)
 * - Left Column: Single Sticky 9:16 Stage Monitor (switches between Active Clip & Full Episode Preview) + Audio/Captions Master Strip
 * - Right Column: Sequence Timeline (compact inactive clips, expanded active clip with draggable waveform trimmer & overdubs)
 */
export default function AssemblyPage() {
  const episodeId = Number(useParams().id);
  const queryClient = useQueryClient();
  const key = ["assembly", episodeId];
  const state = useQuery({
    queryKey: key,
    queryFn: () => api.get<AssemblyState>(`/episodes/${episodeId}/assembly`),
  });
  const episode = useQuery({
    queryKey: ["episode", episodeId],
    queryFn: () => api.get<Episode>(`/episodes/${episodeId}`),
  });

  const [progress, setProgress] = useState<Record<string, Progress>>({});
  const [activeSnippetId, setActiveSnippetId] = useState<number | null>(null);
  const [monitorMode, setMonitorMode] = useState<"clip" | "episode">("clip");
  const [currentTime, setCurrentTime] = useState(0);
  const [playTrimOut, setPlayTrimOut] = useState<number | null>(null);
  const [showExportModal, setShowExportModal] = useState(false);

  const clipVideoRef = useRef<HTMLVideoElement | null>(null);

  const prepare = useMutation({
    mutationFn: () => api.post<PrepareResult>(`/episodes/${episodeId}/assembly/prepare`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: key }),
  });

  const prepareMutate = prepare.mutate;
  useEffect(() => {
    prepareMutate();
  }, [episodeId, prepareMutate]);

  useServerEvent((raw) => {
    if (raw.type !== "assembly") return;
    const e = raw as unknown as AssemblyEvent;
    if (e.episode_id !== episodeId || (e.kind !== "preview" && e.kind !== "export") || !e.status) return;
    setProgress((p) => ({ ...p, [e.kind]: { status: e.status!, progress: e.progress ?? 0, error: e.error } }));
  });

  // Default to first snippet on load
  useEffect(() => {
    const first = state.data?.items[0];
    if (activeSnippetId === null && first) {
      setActiveSnippetId(first.snippet_id);
    }
  }, [activeSnippetId, state.data]);

  if (state.isPending) return <section className="page">Loading assembly…</section>;
  if (state.isError) return <section className="page warn">Failed to load: {errorText(state.error)}</section>;
  const data = state.data;
  const ready = data.items.length > 0 && data.items.every((i) => i.problems.length === 0);

  const activeItem: AssemblyItem | null =
    data.items.find((i) => i.snippet_id === activeSnippetId) ?? data.items[0] ?? null;

  const seekClip = (t: number) => {
    setMonitorMode("clip");
    const v = clipVideoRef.current;
    const dur = activeItem?.take?.duration_s ?? 0;
    const clamped = Math.min(Math.max(0, t), dur > 0 ? dur : t);
    setCurrentTime(clamped);
    if (v) v.currentTime = clamped;
  };

  const playTrimmed = (trimIn: number, trimOut: number) => {
    setMonitorMode("clip");
    setPlayTrimOut(trimOut);
    window.setTimeout(() => {
      const v = clipVideoRef.current;
      if (v) {
        v.currentTime = trimIn;
        void v.play();
      }
    }, 20);
  };

  const runningExport = data.exports.some((e) => e.status === "queued" || e.status === "running");

  return (
    <div className="ws assembly-studio">
      <EpisodeHeader
        episodeId={episodeId}
        episode={episode.data}
        seriesId={data.episode.series_id}
        episodeNumber={data.episode.number}
        episodeTitle={data.episode.title}
        mode="assembly"
        rightExtra={
          <div className="asm-header-actions">
            <button
              type="button"
              className="small-btn"
              disabled={prepare.isPending}
              onClick={() => prepare.mutate()}
              title="Re-extract waveforms and re-run caption alignment"
            >
              {prepare.isPending ? "Refreshing…" : "↻ Refresh Stems"}
            </button>
            <div className="asm-export-wrap">
              <button
                type="button"
                className="primary small-btn"
                onClick={() => setShowExportModal((v) => !v)}
              >
                {runningExport ? "◌ Exporting…" : "Export MP4 ▾"}
              </button>
              {showExportModal && (
                <ExportPopover
                  episodeId={episodeId}
                  data={data}
                  ready={ready}
                  progress={progress.export}
                  onClose={() => setShowExportModal(false)}
                />
              )}
            </div>
          </div>
        }
      />

      {prepare.data &&
        (prepare.data.missing_selected_takes.length > 0 || prepare.data.missing_stems.length > 0) && (
          <div className="asm-alert warn small">
            {prepare.data.missing_selected_takes.length > 0 &&
              `No selected take: scene ${prepare.data.missing_selected_takes.join(", ")}. `}
            {prepare.data.missing_stems.length > 0 &&
              `Missing stems: scene ${prepare.data.missing_stems.join(", ")}.`}
          </div>
        )}
      {prepare.error && <div className="asm-alert warn small">Prepare failed: {errorText(prepare.error)}</div>}

      <div className="asm-nle-grid">
        {/* LEFT PANE: Single Sticky 9:16 Stage Monitor + Master Audio Strip */}
        <aside className="asm-stage-col">
          <div className="asm-monitor-card">
            <div className="asm-monitor-switch" role="tablist">
              <button
                type="button"
                role="tab"
                aria-selected={monitorMode === "clip"}
                className={monitorMode === "clip" ? "asm-mon-tab active" : "asm-mon-tab"}
                onClick={() => setMonitorMode("clip")}
              >
                {activeItem ? `Scene #${activeItem.index} Monitor` : "Clip Monitor"}
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={monitorMode === "episode"}
                className={monitorMode === "episode" ? "asm-mon-tab active" : "asm-mon-tab"}
                onClick={() => setMonitorMode("episode")}
              >
                Full Episode ({data.total_duration_s.toFixed(1)}s)
              </button>
            </div>

            {monitorMode === "clip" ? (
              <div className="asm-monitor-body">
                {activeItem?.take?.video_url ? (
                  <>
                    <div className="asm-video-frame">
                      <video
                        key={activeItem.take.id}
                        ref={clipVideoRef}
                        src={activeItem.take.video_url}
                        poster={activeItem.take.poster_url ?? undefined}
                        controls
                        muted={activeItem.take.audio_muted}
                        preload="metadata"
                        onTimeUpdate={(e) => {
                          const t = e.currentTarget.currentTime;
                          setCurrentTime(t);
                          if (playTrimOut !== null && t >= playTrimOut) {
                            e.currentTarget.pause();
                            setPlayTrimOut(null);
                          }
                        }}
                      />
                    </div>
                    <div className="asm-transport-bar">
                      <span className="asm-timecode">{currentTime.toFixed(2)}s</span>
                      <button
                        type="button"
                        className="small-btn"
                        onClick={() => {
                          const tIn = activeItem.take?.trim_in_s ?? 0;
                          const tOut = activeItem.take?.trim_out_s ?? activeItem.take?.duration_s ?? 0;
                          playTrimmed(tIn, tOut);
                        }}
                      >
                        ▶ Play Trimmed
                      </button>
                      <span className="muted small">
                        in {(activeItem.take.trim_in_s ?? 0).toFixed(1)}s –{" "}
                        {(activeItem.take.trim_out_s ?? activeItem.take.duration_s ?? 0).toFixed(1)}s
                      </span>
                    </div>
                  </>
                ) : (
                  <div className="asm-video-frame empty">
                    <span className="muted small">Select a scene with a take to monitor</span>
                  </div>
                )}
              </div>
            ) : (
              <EpisodePreviewMonitor episodeId={episodeId} data={data} ready={ready} progress={progress.preview} />
            )}
          </div>

          <SettingsPanel episodeId={episodeId} settings={data.settings} tracks={data.music_tracks} />
        </aside>

        {/* RIGHT PANE: Sequence Timeline */}
        <section className="asm-timeline-col">
          <header className="asm-timeline-head">
            <div>
              <h2>Sequence Timeline</h2>
              <span className="muted small">
                {data.items.length} scenes · <strong>{data.total_duration_s.toFixed(2)}s</strong> assembled duration
              </span>
            </div>
            <span className={ready ? "badge ok-badge" : "badge warn-badge"}>
              {ready ? "● Ready to preview & export" : "⚠ Needs takes/stems"}
            </span>
          </header>

          <div className="asm-takes">
            {data.items.length === 0 && (
              <div className="ws-empty-board muted">No active scenes on the storyboard yet.</div>
            )}
            {data.items.map((item) => {
              const isActive = item.snippet_id === activeItem?.snippet_id;
              return (
                <TakeTrimCard
                  key={item.snippet_id}
                  item={item}
                  episodeId={episodeId}
                  active={isActive}
                  onSelect={() => {
                    setActiveSnippetId(item.snippet_id);
                    setMonitorMode("clip");
                    setCurrentTime(item.take?.trim_in_s ?? 0);
                  }}
                  currentTime={isActive ? currentTime : 0}
                  onSeek={seekClip}
                  onPlayTrimmed={playTrimmed}
                  videoRef={clipVideoRef}
                />
              );
            })}
          </div>
        </section>
      </div>
    </div>
  );
}

function NumberField(props: {
  label: string;
  value: number;
  step: number;
  min: number;
  max?: number;
  suffix?: string;
  onCommit: (v: number) => void;
}) {
  const [draft, setDraft] = useState(String(props.value));
  useEffect(() => setDraft(String(props.value)), [props.value]);
  const commit = () => {
    const v = Number(draft);
    if (draft.trim() !== "" && Number.isFinite(v) && v !== props.value) props.onCommit(v);
    else setDraft(String(props.value));
  };
  return (
    <label className="field">
      <span>{props.label}</span>
      <input
        type="number"
        step={props.step}
        min={props.min}
        max={props.max}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => e.key === "Enter" && commit()}
      />
      {props.suffix && <span className="muted small">{props.suffix}</span>}
    </label>
  );
}

function SettingsPanel({
  episodeId,
  settings,
  tracks,
}: {
  episodeId: number;
  settings: AssemblySettings;
  tracks: MusicTrack[];
}) {
  const queryClient = useQueryClient();
  const [expanded, setExpanded] = useState(false);
  const patch = useMutation({
    mutationFn: (body: Partial<AssemblySettings>) => api.patch(`/episodes/${episodeId}/assembly/settings`, body),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["assembly", episodeId] }),
  });
  const track = tracks.find((t) => t.id === settings.music_track_id);
  return (
    <section className="asm-master-strip">
      <div className="asm-strip-top">
        <label className="asm-music-pick">
          <span>🎵</span>
          <select
            value={settings.music_track_id ?? ""}
            onChange={(e) => patch.mutate({ music_track_id: e.target.value === "" ? null : Number(e.target.value) })}
          >
            <option value="">No background music</option>
            {tracks.map((t) => (
              <option key={t.id} value={t.id}>
                {t.name}
                {t.duration_s ? ` (${t.duration_s.toFixed(0)}s)` : ""}
              </option>
            ))}
          </select>
        </label>
        <label className="small checkbox">
          <input
            type="checkbox"
            checked={settings.captions_on}
            onChange={(e) => patch.mutate({ captions_on: e.target.checked })}
          />
          <span>Captions</span>
        </label>
      </div>

      <div className="asm-strip-sub">
        <span className="muted small">
          Music {settings.music_volume}× · Duck −{settings.music_duck_db}dB · FX {settings.fx_volume}×
        </span>
        <button type="button" className="link small" onClick={() => setExpanded((v) => !v)}>
          {expanded ? "Hide mix ▴" : "Edit mix ▾"}
        </button>
      </div>

      {expanded && (
        <div className="asm-mix-drawer">
          {track && <audio className="music-audition" src={track.url} controls preload="none" />}
          <NumberField
            label="Music offset"
            value={settings.music_offset_s}
            step={0.1}
            min={0}
            max={track?.duration_s ?? undefined}
            suffix="s"
            onCommit={(v) => patch.mutate({ music_offset_s: v })}
          />
          <NumberField
            label="Music vol"
            value={settings.music_volume}
            step={0.05}
            min={0}
            max={2}
            suffix="×"
            onCommit={(v) => patch.mutate({ music_volume: v })}
          />
          <NumberField
            label="Duck dialogue"
            value={settings.music_duck_db}
            step={1}
            min={0}
            max={30}
            suffix="dB"
            onCommit={(v) => patch.mutate({ music_duck_db: v })}
          />
          <NumberField
            label="FX stem vol"
            value={settings.fx_volume}
            step={0.05}
            min={0}
            max={2}
            suffix="×"
            onCommit={(v) => patch.mutate({ fx_volume: v })}
          />
        </div>
      )}
      {patch.error && <div className="warn small">{errorText(patch.error)}</div>}
    </section>
  );
}

function ProgressLine({ p }: { p?: Progress }) {
  if (!p) return null;
  if (p.status === "failed") return <div className="warn small">Failed: {p.error}</div>;
  if (p.status === "done") return null;
  return (
    <div className="progress">
      <div className="bar" style={{ width: `${Math.round(p.progress * 100)}%` }} />
      <span className="small">
        {p.status} {Math.round(p.progress * 100)}%
      </span>
    </div>
  );
}

function EpisodePreviewMonitor({
  episodeId,
  data,
  ready,
  progress,
}: {
  episodeId: number;
  data: AssemblyState;
  ready: boolean;
  progress?: Progress;
}) {
  const queryClient = useQueryClient();
  const render = useMutation({
    mutationFn: () =>
      api.post<{ reused: boolean; job_id: number | null }>(`/episodes/${episodeId}/assembly/preview`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["assembly", episodeId] }),
  });
  const p = data.preview;
  const busy = p.job_id !== null || progress?.status === "running" || progress?.status === "queued";

  return (
    <div className="asm-monitor-body">
      <div className="asm-video-frame">
        {p.url ? (
          <video
            key={p.rendered_at ?? ""}
            src={`${p.url}?v=${encodeURIComponent(p.rendered_at ?? "")}`}
            controls
          />
        ) : (
          <div className="asm-empty-preview muted small">
            No assembled preview rendered yet.
          </div>
        )}
      </div>
      <div className="asm-transport-bar">
        <button
          className="primary small-btn"
          disabled={!ready || busy || render.isPending}
          onClick={() => render.mutate()}
        >
          {busy ? "Rendering…" : p.fresh ? "✓ Preview Up to Date" : "Render Episode Preview"}
        </button>
        {p.rendered_at && !p.fresh && <span className="warn small">Out of date</span>}
      </div>
      <ProgressLine p={progress} />
      {render.error && <div className="warn small">{errorText(render.error)}</div>}
    </div>
  );
}

function ExportPopover({
  episodeId,
  data,
  ready,
  progress,
  onClose,
}: {
  episodeId: number;
  data: AssemblyState;
  ready: boolean;
  progress?: Progress;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const [captionsOn, setCaptionsOn] = useState(data.settings.captions_on);
  useEffect(() => setCaptionsOn(data.settings.captions_on), [data.settings.captions_on]);
  const start = useMutation({
    mutationFn: () => api.post(`/episodes/${episodeId}/assembly/export`, { captions_on: captionsOn }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["assembly", episodeId] }),
  });
  const running = data.exports.some((e) => e.status === "queued" || e.status === "running");

  return (
    <div className="ep-defaults-popover asm-export-popover">
      <div className="row spread">
        <strong className="small">Export Final MP4 (9:16, 768×1344)</strong>
        <button type="button" className="link small muted" onClick={onClose}>
          ✕
        </button>
      </div>
      <label className="field checkbox small">
        <input type="checkbox" checked={captionsOn} onChange={(e) => setCaptionsOn(e.target.checked)} />
        <span>Burn in captions on this export</span>
      </label>
      <button
        className="primary small-btn"
        disabled={!ready || running || start.isPending}
        onClick={() => start.mutate()}
      >
        {running ? "Exporting…" : "Render & Export MP4"}
      </button>
      {!ready && <div className="muted small">Every scene needs a selected take with stems first.</div>}
      <ProgressLine p={progress} />
      {start.error && <div className="warn small">{errorText(start.error)}</div>}

      <div className="asm-exports-title muted small">Export History ({data.exports.length})</div>
      <ul className="exports">
        {data.exports.map((e) => (
          <li key={e.id} className="export-item">
            <div className="row spread">
              {e.url ? (
                <a href={e.url} target="_blank" rel="noreferrer" className="small">
                  ⬇ {e.path?.split("/").pop()}
                </a>
              ) : (
                <span className="small">Export #{e.id}</span>
              )}
              <span className={e.status === "failed" ? "badge warn-badge" : "badge"}>
                {e.status}
              </span>
            </div>
            <div className="muted small">
              {e.settings.duration_s !== undefined && `${e.settings.duration_s.toFixed(1)}s · `}
              captions {e.captions_on ? "on" : "off"}
              {e.error && <span className="warn"> · {e.error}</span>}
            </div>
          </li>
        ))}
        {data.exports.length === 0 && <li className="muted small">No exports yet.</li>}
      </ul>
    </div>
  );
}
