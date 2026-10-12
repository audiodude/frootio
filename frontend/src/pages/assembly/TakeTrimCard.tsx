import { useEffect, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "../../api";
import type { AssemblyItem, CaptionWord } from "../../types/assembly";
import OverdubPanel from "./OverdubPanel";
import Waveform from "./Waveform";

const fmt = (t: number) => t.toFixed(2);

export default function TakeTrimCard({ item, episodeId }: { item: AssemblyItem; episodeId: number }) {
  const take = item.take;
  const queryClient = useQueryClient();
  const videoRef = useRef<HTMLVideoElement>(null);
  const [currentTime, setCurrentTime] = useState(0);
  const [playTrimmed, setPlayTrimmed] = useState(false);
  const duration = take?.duration_s ?? 0;
  const serverIn = take?.trim_in_s ?? 0;
  const serverOut = take?.trim_out_s ?? duration;
  const [trimIn, setTrimIn] = useState(serverIn);
  const [trimOut, setTrimOut] = useState(serverOut);

  useEffect(() => {
    setTrimIn(serverIn);
    setTrimOut(serverOut);
  }, [take?.id, serverIn, serverOut]);

  const save = useMutation({
    mutationFn: (body: { trim_in_s: number; trim_out_s: number | null }) =>
      api.patch(`/takes/${take?.id}/trim`, body),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["assembly", episodeId] }),
  });
  const setMuted = useMutation({
    mutationFn: (muted: boolean) => api.patch(`/takes/${take?.id}/audio`, { muted }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["assembly", episodeId] }),
  });

  if (!take) {
    return (
      <article className="asm-card missing">
        <header>
          <strong>Snippet {item.index}</strong>
          <span className="warn">no selected take</span>
        </header>
        <p className="muted small">Pick a take for this snippet in the episode workspace.</p>
      </article>
    );
  }

  const dirty = Math.abs(trimIn - serverIn) > 1e-3 || Math.abs(trimOut - serverOut) > 1e-3;
  const valid = trimOut - trimIn >= 0.2 && trimIn >= 0 && trimOut <= duration + 1e-3;
  const seek = (t: number) => {
    const v = videoRef.current;
    if (v) v.currentTime = Math.min(Math.max(0, t), duration);
  };

  return (
    <article className="asm-card">
      <header>
        <strong>Snippet {item.index}</strong>
        <span className="muted small">
          take #{take.id}
          {take.seed !== null && ` · seed ${take.seed}`} · {fmt(duration)}s → {fmt(trimOut - trimIn)}s
        </span>
      </header>
      {item.problems.length > 0 && <div className="warn small">{item.problems.join(" · ")}</div>}
      <div className="asm-card-body">
        {take.video_url && (
          <video
            ref={videoRef}
            src={take.video_url}
            poster={take.poster_url ?? undefined}
            controls
            muted={take.audio_muted}
            preload="metadata"
            onTimeUpdate={(e) => {
              const t = e.currentTarget.currentTime;
              setCurrentTime(t);
              if (playTrimmed && t >= trimOut) {
                e.currentTarget.pause();
                setPlayTrimmed(false);
              }
            }}
          />
        )}
        <div className="asm-card-controls">
          <Waveform
            peaksUrl={take.peaks_url}
            duration={duration}
            trimIn={trimIn}
            trimOut={trimOut}
            currentTime={currentTime}
            onSeek={seek}
          />
          <div className="trim-row">
            <label>
              In
              <input
                type="number"
                step={0.01}
                min={0}
                max={duration}
                value={trimIn}
                onChange={(e) => setTrimIn(Number(e.target.value))}
              />
            </label>
            <button onClick={() => setTrimIn(Number(currentTime.toFixed(3)))}>Set in ⟵ playhead</button>
            <label>
              Out
              <input
                type="number"
                step={0.01}
                min={0}
                max={duration}
                value={trimOut}
                onChange={(e) => setTrimOut(Number(e.target.value))}
              />
            </label>
            <button onClick={() => setTrimOut(Number(currentTime.toFixed(3)))}>Set out ⟵ playhead</button>
          </div>
          <div className="trim-row">
            <label title="Drops this take's voice and fx from the preview and export; music still plays">
              <input
                type="checkbox"
                checked={take.audio_muted}
                disabled={setMuted.isPending}
                onChange={(e) => setMuted.mutate(e.target.checked)}
              />
              Strip all audio (voice + fx)
            </label>
            {setMuted.error && (
              <span className="warn small">
                {setMuted.error instanceof ApiError ? setMuted.error.detail : String(setMuted.error)}
              </span>
            )}
          </div>
          <div className="trim-row">
            <button
              onClick={() => {
                seek(trimIn);
                setPlayTrimmed(true);
                void videoRef.current?.play();
              }}
            >
              ▶ Play trimmed
            </button>
            <button
              className="primary"
              disabled={!dirty || !valid || save.isPending}
              onClick={() =>
                save.mutate({
                  trim_in_s: trimIn,
                  trim_out_s: Math.abs(trimOut - duration) < 1e-3 ? null : trimOut,
                })
              }
            >
              {save.isPending ? "Saving…" : "Save trim"}
            </button>
            <button disabled={!dirty} onClick={() => (setTrimIn(serverIn), setTrimOut(serverOut))}>
              Revert
            </button>
            <button
              disabled={serverIn === 0 && !take.trim_out_set}
              onClick={() => save.mutate({ trim_in_s: 0, trim_out_s: null })}
            >
              Clear trim
            </button>
            {!valid && <span className="warn small">trim must keep ≥ 0.2s inside the video</span>}
            {save.error && (
              <span className="warn small">
                {save.error instanceof ApiError ? save.error.detail : String(save.error)}
              </span>
            )}
          </div>
          <OverdubPanel take={take} episodeId={episodeId} videoRef={videoRef} currentTime={currentTime} />
          <CaptionPreview item={item} trimIn={trimIn} trimOut={trimOut} onSeek={seek} currentTime={currentTime} />
        </div>
      </div>
    </article>
  );
}

