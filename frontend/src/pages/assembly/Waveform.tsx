import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import type { Peaks } from "../../types/assembly";

interface Props {
  peaksUrl: string | null;
  duration: number;
  trimIn: number;
  trimOut: number;
  currentTime: number;
  compact?: boolean;
  onSeek: (time: number) => void;
  onTrimChange?: (trimIn: number, trimOut: number) => void;
  onTrimCommit?: (trimIn: number, trimOut: number) => void;
}

const MIN_SPAN = 0.2;

/**
 * Interactive vocals-stem waveform with draggable In/Out trim handles and click-to-seek playhead.
 */
export default function Waveform({
  peaksUrl,
  duration,
  trimIn,
  trimOut,
  currentTime,
  compact = false,
  onSeek,
  onTrimChange,
  onTrimCommit,
}: Props) {
  const height = compact ? 32 : 64;
  const wrapRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [dragging, setDragging] = useState<"in" | "out" | "scrub" | null>(null);

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

  const total = duration > 0 ? duration : (peaks.data?.duration ?? 0);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const width = canvas.clientWidth || 300;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    if (total <= 0) return;
    const x = (t: number) => (t / total) * width;
    const data = peaks.data;
    if (data) {
      const mid = height / 2;
      for (let px = 0; px < width; px++) {
        const t0 = (px / width) * total;
        const t1 = ((px + 1) / width) * total;
        const i0 = Math.floor(t0 * data.per_second);
        const i1 = Math.max(i0 + 1, Math.ceil(t1 * data.per_second));
        let peak = 0;
        for (let i = i0; i < i1 && i < data.peaks.length; i++) peak = Math.max(peak, data.peaks[i] ?? 0);
        const inside = t1 > trimIn && t0 < trimOut;
        ctx.fillStyle = inside ? "#ffd23f" : "#4b4e57";
        const h = Math.max(1.5, peak * (height - 6));
        ctx.fillRect(px, mid - h / 2, 1, h);
      }
    }
    // Dim trimmed-out areas
    ctx.fillStyle = "rgba(8, 9, 12, 0.62)";
    ctx.fillRect(0, 0, x(trimIn), height);
    ctx.fillRect(x(trimOut), 0, width - x(trimOut), height);

    // Active window border
    ctx.strokeStyle = "#ffd23f";
    ctx.lineWidth = 1.5;
    ctx.strokeRect(x(trimIn), 0.75, Math.max(2, x(trimOut) - x(trimIn)), height - 1.5);

    // Playhead
    ctx.fillStyle = "#ff5c5c";
    ctx.fillRect(x(currentTime) - 1, 0, 2, height);
  }, [peaks.data, total, trimIn, trimOut, currentTime, height]);

  // Pointer drag handling for In/Out handles and playhead scrub
  const latestTrim = useRef({ trimIn, trimOut });
  latestTrim.current = { trimIn, trimOut };

  useEffect(() => {
    if (!dragging || total <= 0) return;
    const onMove = (e: PointerEvent) => {
      const rect = wrapRef.current?.getBoundingClientRect();
      if (!rect || rect.width <= 0) return;
      const ratio = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width));
      const t = Number((ratio * total).toFixed(3));
      if (dragging === "scrub") {
        onSeek(t);
      } else if (dragging === "in") {
        const nextIn = Math.min(t, Math.max(0, latestTrim.current.trimOut - MIN_SPAN));
        onTrimChange?.(nextIn, latestTrim.current.trimOut);
      } else if (dragging === "out") {
        const nextOut = Math.max(t, Math.min(total, latestTrim.current.trimIn + MIN_SPAN));
        onTrimChange?.(latestTrim.current.trimIn, nextOut);
      }
    };
    const onUp = () => {
      if (dragging === "in" || dragging === "out") {
        onTrimCommit?.(latestTrim.current.trimIn, latestTrim.current.trimOut);
      }
      setDragging(null);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    };
  }, [dragging, total, onSeek, onTrimChange, onTrimCommit]);

  const inPct = total > 0 ? Math.min(100, Math.max(0, (trimIn / total) * 100)) : 0;
  const outPct = total > 0 ? Math.min(100, Math.max(0, (trimOut / total) * 100)) : 100;

  return (
    <div
      ref={wrapRef}
      className={compact ? "waveform-wrap compact" : "waveform-wrap"}
      style={{ height }}
    >
      <canvas
        ref={canvasRef}
        style={{ width: "100%", height }}
        onPointerDown={(e) => {
          if (total <= 0) return;
          const rect = e.currentTarget.getBoundingClientRect();
          const t = ((e.clientX - rect.left) / rect.width) * total;
          onSeek(Math.min(total, Math.max(0, t)));
          setDragging("scrub");
        }}
      />
      {!compact && total > 0 && onTrimChange && (
        <>
          <div
            className="trim-handle trim-handle-in"
            style={{ left: `${inPct}%` }}
            title={`Drag to trim IN (${trimIn.toFixed(2)}s)`}
            onPointerDown={(e) => {
              e.stopPropagation();
              setDragging("in");
            }}
          >
            <span className="trim-tag">IN {trimIn.toFixed(2)}s</span>
          </div>
          <div
            className="trim-handle trim-handle-out"
            style={{ left: `${outPct}%` }}
            title={`Drag to trim OUT (${trimOut.toFixed(2)}s)`}
            onPointerDown={(e) => {
              e.stopPropagation();
              setDragging("out");
            }}
          >
            <span className="trim-tag">OUT {trimOut.toFixed(2)}s</span>
          </div>
        </>
      )}
      {peaksUrl === null && <div className="waveform-msg muted small">preparing waveform…</div>}
      {peaks.isError && <div className="waveform-msg warn small">waveform failed to load</div>}
    </div>
  );
}
