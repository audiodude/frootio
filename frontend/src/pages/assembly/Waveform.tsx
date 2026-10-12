import { useEffect, useRef } from "react";
import { useQuery } from "@tanstack/react-query";
import type { Peaks } from "../../types/assembly";

interface Props {
  peaksUrl: string | null;
  duration: number;
  trimIn: number;
  trimOut: number;
  currentTime: number;
  onSeek: (time: number) => void;
}

const HEIGHT = 56;

/** Vocals-stem waveform with the trimmed-away parts dimmed and a playhead. Click to seek. */
export default function Waveform({ peaksUrl, duration, trimIn, trimOut, currentTime, onSeek }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const peaks = useQuery({
    queryKey: ["assembly", "peaks", peaksUrl],
    queryFn: async () => {
      const res = await fetch(peaksUrl as string);
      if (!res.ok) throw new Error(`peaks: ${res.status}`);
      return (await res.json()) as Peaks;
    },
    enabled: peaksUrl !== null,
    staleTime: Infinity,
  });

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const width = canvas.clientWidth || 300;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(HEIGHT * dpr);
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, HEIGHT);
    const total = duration > 0 ? duration : (peaks.data?.duration ?? 0);
    if (total <= 0) return;
    const x = (t: number) => (t / total) * width;
    const data = peaks.data;
    if (data) {
      const mid = HEIGHT / 2;
      for (let px = 0; px < width; px++) {
        const t0 = (px / width) * total;
        const t1 = ((px + 1) / width) * total;
        const i0 = Math.floor(t0 * data.per_second);
        const i1 = Math.max(i0 + 1, Math.ceil(t1 * data.per_second));
        let peak = 0;
        for (let i = i0; i < i1 && i < data.peaks.length; i++) peak = Math.max(peak, data.peaks[i] ?? 0);
        const inside = t1 > trimIn && t0 < trimOut;
        ctx.fillStyle = inside ? "#ffd23f" : "#5a5d66";
        const h = Math.max(1, peak * (HEIGHT - 4));
        ctx.fillRect(px, mid - h / 2, 1, h);
      }
    }
    ctx.fillStyle = "rgba(0,0,0,0.55)";
    ctx.fillRect(0, 0, x(trimIn), HEIGHT);
    ctx.fillRect(x(trimOut), 0, width - x(trimOut), HEIGHT);
    ctx.fillStyle = "#e6e6e6";
    ctx.fillRect(x(trimIn), 0, 2, HEIGHT);
    ctx.fillRect(x(trimOut) - 2, 0, 2, HEIGHT);
    ctx.fillStyle = "#ff8a5c";
    ctx.fillRect(x(currentTime), 0, 1, HEIGHT);
  }, [peaks.data, duration, trimIn, trimOut, currentTime]);

  return (
    <div className="waveform">
      <canvas
        ref={canvasRef}
        style={{ width: "100%", height: HEIGHT }}
        onClick={(e) => {
          const rect = e.currentTarget.getBoundingClientRect();
          if (duration > 0) onSeek(((e.clientX - rect.left) / rect.width) * duration);
        }}
      />
      {peaksUrl === null && <div className="muted small">no waveform yet</div>}
      {peaks.isError && <div className="warn small">waveform failed to load</div>}
    </div>
  );
}