function CaptionPreview({
  item,
  trimIn,
  trimOut,
  currentTime,
  onSeek,
}: {
  item: AssemblyItem;
  trimIn: number;
  trimOut: number;
  currentTime: number;
  onSeek: (t: number) => void;
}) {
  const c = item.captions;
  const inside = (w: CaptionWord) => (w.end > w.start ? w.end > trimIn && w.start < trimOut : w.start >= trimIn && w.start < trimOut);
  return (
    <div className="captions-preview">
      <div className="small">
        Captions:{" "}
        {c.status === "ready" && (
          <span className={c.method === "aligned" ? "ok" : "warn"}>
            {c.method === "aligned" ? "aligned" : c.method === "mixed" ? "partly spread (fallback)" : "spread (fallback)"}
          </span>
        )}
        {c.status === "pending" && <span className="muted">aligning…</span>}
        {c.status === "missing" && <span className="muted">not aligned yet</span>}
        {c.status === "failed" && <span className="warn">alignment failed: {c.error}</span>}
        {c.status === "unavailable" && <span className="muted">needs a vocals stem</span>}
        {c.lines.length === 0 && <span className="muted"> · script slice has no dialogue</span>}
      </div>
      {c.status === "ready" ? (
        <div className="word-timeline">
          {c.words.map((w, i) => (
            <button
              key={i}
              className={[
                "word",
                inside(w) ? "" : "dropped",
                currentTime >= w.start && currentTime < Math.max(w.end, w.start + 0.05) ? "active" : "",
              ].join(" ")}
              title={`${w.start.toFixed(2)}–${w.end.toFixed(2)}s${inside(w) ? "" : " (trimmed away)"}`}
              onClick={() => onSeek(w.start)}
            >
              {w.word}
              <span className="t">{w.start.toFixed(1)}</span>
            </button>
          ))}
        </div>
      ) : (
        c.lines.length > 0 && (
          <ul className="caption-lines small">
            {c.lines.map((l, i) => (
              <li key={i}>{l}</li>
            ))}
          </ul>
        )
      )}
    </div>
  );
}
