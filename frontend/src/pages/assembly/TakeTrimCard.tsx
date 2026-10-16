import { useEffect, useState, type RefObject } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "../../api";
import type { AssemblyItem, CaptionWord } from "../../types/assembly";
import OverdubPanel from "./OverdubPanel";
import Waveform from "./Waveform";

interface Props {
  item: AssemblyItem;
  episodeId: number;
  active: boolean;
  onSelect: () => void;
  currentTime: number;
  onSeek: (time: number) => void;
  onPlayTrimmed: (trimIn: number, trimOut: number) => void;
  videoRef: RefObject<HTMLVideoElement | null>;
}

/**
 * NLE Timeline Clip Card:
 * - Collapsed when inactive: header + compact waveform + script preview + alignment badge
 * - Expanded when active: interactive draggable waveform + fine trim controls + synchronized word track + overdubs
 */
export default function TakeTrimCard({
  item,
  episodeId,
  active,
  onSelect,
  currentTime,
  onSeek,
  onPlayTrimmed,
  videoRef,
}: Props) {
  const take = item.take;
  const queryClient = useQueryClient();
  const duration = take?.duration_s ?? 0;
  const serverIn = take?.trim_in_s ?? 0;
  const serverOut = take?.trim_out_s ?? duration;
  const [trimIn, setTrimIn] = useState(serverIn);
  const [trimOut, setTrimOut] = useState(serverOut);
  const [showNumericTrim, setShowNumericTrim] = useState(false);

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
        <header className="asm-card-head">
          <strong>Scene #{item.index}</strong>
          <span className="badge warn-badge">No selected take</span>
          <Link to={`/episodes/${episodeId}?snippet=${item.snippet_id}`} className="small" style={{ marginLeft: "auto" }}>
            Pick take in Storyboard →
          </Link>
        </header>
        {item.script_slice && <div className="muted small clamp">{item.script_slice}</div>}
      </article>
    );
  }

  const dirty = Math.abs(trimIn - serverIn) > 1e-3 || Math.abs(trimOut - serverOut) > 1e-3;
  const valid = trimOut - trimIn >= 0.2 && trimIn >= 0 && trimOut <= duration + 1e-3;
  const keptS = Math.max(0, trimOut - trimIn);
  const isTrimmed = serverIn > 0.005 || take.trim_out_set;

  const commitTrim = (nextIn: number, nextOut: number) => {
    if (nextOut - nextIn < 0.2) return;
    if (Math.abs(nextIn - serverIn) <= 1e-3 && Math.abs(nextOut - serverOut) <= 1e-3) return;
    save.mutate({
      trim_in_s: Number(nextIn.toFixed(3)),
      trim_out_s: Math.abs(nextOut - duration) < 1e-3 ? null : Number(nextOut.toFixed(3)),
    });
  };

  const c = item.captions;
  const captionBadge =
    c.status === "ready" ? (
      <span className={c.method === "aligned" ? "badge ok-badge" : "badge warn-badge"}>
        {c.method === "aligned" ? "✓ Aligned" : c.method === "mixed" ? "≈ Mixed" : "≈ Spread"}
      </span>
    ) : c.status === "pending" || c.status === "missing" ? (
      <span className="badge muted-badge">◌ Aligning…</span>
    ) : c.status === "unavailable" ? (
      <span className="badge muted-badge">No dialogue</span>
    ) : (
      <span className="badge warn-badge">Caption err</span>
    );

  if (!active) {
    return (
      <article className="asm-card collapsed" onClick={onSelect}>
        <header className="asm-card-head">
          <strong>Scene #{item.index}</strong>
          <span className="muted small">
            Take #{take.id} · {duration.toFixed(1)}s → <strong>{keptS.toFixed(1)}s</strong>
          </span>
          {take.audio_muted && <span className="badge muted-badge">🔇 Audio stripped</span>}
          {take.overdubs.length > 0 && <span className="badge">🎙 {take.overdubs.length} overdub</span>}
          {item.problems.length > 0 && <span className="badge warn-badge">⚠ {item.problems.length}</span>}
          <span style={{ marginLeft: "auto" }}>{captionBadge}</span>
        </header>
        <div className="asm-collapsed-row">
          <div className="asm-collapsed-wave">
            <Waveform
              peaksUrl={take.peaks_url}
              duration={duration}
              trimIn={trimIn}
              trimOut={trimOut}
              currentTime={0}
              compact
              onSeek={() => onSelect()}
            />
          </div>
          {item.script_slice && (
            <div className="asm-collapsed-slice muted small">
              “{item.script_slice.split("\n")[0]}”
            </div>
          )}
        </div>
      </article>
    );
  }

  return (
    <article className="asm-card active-card">
      <header className="asm-card-head">
        <strong>Scene #{item.index}</strong>
        <span className="muted small">
          Take #{take.id}
          {take.seed !== null && ` (seed ${take.seed})`} · {duration.toFixed(2)}s raw →{" "}
          <strong className="accent-text">{keptS.toFixed(2)}s kept</strong>
        </span>

        <div className="asm-card-actions">
          <button
            type="button"
            className="small-btn"
            onClick={() => onPlayTrimmed(trimIn, trimOut)}
            title="Play trimmed range in Stage Monitor"
          >
            ▶ Play Trimmed
          </button>
          <label
            className="small asm-strip-toggle"
            title="Drops this take's voice and fx from preview and export; music still plays"
          >
            <input
              type="checkbox"
              checked={take.audio_muted}
              disabled={setMuted.isPending}
              onChange={(e) => setMuted.mutate(e.target.checked)}
            />
            Strip take audio
          </label>
          {isTrimmed && (
            <button
              type="button"
              className="small-btn"
              onClick={() => save.mutate({ trim_in_s: 0, trim_out_s: null })}
            >
              Reset trim
            </button>
          )}
          <button
            type="button"
            className="small-btn icon-btn"
            title="Toggle exact numeric trim inputs"
            onClick={() => setShowNumericTrim((v) => !v)}
          >
            {showNumericTrim ? "Hide #s" : "Exact #s"}
          </button>
          {captionBadge}
        </div>
      </header>

      {item.problems.length > 0 && <div className="warn small">{item.problems.join(" · ")}</div>}
      {setMuted.error && (
        <div className="warn small">
          {setMuted.error instanceof ApiError ? setMuted.error.detail : String(setMuted.error)}
        </div>
      )}

      <div className="asm-card-controls">
        <Waveform
          peaksUrl={take.peaks_url}
          duration={duration}
          trimIn={trimIn}
          trimOut={trimOut}
          currentTime={currentTime}
          onSeek={onSeek}
          onTrimChange={(nextIn, nextOut) => {
            setTrimIn(nextIn);
            setTrimOut(nextOut);
          }}
          onTrimCommit={commitTrim}
        />

        {showNumericTrim && (
          <div className="trim-row numeric-trim-bar">
            <label className="small">
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
            <button
              type="button"
              className="small-btn"
              onClick={() => {
                const nextIn = Number(currentTime.toFixed(3));
                setTrimIn(nextIn);
                commitTrim(nextIn, trimOut);
              }}
            >
              Set In ⟵ playhead
            </button>
            <label className="small">
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
            <button
              type="button"
              className="small-btn"
              onClick={() => {
                const nextOut = Number(currentTime.toFixed(3));
                setTrimOut(nextOut);
                commitTrim(trimIn, nextOut);
              }}
            >
              Set Out ⟵ playhead
            </button>
            {dirty && (
              <button
                type="button"
                className="primary small-btn"
                disabled={!valid || save.isPending}
                onClick={() => commitTrim(trimIn, trimOut)}
              >
                {save.isPending ? "Saving…" : "Save trim"}
              </button>
            )}
          </div>
        )}

        {!valid && <span className="warn small">Trim must keep ≥ 0.2s inside the video</span>}
        {save.error && (
          <span className="warn small">
            {save.error instanceof ApiError ? save.error.detail : String(save.error)}
          </span>
        )}

        <CaptionPreview
          item={item}
          episodeId={episodeId}
          duration={duration}
          trimIn={trimIn}
          trimOut={trimOut}
          onSeek={onSeek}
          currentTime={currentTime}
        />
        <OverdubPanel take={take} episodeId={episodeId} videoRef={videoRef} currentTime={currentTime} />
      </div>
    </article>
  );
}

