import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, formatUsd } from "../../api";
import type { Take, TakeList } from "../../types/takes";
import GenerateBar from "./takes/GenerateBar";
import TakePromptView from "./takes/TakePromptView";
import { STATUS_LABEL, errorDetail } from "./takes/format";
import "./takes/TakesPanel.css";

/**
 * Takes area of the snippet side panel: Generate (N, seed, estimate), the takes
 * list (status, seed, cost, time, selected marker, slow/cancel, retry, hide),
 * one player for the clicked take, and the selected take's exact prompt.
 */
export function TakesPanel({ snippetId }: { snippetId: number }) {
  const queryClient = useQueryClient();
  const [showHidden, setShowHidden] = useState(false);
  const [playingId, setPlayingId] = useState<number | null>(null);

  const list = useQuery({
    queryKey: ["takes", snippetId, showHidden],
    queryFn: () => api.get<TakeList>(`/snippets/${snippetId}/takes${showHidden ? "?show_hidden=true" : ""}`),
    // Keeps the "slow" flag (queued/running > 30 min) current without events.
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
  const playing = takes.find((t) => t.id === playingId) ?? takes.find((t) => t.selected) ?? null;
  const promptTakeId = playing?.id ?? list.data?.selected_take_id ?? null;

  return (
    <section className="takes-panel">
      <h3>Takes</h3>
      <GenerateBar snippetId={snippetId} />

      {playing?.video_url ? (
        <div className="take-player">
          <video key={playing.id} src={playing.video_url} poster={playing.poster_url ?? undefined} controls playsInline />
          <div className="muted small">
            Take #{playing.id}
            {playing.selected && " · selected"}
            {playing.video_duration_s != null && ` · ${playing.video_duration_s.toFixed(2)}s`}
          </div>
        </div>
      ) : null}

      {list.error && <div className="warn">{errorDetail(list.error)}</div>}
      {action.error && <div className="warn">{errorDetail(action.error)}</div>}

      <ul className="takes-list">
        {takes.map((t) => (
          <li
            key={t.id}
            className={["take-row", t.selected && "selected", t.id === playing?.id && "playing", t.hidden && "hidden-take"]
              .filter(Boolean)
              .join(" ")}
            onClick={() => t.video_url && setPlayingId(t.id)}
          >
            <div className="take-thumb">
              {t.poster_url ? <img src={t.poster_url} alt="" loading="lazy" /> : <span className="muted">—</span>}
            </div>
            <div className="take-info">
              <div>
                <strong>#{t.id}</strong>
                {t.selected && (
                  <span className="badge selected-badge" title="Selected take">
                    ★ selected
                  </span>
                )}
                <span className={`badge status-${t.status}`}>{STATUS_LABEL[t.status]}</span>
                {t.slow && (
                  <span className="badge slow" title="Queued or running for more than 30 minutes">
                    slow
                  </span>
                )}
                {t.hidden && <span className="badge">hidden</span>}
              </div>
              <div className="muted small">
                seed {t.seed ?? (t.seed_requested != null ? `${t.seed_requested} (pinned)` : "random")} ·{" "}
                {t.cost_usd != null ? formatUsd(t.cost_usd) : `≈ ${formatUsd(t.estimated_cost_usd)}`} ·{" "}
                <time dateTime={t.created_at} title={t.created_at}>
                  {new Date(t.created_at).toLocaleString()}
                </time>
              </div>
              {t.error && <div className="warn small take-error">{t.error}</div>}
            </div>
            <div className="take-actions" onClick={(e) => e.stopPropagation()}>
              {t.can_select && !t.selected && (
                <button disabled={action.isPending} onClick={() => action.mutate({ take: t, verb: "select" })}>
                  Select
                </button>
              )}
              {t.can_cancel && (
                <button
                  disabled={action.isPending}
                  className={t.slow ? "danger" : undefined}
                  onClick={() => window.confirm(`Cancel take #${t.id} at fal?`) && action.mutate({ take: t, verb: "cancel" })}
                >
                  Cancel
                </button>
              )}
              {t.can_retry && (
                <button
                  disabled={action.isPending}
                  title={
                    t.status === "failed" ? "New take from the stored parameters (new fal call)" : "Fetch/process again (no new fal call)"
                  }
                  onClick={() => action.mutate({ take: t, verb: "retry" })}
                >
                  Retry
                </button>
              )}
              {!t.selected &&
                (t.hidden ? (
                  <button disabled={action.isPending} onClick={() => action.mutate({ take: t, verb: "unhide" })}>
                    Unhide
                  </button>
                ) : (
                  <button disabled={action.isPending} onClick={() => action.mutate({ take: t, verb: "hide" })}>
                    Hide
                  </button>
                ))}
            </div>
          </li>
        ))}
        {list.data && takes.length === 0 && <li className="muted">No takes yet.</li>}
      </ul>

      {list.data && (list.data.hidden_count > 0 || showHidden) && (
        <label className="small">
          <input type="checkbox" checked={showHidden} onChange={(e) => setShowHidden(e.target.checked)} /> show hidden (
          {list.data.hidden_count})
        </label>
      )}

      {promptTakeId != null && (
        <TakePromptView takeId={promptTakeId} selected={promptTakeId === list.data?.selected_take_id} />
      )}
    </section>
  );
}

export default TakesPanel;
