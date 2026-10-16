import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, formatUsd } from "../../api";
import type { Take, TakeList } from "../../types/takes";
import GenerateBar from "./takes/GenerateBar";
import TakePromptView from "./takes/TakePromptView";
import { STATUS_LABEL, errorDetail } from "./takes/format";
import "./takes/TakesPanel.css";

interface PromptSummary {
  refsCount: number;
  charCount: number;
  maxChars: number;
  onInspectPrompt: () => void;
}

/**
 * Takes area of the snippet side panel:
 * - Side-by-side 9:16 video player + takes list when takes exist
 * - Sticky/docked Generate bar
 * - Collapsible frozen prompt viewer for the selected/playing take
 */
export function TakesPanel({ snippetId, promptSummary }: { snippetId: number; promptSummary?: PromptSummary }) {
  const queryClient = useQueryClient();
  const [showHidden, setShowHidden] = useState(false);
  const [playingId, setPlayingId] = useState<number | null>(null);
  const [inspectTakePrompt, setInspectTakePrompt] = useState(false);

  const list = useQuery({
    queryKey: ["takes", snippetId, showHidden],
    queryFn: () => api.get<TakeList>(`/snippets/${snippetId}/takes${showHidden ? "?show_hidden=true" : ""}`),
    refetchInterval: (q) => (q.state.data?.takes.some((t) => t.can_cancel) ? 60_000 : false),
  });

  const action = useMutation({
    mutationFn: ({ take, verb }: { take: Take; verb: "select" | "hide" | "unhide" | "cancel" | "retry" }) =>
      api.post<unknown>(`/takes/${take.id}/${verb}`),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["takes", snippetId] });
      void queryClient.invalidateQueries({ queryKey: ["snippets"] });
    },
  });

  const takes = list.data?.takes ?? [];
  const playing = takes.find((t) => t.id === playingId) ?? takes.find((t) => t.selected) ?? takes[0] ?? null;
  const promptTakeId = playing?.id ?? list.data?.selected_take_id ?? null;

  return (
    <section className="takes-panel">
      <div className="takes-head">
        <h3>Takes ({takes.length})</h3>
        {promptSummary && (
          <button
            type="button"
            className="link small"
            onClick={promptSummary.onInspectPrompt}
            title="View all 5 prompt parts and attached references"
          >
            {promptSummary.refsCount} refs · {promptSummary.charCount.toLocaleString()}/
            {promptSummary.maxChars.toLocaleString()} chars ↗
          </button>
        )}
      </div>

      {list.error && <div className="warn small">{errorDetail(list.error)}</div>}
      {action.error && <div className="warn small">{errorDetail(action.error)}</div>}

      {takes.length > 0 ? (
        <div className="takes-stage">
          <div className="take-player-col">
            {playing?.video_url ? (
              <div className="take-player">
                <video
                  key={playing.id}
                  src={playing.video_url}
                  poster={playing.poster_url ?? undefined}
                  controls
                  playsInline
                />
                <div className="take-player-caption">
                  <strong>Take #{playing.id}</strong>
                  {playing.selected && <span className="badge selected-badge">★ Selected</span>}
                  {playing.video_duration_s != null && (
                    <span className="muted small">{playing.video_duration_s.toFixed(1)}s</span>
                  )}
                </div>
              </div>
            ) : (
              <div className="take-player take-player-placeholder">
                <span className="muted small">
                  {playing ? `Take #${playing.id} (${STATUS_LABEL[playing.status]})` : "No video ready"}
                </span>
              </div>
            )}
          </div>

          <div className="takes-list-col">
            <ul className="takes-list">
              {takes.map((t) => (
                <li
                  key={t.id}
                  className={[
                    "take-row",
                    t.selected && "selected",
                    t.id === playing?.id && "playing",
                    t.hidden && "hidden-take",
                  ]
                    .filter(Boolean)
                    .join(" ")}
                  onClick={() => setPlayingId(t.id)}
                >
                  <div className="take-thumb">
                    {t.poster_url ? (
                      <img src={t.poster_url} alt="" loading="lazy" />
                    ) : (
                      <span className="muted small">—</span>
                    )}
                  </div>
                  <div className="take-info">
                    <div className="take-info-top">
                      <strong>#{t.id}</strong>
                      {t.selected ? (
                        <span className="badge selected-badge" title="Selected take for assembly">
                          ★ Selected
                        </span>
                      ) : (
                        <span className={`badge status-${t.status}`}>{STATUS_LABEL[t.status]}</span>
                      )}
                      {t.slow && (
                        <span className="badge slow" title="Queued or running for more than 30 minutes">
                          slow
                        </span>
                      )}
                      {t.hidden && <span className="badge">hidden</span>}
                    </div>
                    <div className="muted small">
                      seed {t.seed ?? (t.seed_requested != null ? `${t.seed_requested}` : "auto")} ·{" "}
                      {t.cost_usd != null ? formatUsd(t.cost_usd) : `≈${formatUsd(t.estimated_cost_usd)}`}
                    </div>
                    {t.error && <div className="warn small take-error">{t.error}</div>}
                    <div className="take-actions" onClick={(e) => e.stopPropagation()}>
                      {t.can_select && !t.selected && (
                        <button
                          className="small-btn"
                          disabled={action.isPending}
                          onClick={() => action.mutate({ take: t, verb: "select" })}
                        >
                          Select
                        </button>
                      )}
                      {t.can_cancel && (
                        <button
                          disabled={action.isPending}
                          className={t.slow ? "danger small-btn" : "small-btn"}
                          onClick={() =>
                            window.confirm(`Cancel take #${t.id} at fal?`) &&
                            action.mutate({ take: t, verb: "cancel" })
                          }
                        >
                          Cancel
                        </button>
                      )}
                      {t.can_retry && (
                        <button
                          className="small-btn"
                          disabled={action.isPending}
                          title={
                            t.status === "failed"
                              ? "New take from stored parameters (new fal call)"
                              : "Fetch/process again (no new fal call)"
                          }
                          onClick={() => action.mutate({ take: t, verb: "retry" })}
                        >
                          Retry
                        </button>
                      )}
                      {!t.selected &&
                        (t.hidden ? (
                          <button
                            className="link small"
                            disabled={action.isPending}
                            onClick={() => action.mutate({ take: t, verb: "unhide" })}
                          >
                            unhide
                          </button>
                        ) : (
                          <button
                            className="link small"
                            disabled={action.isPending}
                            onClick={() => action.mutate({ take: t, verb: "hide" })}
                          >
                            hide
                          </button>
                        ))}
                    </div>
                  </div>
                </li>
              ))}
            </ul>

            <div className="takes-foot-links">
              {promptTakeId != null && (
                <button
                  type="button"
                  className="link small"
                  onClick={() => setInspectTakePrompt((v) => !v)}
                >
                  {inspectTakePrompt ? "Hide frozen take prompt" : `View take #${promptTakeId} frozen prompt ▾`}
                </button>
              )}
              {list.data && (list.data.hidden_count > 0 || showHidden) && (
                <label className="small muted">
                  <input type="checkbox" checked={showHidden} onChange={(e) => setShowHidden(e.target.checked)} />{" "}
                  hidden ({list.data.hidden_count})
                </label>
              )}
            </div>
          </div>
        </div>
      ) : (
        <div className="takes-empty muted small">
          No takes generated for this scene yet. Click Generate below to create H3 video takes.
        </div>
      )}

      {promptTakeId != null && inspectTakePrompt && (
        <TakePromptView takeId={promptTakeId} selected={promptTakeId === list.data?.selected_take_id} />
      )}

      <GenerateBar snippetId={snippetId} />
    </section>
  );
}

export default TakesPanel;
