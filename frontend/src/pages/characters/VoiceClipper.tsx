// Voice clipper: waveform (server-computed peaks) + drag to select a 2–5s range,
// audition the selection, then save it as the character's voice sample.

import { useEffect, useRef, useState, type PointerEvent } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, mediaUrl } from "../../api";
import type { Peaks } from "../../types/library";
import { ErrorLine } from "./LibraryWidgets";

const MIN_S = 2;
const MAX_S = 5;
const HEIGHT = 96;

export interface ClipRange {
  start: number;
  end: number;
}

export default function VoiceClipper({
  path,
  title,
  saving,
  error,
  onSave,
  onCancel,
}: {
  path: string;
  title: string;
  saving: boolean;
  error: unknown;
  onSave: (range: ClipRange) => void;
  onCancel: () => void;
}) {
  const peaks = useQuery({
    queryKey: ["voice-peaks", path],
    queryFn: () => api.get<Peaks>(`/characters/voice/peaks?path=${encodeURIComponent(path)}`),
    staleTime: Infinity,
  });
  const duration = peaks.data?.duration_s ?? 0;
  const [range, setRange] = useState<ClipRange>({ start: 0, end: 0 });
  const [playhead, setPlayhead] = useState<number | null>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const audio = useRef<HTMLAudioElement>(null);
  const drag = useRef<{ anchor: number } | null>(null);
  const raf = useRef<number>(0);

  // Default selection: the first 4s (or the whole clip if shorter).
  useEffect(() => {
    if (duration > 0) setRange({ start: 0, end: Math.min(duration, 4) });
  }, [duration]);

  useEffect(() => {
    const el = canvas.current;
    const data = peaks.data;
    if (!el || !data) return;
    const width = el.clientWidth;
    const dpr = window.devicePixelRatio || 1;
    el.width = width * dpr;
    el.height = HEIGHT * dpr;
    const ctx = el.getContext("2d");
    if (!ctx) return;
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, width, HEIGHT);
    const x = (t: number) => (t / data.duration_s) * width;
    ctx.fillStyle = "rgba(255, 210, 63, 0.18)";
    ctx.fillRect(x(range.start), 0, x(range.end) - x(range.start), HEIGHT);
    const n = data.peaks.length;
    for (let i = 0; i < n; i++) {
      const t = ((i + 0.5) / n) * data.duration_s;
      const h = Math.max(1, (data.peaks[i] ?? 0) * (HEIGHT - 4));
      ctx.fillStyle = t >= range.start && t <= range.end ? "#ffd23f" : "#5b6070";
      ctx.fillRect((i / n) * width, (HEIGHT - h) / 2, Math.max(1, width / n - 0.5), h);
    }
    if (playhead !== null) {
      ctx.fillStyle = "#fff";
      ctx.fillRect(x(playhead), 0, 1.5, HEIGHT);
    }
  }, [peaks.data, range, playhead]);

  useEffect(() => () => cancelAnimationFrame(raf.current), []);

  const timeAt = (e: PointerEvent<HTMLCanvasElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    return Math.min(duration, Math.max(0, ((e.clientX - rect.left) / rect.width) * duration));
  };
  const onDown = (e: PointerEvent<HTMLCanvasElement>) => {
    e.currentTarget.setPointerCapture(e.pointerId);
    const t = timeAt(e);
    drag.current = { anchor: t };
    setRange({ start: t, end: t });
  };
  const onMove = (e: PointerEvent<HTMLCanvasElement>) => {
    if (!drag.current) return;
    const t = timeAt(e);
    const a = drag.current.anchor;
    setRange({ start: Math.min(a, t), end: Math.max(a, t) });
  };

  const stop = () => {
    cancelAnimationFrame(raf.current);
    audio.current?.pause();
    setPlayhead(null);
  };
  const audition = () => {
    const el = audio.current;
    if (!el) return;
    stop();
    el.currentTime = range.start;
    void el.play();
    const tick = () => {
      if (el.currentTime >= range.end || el.paused) {
        stop();
        return;
      }
      setPlayhead(el.currentTime);
      raf.current = requestAnimationFrame(tick);
    };
    raf.current = requestAnimationFrame(tick);
  };

  const length = range.end - range.start;
  const valid = length >= MIN_S - 0.005 && length <= MAX_S + 0.005;

  return (
    <div className="panel clipper">
      <div className="row spread">
        <strong>{title}</strong>
        <span className="muted small">
          {duration ? `${duration.toFixed(2)}s source` : "loading…"} · pick {MIN_S}–{MAX_S}s
        </span>
      </div>
      <ErrorLine error={peaks.error} />
      <canvas
        ref={canvas}
        className="waveform"
        style={{ height: HEIGHT }}
        onPointerDown={onDown}
        onPointerMove={onMove}
        onPointerUp={() => (drag.current = null)}
      />
      <audio ref={audio} src={mediaUrl(path)} preload="auto" />
      <div className="row">
        <label>
          Start{" "}
          <input
            type="number"
            className="narrow"
            step={0.05}
            min={0}
            value={range.start.toFixed(2)}
            onChange={(e) => setRange({ ...range, start: Math.min(duration, Math.max(0, Number(e.target.value))) })}
          />
        </label>
        <label>
          End{" "}
          <input
            type="number"
            className="narrow"
            step={0.05}
            min={0}
            value={range.end.toFixed(2)}
            onChange={(e) => setRange({ ...range, end: Math.min(duration, Math.max(0, Number(e.target.value))) })}
          />
        </label>
        <span className={valid ? "ok" : "warn"}>{length.toFixed(2)}s</span>
        <button onClick={audition} disabled={length <= 0}>
          ▶ Audition
        </button>
        <button onClick={stop}>■</button>
        <button className="primary" disabled={!valid || saving} onClick={() => onSave(range)}>
          {saving ? "Saving…" : "Save as voice sample"}
        </button>
        <button onClick={onCancel}>Cancel</button>
      </div>
      {!valid && length > 0 && (
        <p className="warn small">
          Selection must be {MIN_S}–{MAX_S}s.
        </p>
      )}
      <ErrorLine error={error} />
    </div>
  );
}
