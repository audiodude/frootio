import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, useServerEvent } from "../../api";
import type {
  AssemblyEvent,
  AssemblySettings,
  AssemblyState,
  MusicTrack,
  PrepareResult,
} from "../../types/assembly";
import TakeTrimCard from "./TakeTrimCard";
import "./assembly.css";

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

export default function AssemblyPage() {
  const episodeId = Number(useParams().id);
  const queryClient = useQueryClient();
  const key = ["assembly", episodeId];
  const state = useQuery({ queryKey: key, queryFn: () => api.get<AssemblyState>(`/episodes/${episodeId}/assembly`) });
  const [progress, setProgress] = useState<Record<string, Progress>>({});

  const prepare = useMutation({
    mutationFn: () => api.post<PrepareResult>(`/episodes/${episodeId}/assembly/prepare`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: key }),
  });
  // Waveforms and caption alignment are generated when the assembly screen opens.
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

  if (state.isPending) return <section className="page">Loading assembly…</section>;
  if (state.isError) return <section className="page warn">Failed to load: {errorText(state.error)}</section>;
  const data = state.data;
  const ready = data.items.length > 0 && data.items.every((i) => i.problems.length === 0);

  return (
    <section className="page assembly">
      <h1>
        Assembly — episode {data.episode.number}
        {data.episode.title && `: ${data.episode.title}`}
      </h1>
      <p className="muted">
        <Link to={`/episodes/${episodeId}`}>← back to workspace</Link> · {data.items.length} snippets · assembled
        length {data.total_duration_s.toFixed(2)}s
      </p>
      {prepare.data &&
        (prepare.data.missing_selected_takes.length > 0 || prepare.data.missing_stems.length > 0) && (
          <div className="warn">
            {prepare.data.missing_selected_takes.length > 0 &&
              `No selected take: snippet ${prepare.data.missing_selected_takes.join(", ")}. `}
            {prepare.data.missing_stems.length > 0 &&
              `Missing stems: snippet ${prepare.data.missing_stems.join(", ")}.`}
          </div>
        )}
      {prepare.error && <div className="warn">Prepare failed: {errorText(prepare.error)}</div>}

      <div className="asm-layout">
        <div className="asm-takes">
          {data.items.length === 0 && <p className="muted">No active snippets on the board.</p>}
          {data.items.map((item) => (
            <TakeTrimCard key={item.snippet_id} item={item} episodeId={episodeId} />
          ))}
          <button disabled={prepare.isPending} onClick={() => prepare.mutate()}>
            {prepare.isPending ? "Refreshing…" : "Refresh waveforms & captions"}
          </button>
        </div>
        <aside className="asm-side">
          <SettingsPanel episodeId={episodeId} settings={data.settings} tracks={data.music_tracks} />
          <PreviewPanel episodeId={episodeId} data={data} ready={ready} progress={progress.preview} />
          <ExportPanel episodeId={episodeId} data={data} ready={ready} progress={progress.export} />
        </aside>
      </div>
    </section>
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
  const patch = useMutation({
    mutationFn: (body: Partial<AssemblySettings>) => api.patch(`/episodes/${episodeId}/assembly/settings`, body),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["assembly", episodeId] }),
  });
  const track = tracks.find((t) => t.id === settings.music_track_id);
  return (
    <section className="asm-panel">
      <h2>Audio & captions</h2>
      <label className="field">
        <span>Music</span>
        <select
          value={settings.music_track_id ?? ""}
          onChange={(e) => patch.mutate({ music_track_id: e.target.value === "" ? null : Number(e.target.value) })}
        >
          <option value="">— none —</option>
          {tracks.map((t) => (
            <option key={t.id} value={t.id}>
              {t.name}
              {t.duration_s ? ` (${t.duration_s.toFixed(0)}s)` : ""}
              {t.archived ? " [archived]" : ""}
            </option>
          ))}
        </select>
      </label>
      {tracks.length === 0 && <p className="muted small">The series music library is empty.</p>}
      {track && <audio className="music-audition" src={track.url} controls preload="none" />}
      <NumberField
        label="Start offset"
        value={settings.music_offset_s}
        step={0.1}
        min={0}
        max={track?.duration_s ?? undefined}
        suffix="s into the track"
        onCommit={(v) => patch.mutate({ music_offset_s: v })}
      />
      <NumberField
        label="Music volume"
        value={settings.music_volume}
        step={0.05}
        min={0}
        max={2}
        suffix="× (linear)"
        onCommit={(v) => patch.mutate({ music_volume: v })}
      />
      <NumberField
        label="Duck amount"
        value={settings.music_duck_db}
        step={1}
        min={0}
        max={30}
        suffix="dB under dialogue"
        onCommit={(v) => patch.mutate({ music_duck_db: v })}
      />
      <NumberField
        label="FX volume"
        value={settings.fx_volume}
        step={0.05}
        min={0}
        max={2}
        suffix="× (all fx stems)"
        onCommit={(v) => patch.mutate({ fx_volume: v })}
      />
      <label className="field checkbox">
        <input
          type="checkbox"
          checked={settings.captions_on}
          onChange={(e) => patch.mutate({ captions_on: e.target.checked })}
        />
        <span>Burn in captions</span>
      </label>
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

function PreviewPanel({
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
    <section className="asm-panel">
      <h2>Preview</h2>
      <button className="primary" disabled={!ready || busy || render.isPending} onClick={() => render.mutate()}>
        {busy ? "Rendering…" : p.fresh ? "Preview is up to date" : "Render preview"}
      </button>
      {!ready && <p className="muted small">Every snippet needs a selected take with stems.</p>}
      <ProgressLine p={progress} />
      {render.error && <div className="warn small">{errorText(render.error)}</div>}
      {p.url && (
        <>
          <video
            key={p.rendered_at ?? ""}
            className="preview-video"
            src={`${p.url}?v=${encodeURIComponent(p.rendered_at ?? "")}`}
            controls
          />
          <div className="small muted">
            rendered {p.rendered_at} {!p.fresh && <span className="warn">· out of date</span>}
          </div>
        </>
      )}
    </section>
  );
}

function ExportPanel({
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
  const [captionsOn, setCaptionsOn] = useState(data.settings.captions_on);
  useEffect(() => setCaptionsOn(data.settings.captions_on), [data.settings.captions_on]);
  const start = useMutation({
    mutationFn: () => api.post(`/episodes/${episodeId}/assembly/export`, { captions_on: captionsOn }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["assembly", episodeId] }),
  });
  const running = data.exports.some((e) => e.status === "queued" || e.status === "running");
  return (
    <section className="asm-panel">
      <h2>Export (9:16, 768×1344)</h2>
      <label className="field checkbox">
        <input type="checkbox" checked={captionsOn} onChange={(e) => setCaptionsOn(e.target.checked)} />
        <span>Captions in this export</span>
      </label>
      <button className="primary" disabled={!ready || running || start.isPending} onClick={() => start.mutate()}>
        {running ? "Exporting…" : "Export MP4"}
      </button>
      <ProgressLine p={progress} />
      {start.error && <div className="warn small">{errorText(start.error)}</div>}
      <ul className="exports">
        {data.exports.map((e) => (
          <li key={e.id}>
            {e.url ? (
              <a href={e.url} target="_blank" rel="noreferrer">
                {e.path?.split("/").pop()}
              </a>
            ) : (
              <span>export #{e.id}</span>
            )}{" "}
            <span className={e.status === "failed" ? "warn small" : "muted small"}>
              {e.status}
              {e.settings.duration_s !== undefined && ` · ${e.settings.duration_s.toFixed(1)}s`}
              {` · captions ${e.captions_on ? "on" : "off"}`} · {e.created_at}
              {e.error && ` · ${e.error}`}
            </span>
          </li>
        ))}
        {data.exports.length === 0 && <li className="muted small">No exports yet.</li>}
      </ul>
    </section>
  );
}