function CaptionPreview({
  item,
  episodeId,
  duration,
  trimIn,
  trimOut,
  currentTime,
  onSeek,
}: {
  item: AssemblyItem;
  episodeId: number;
  duration: number;
  trimIn: number;
  trimOut: number;
  currentTime: number;
  onSeek: (t: number) => void;
}) {
  const queryClient = useQueryClient();
  const c = item.captions;
  const [editing, setEditing] = useState(false);
  const [words, setWords] = useState<CaptionWord[]>(c.words);
  const [selectedIdx, setSelectedIdx] = useState<number | null>(null);

  useEffect(() => {
    setWords(c.words);
  }, [c.words]);

  const saveWords = useMutation({
    mutationFn: (nextWords: CaptionWord[]) =>
      api.put(`/snippets/${item.snippet_id}/captions`, { words: nextWords }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["assembly", episodeId] }),
  });

  const realign = useMutation({
    mutationFn: () => api.post(`/snippets/${item.snippet_id}/captions/realign`),
    onSuccess: () => {
      setEditing(false);
      setSelectedIdx(null);
      void queryClient.invalidateQueries({ queryKey: ["assembly", episodeId] });
    },
  });

  const inside = (w: CaptionWord) =>
    w.end > w.start ? w.end > trimIn && w.start < trimOut : w.start >= trimIn && w.start < trimOut;

  if (c.status === "unavailable" && c.lines.length === 0) return null;

  const dirty = JSON.stringify(words) !== JSON.stringify(c.words);
  const selWord = selectedIdx !== null ? words[selectedIdx] : undefined;

  const updateWord = (idx: number, patch: Partial<CaptionWord>) => {
    setWords((prev) =>
      prev.map((w, i) => {
        if (i !== idx) return w;
        const nextStart = patch.start !== undefined ? Math.max(0, Number(patch.start.toFixed(3))) : w.start;
        const rawEnd = patch.end !== undefined ? Number(patch.end.toFixed(3)) : w.end;
        const nextEnd = Math.max(nextStart, duration > 0 ? Math.min(duration, rawEnd) : rawEnd);
        return { ...w, ...patch, start: nextStart, end: nextEnd };
      }),
    );
  };

  const shiftFromIndex = (fromIdx: number, delta: number) => {
    setWords((prev) =>
      prev.map((w, i) => {
        if (i < fromIdx) return w;
        const span = Math.max(0.05, w.end - w.start);
        const nextStart = Math.max(0, Number((w.start + delta).toFixed(3)));
        const nextEnd = Number(
          (duration > 0 ? Math.min(duration, nextStart + span) : nextStart + span).toFixed(3),
        );
        return { ...w, start: nextStart, end: Math.max(nextStart, nextEnd) };
      }),
    );
  };

  return (
    <div className="captions-preview">
      <div className="captions-head">
        <span className="small muted">
          Captions ({words.length} words)
          {editing && " · click a word to edit its timing"}
        </span>
        {c.words.length > 0 && (
          <div className="row">
            {editing && (
              <>
                <button
                  type="button"
                  className="small-btn"
                  title="Shift all words by −0.10s"
                  onClick={() => shiftFromIndex(0, -0.1)}
                >
                  All −0.1s
                </button>
                <button
                  type="button"
                  className="small-btn"
                  title="Shift all words by +0.10s"
                  onClick={() => shiftFromIndex(0, 0.1)}
                >
                  All +0.1s
                </button>
                <button
                  type="button"
                  className="small-btn"
                  disabled={realign.isPending}
                  title="Discard manual edits and re-run automatic Whisper alignment"
                  onClick={() => realign.mutate()}
                >
                  {realign.isPending ? "Re-aligning…" : "↻ Auto Re-align"}
                </button>
              </>
            )}
            {dirty && (
              <>
                <button
                  type="button"
                  className="primary small-btn"
                  disabled={saveWords.isPending}
                  onClick={() => saveWords.mutate(words)}
                >
                  {saveWords.isPending ? "Saving…" : "Save timings"}
                </button>
                <button
                  type="button"
                  className="small-btn"
                  onClick={() => setWords(c.words)}
                >
                  Revert
                </button>
              </>
            )}
            <button
              type="button"
              className={editing ? "small-btn accent-outline" : "small-btn"}
              onClick={() => {
                setEditing((v) => !v);
                if (!editing && selectedIdx === null && words.length > 0) setSelectedIdx(0);
              }}
            >
              {editing ? "Done editing" : "✎ Edit timings"}
            </button>
          </div>
        )}
      </div>

      {c.error && <div className="warn small">{c.error}</div>}
      {saveWords.error && (
        <div className="warn small">
          {saveWords.error instanceof ApiError ? saveWords.error.detail : String(saveWords.error)}
        </div>
      )}

      {words.length > 0 ? (
        <div className="word-timeline">
          {words.map((w, i) => {
            const kept = inside(w);
            const activeWord = kept && currentTime >= w.start && currentTime <= Math.max(w.end, w.start + 0.15);
            const isSelected = editing && selectedIdx === i;
            return (
              <button
                key={i}
                type="button"
                className={[
                  "word",
                  kept ? "" : "dropped",
                  activeWord ? "active" : "",
                  isSelected ? "selected-word" : "",
                ]
                  .filter(Boolean)
                  .join(" ")}
                title={`${w.start.toFixed(2)}s – ${w.end.toFixed(2)}s${kept ? "" : " (trimmed out)"}`}
                onClick={() => {
                  onSeek(w.start);
                  if (editing) setSelectedIdx(i);
                }}
              >
                {w.word}
                <span className="t">{w.start.toFixed(1)}</span>
              </button>
            );
          })}
        </div>
      ) : (
        c.lines.length > 0 && <div className="muted small">{c.lines.join(" / ")}</div>
      )}

      {editing && selWord && selectedIdx !== null && (
        <div className="word-editor-bar">
          <strong className="accent-text">“{selWord.word}”</strong>
          <label className="small">
            start
            <input
              type="number"
              step={0.05}
              min={0}
              max={duration}
              value={selWord.start}
              onChange={(e) => {
                const nextStart = Number(e.target.value);
                const dur = Math.max(0.1, selWord.end - selWord.start);
                updateWord(selectedIdx, {
                  start: nextStart,
                  end: nextStart > selWord.end ? nextStart + dur : selWord.end,
                });
              }}
            />
            s
          </label>
          <button
            type="button"
            className="small-btn"
            title="Set word start to current playhead (keeps word duration)"
            onClick={() => {
              const dur = Math.max(0.15, selWord.end - selWord.start);
              const nextStart = Number(currentTime.toFixed(3));
              updateWord(selectedIdx, { start: nextStart, end: Number((nextStart + dur).toFixed(3)) });
            }}
          >
            Start ⟵ playhead ({currentTime.toFixed(2)}s)
          </button>
          <label className="small">
            end
            <input
              type="number"
              step={0.05}
              min={selWord.start}
              max={duration}
              value={selWord.end}
              onChange={(e) => updateWord(selectedIdx, { end: Number(e.target.value) })}
            />
            s
          </label>
          <button
            type="button"
            className="small-btn"
            title="Set word end to current playhead"
            onClick={() => updateWord(selectedIdx, { end: Math.max(selWord.start, Number(currentTime.toFixed(3))) })}
          >
            End ⟵ playhead
          </button>
          <button
            type="button"
            className="small-btn"
            title="Shift this word and all following words by −0.10s"
            onClick={() => shiftFromIndex(selectedIdx, -0.1)}
          >
            From here −0.1s
          </button>
          <button
            type="button"
            className="small-btn"
            title="Shift this word and all following words by +0.10s"
            onClick={() => shiftFromIndex(selectedIdx, 0.1)}
          >
            From here +0.1s
          </button>
        </div>
      )}
    </div>
  );
}
