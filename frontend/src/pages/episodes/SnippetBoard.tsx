import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, formatUsd } from "../../api";
import type { Board, Episode, Level0Result, Snippet, SplitProposal } from "../../types/workspace";

interface Props {
  episode: Episode;
  board: Board;
  selectedId: number | null;
  onSelect: (id: number | null) => void;
  scriptDirty: boolean;
}

/** Visual Storyboard: split (L0 / L2 preview), add, drag reorder, 9:16 poster thumbnails, status badges. */
export default function SnippetBoard({ episode, board, selectedId, onSelect, scriptDirty }: Props) {
  const queryClient = useQueryClient();
  const episodeId = episode.id;
  const [level, setLevel] = useState<0 | 2>(0);
  const [model, setModel] = useState(episode.openrouter_model);
  const [showModelInput, setShowModelInput] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [l0Result, setL0Result] = useState<Level0Result | null>(null);
  const [dragId, setDragId] = useState<number | null>(null);
  const [dropIndex, setDropIndex] = useState<number | null>(null);
  const [dismissedProposal, setDismissedProposal] = useState<number | null>(null);

  const proposals = useQuery({
    queryKey: ["snippets", "proposals", episodeId],
    queryFn: () => api.get<SplitProposal[]>(`/episodes/${episodeId}/split-proposals?level=2&limit=1`),
    refetchInterval: (q) => (q.state.data?.[0]?.running ? 2000 : false),
  });
  const proposal = proposals.data?.[0];
  const showProposal =
    proposal && proposal.id !== dismissedProposal && (proposal.status === "pending" || proposal.status === "failed");

  const split = useMutation({
    mutationFn: () =>
      api.post<Level0Result | { level: 2; proposal: SplitProposal }>(`/episodes/${episodeId}/split`, {
        level,
        model: level === 2 ? model : undefined,
      }),
    onSuccess: (res) => {
      setError(null);
      if (res.level === 0) {
        setL0Result(res);
        onSelect(res.snippet_ids[0] ?? null);
      } else {
        setL0Result(null);
        setDismissedProposal(null);
      }
      void queryClient.invalidateQueries({ queryKey: ["snippets"] });
      void queryClient.invalidateQueries({ queryKey: ["spend"] });
    },
    onError: (err: Error) => setError(err.message),
  });

  const runSplit = () => {
    if (scriptDirty && !window.confirm("The script has unsaved changes. Split uses the last saved version. Continue?"))
      return;
    if (
      level === 0 &&
      board.active.length > 0 &&
      !window.confirm(`Archive the ${board.active.length} snippets on the board and replace them?`)
    )
      return;
    split.mutate();
  };

  const decide = useMutation({
    mutationFn: ({ id, action }: { id: number; action: "accept" | "reject" }) =>
      api.post(`/split-proposals/${id}/${action}`),
    onSuccess: () => {
      setError(null);
      onSelect(null);
      void queryClient.invalidateQueries({ queryKey: ["snippets"] });
    },
    onError: (err: Error) => setError(err.message),
  });

  const add = useMutation({
    mutationFn: () => api.post<Snippet>(`/episodes/${episodeId}/snippets`),
    onSuccess: (s) => {
      onSelect(s.id);
      void queryClient.invalidateQueries({ queryKey: ["snippets"] });
    },
    onError: (err: Error) => setError(err.message),
  });

  const action = useMutation({
    mutationFn: ({ id, verb }: { id: number; verb: "archive" | "restore" | "copy" }) =>
      api.post<Snippet>(`/snippets/${id}/${verb}`),
    onSuccess: (s, { id, verb }) => {
      if (verb === "copy") onSelect(s.id);
      if (verb === "archive" && id === selectedId) onSelect(null);
      void queryClient.invalidateQueries({ queryKey: ["snippets"] });
    },
    onError: (err: Error) => setError(err.message),
  });

  const reorder = useMutation({
    mutationFn: (ids: number[]) => api.put<Board>(`/episodes/${episodeId}/snippets/order`, { ids }),
    onMutate: async (ids) => {
      await queryClient.cancelQueries({ queryKey: ["snippets", episodeId] });
      const prev = queryClient.getQueryData<Board>(["snippets", episodeId]);
      if (prev) {
        const byId = new Map(prev.active.map((s) => [s.id, s]));
        const active = ids.map((id) => byId.get(id)).filter((s): s is Snippet => s !== undefined);
        queryClient.setQueryData<Board>(["snippets", episodeId], { ...prev, active });
      }
      return { prev };
    },
    onError: (err: Error, _ids, ctx) => {
      if (ctx?.prev) queryClient.setQueryData(["snippets", episodeId], ctx.prev);
      setError(err.message);
    },
    onSettled: () => void queryClient.invalidateQueries({ queryKey: ["snippets"] }),
  });

  const drop = (index: number) => {
    if (dragId === null) return;
    const ids = board.active.map((s) => s.id).filter((id) => id !== dragId);
    const from = board.active.findIndex((s) => s.id === dragId);
    const target = index > from ? index - 1 : index;
    ids.splice(target, 0, dragId);
    setDragId(null);
    setDropIndex(null);
    if (ids.some((id, i) => id !== board.active[i]?.id)) reorder.mutate(ids);
  };

  return (
    <section className="ws-board">
      <header className="ws-head">
        <div>
          <h2>Storyboard</h2>
          <span className="ws-sub">
            {board.active.length} scenes · <strong>{board.estimated_total_s}s</strong> est.
            {board.length_warning && (
              <span className="warn" title={board.length_warning}>
                {" "}
                · ⚠ target length
              </span>
            )}
          </span>
        </div>
        <button className="small-btn" onClick={() => add.mutate()} disabled={add.isPending}>
          + Scene
        </button>
      </header>

      <div className="ws-split-bar">
        <select
          className="ws-split-select"
          value={level}
          onChange={(e) => setLevel(Number(e.target.value) as 0 | 2)}
          aria-label="Split mode"
        >
          <option value={0}>Split: Fast Rules (L0)</option>
          <option value={2}>Split: Smart LLM (L2)</option>
        </select>
        {level === 2 && (
          <button
            type="button"
            className="small-btn icon-btn"
            title={`Model: ${model} (click to edit)`}
            onClick={() => setShowModelInput((v) => !v)}
          >
            ⚙ Model
          </button>
        )}
        <button
          className="primary small-btn"
          onClick={runSplit}
          disabled={split.isPending || proposal?.running === true}
        >
          {split.isPending ? "Splitting…" : "Split ↻"}
        </button>
      </div>

      {level === 2 && showModelInput && (
        <div className="ws-model-row">
          <input
            className="ws-model"
            value={model}
            onChange={(e) => setModel(e.target.value)}
            title="OpenRouter model"
            placeholder="OpenRouter model"
          />
        </div>
      )}

      {error && (
        <p className="warn small" onClick={() => setError(null)}>
          {error}
        </p>
      )}
      {l0Result && (
        <div className="ws-note">
          <span>
            Split into {l0Result.snippet_ids.length} scenes (≈{l0Result.script_estimate_s.toFixed(1)}s script,{" "}
            {l0Result.estimated_total_s}s total).
          </span>
          {l0Result.warnings.map((w) => (
            <div key={w} className="warn small">
              {w}
            </div>
          ))}
          <button className="link small" onClick={() => setL0Result(null)}>
            dismiss
          </button>
        </div>
      )}

      {showProposal && proposal && (
        <ProposalPreview
          proposal={proposal}
          busy={decide.isPending}
          onAccept={() => {
            if (
              board.active.length === 0 ||
              window.confirm(`Archive the ${board.active.length} snippets on the board and use this split?`)
            )
              decide.mutate({ id: proposal.id, action: "accept" });
          }}
          onReject={() => decide.mutate({ id: proposal.id, action: "reject" })}
          onDismiss={() => setDismissedProposal(proposal.id)}
        />
      )}

      <ol className="ws-cards" onDragLeave={() => setDropIndex(null)}>
        {board.active.map((s, i) => {
          const hasSelected = s.selected_take_id !== null;
          return (
            <li
              key={s.id}
              draggable
              onDragStart={(e) => {
                setDragId(s.id);
                e.dataTransfer.effectAllowed = "move";
              }}
              onDragEnd={() => {
                setDragId(null);
                setDropIndex(null);
              }}
              onDragOver={(e) => {
                e.preventDefault();
                const rect = e.currentTarget.getBoundingClientRect();
                setDropIndex(e.clientY < rect.top + rect.height / 2 ? i : i + 1);
              }}
              onDrop={(e) => {
                e.preventDefault();
                drop(dropIndex ?? i);
              }}
              className={[
                "ws-card",
                s.id === selectedId ? "selected" : "",
                !hasSelected && s.take_count === 0 ? "needs-takes" : "",
                s.id === dragId ? "dragging" : "",
                dropIndex === i ? "drop-before" : "",
                dropIndex === i + 1 && i === board.active.length - 1 ? "drop-after" : "",
              ]
                .filter(Boolean)
                .join(" ")}
              onClick={() => onSelect(s.id)}
            >
              <div className="ws-card-poster">
                {s.selected_take_poster_url ? (
                  <img src={s.selected_take_poster_url} alt="" loading="lazy" />
                ) : (
                  <span className="ws-poster-placeholder">{hasSelected ? "▶" : "9:16"}</span>
                )}
              </div>
              <div className="ws-card-body">
                <div className="ws-card-top">
                  <span className="ws-handle" title="Drag to reorder">
                    ⋮⋮
                  </span>
                  <strong>#{i + 1}</strong>
                  <span className="ws-card-loc">{s.location_name ?? "No location"}</span>
                  {s.continue_from_previous && !s.is_first && (
                    <span className="badge" title="Continues from previous snippet's end frame">
                      ↳ cont.
                    </span>
                  )}
                  {s.stale && (
                    <span className="badge warn-badge" title="Predecessor changed since selected take was generated">
                      ⚠ stale
                    </span>
                  )}
                  {s.warnings.length > 0 && (
                    <span className="badge warn-badge" title={s.warnings.join("\n")}>
                      ⚠ {s.warnings.length}
                    </span>
                  )}
                  {s.blocked_reason && (
                    <span className="badge warn-badge" title={s.blocked_reason}>
                      blocked
                    </span>
                  )}
                  <span className="ws-card-status">
                    {hasSelected ? (
                      <span className="badge ok-badge">★ Take #{s.selected_take_id}</span>
                    ) : s.take_count > 0 ? (
                      <span className="badge">{s.take_count} takes</span>
                    ) : (
                      <span className="badge muted-badge">no takes</span>
                    )}
                  </span>
                </div>

                <div className="ws-card-meta">
                  <span>{s.cast.map((c) => c.name).join(", ") || "No cast"}</span>
                  <span>·</span>
                  <span>
                    {s.target_duration_s}s{s.estimate_s !== null ? ` (≈${s.estimate_s.toFixed(1)}s)` : ""}
                  </span>
                  <button
                    className="link ws-card-archive"
                    title="Archive scene"
                    onClick={(e) => {
                      e.stopPropagation();
                      action.mutate({ id: s.id, verb: "archive" });
                    }}
                  >
                    archive
                  </button>
                </div>

                <div className="ws-card-text">{firstLines(s.script_slice || s.body) || <em>Empty scene</em>}</div>
              </div>
            </li>
          );
        })}
        {board.active.length === 0 && (
          <li className="ws-empty-board">
            <p className="muted">No scenes on the storyboard yet.</p>
            <p className="small muted">Click “Split ↻” above to break the script into scenes, or add one manually.</p>
          </li>
        )}
      </ol>

      {board.archived.length > 0 && (
        <details className="ws-archived">
          <summary>Archived scenes ({board.archived.length})</summary>
          <ul>
            {board.archived.map((s) => (
              <li key={s.id}>
                <div className="row spread">
                  <span>
                    <strong>{s.target_duration_s}s</strong>{" "}
                    <span className="ws-muted small">
                      {s.origin} · {s.take_count} takes
                    </span>
                  </span>
                  <span className="row">
                    <button className="small-btn" onClick={() => action.mutate({ id: s.id, verb: "restore" })}>
                      Restore
                    </button>
                    <button className="small-btn" onClick={() => action.mutate({ id: s.id, verb: "copy" })}>
                      Duplicate
                    </button>
                    <button className="link small" onClick={() => onSelect(s.id)}>
                      inspect
                    </button>
                  </span>
                </div>
                <div className="ws-card-text muted">{firstLines(s.script_slice || s.body) || <em>empty</em>}</div>
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}

function firstLines(text: string, n = 2): string {
  const lines = text.split("\n").filter((l) => l.trim());
  return lines.slice(0, n).join(" / ") + (lines.length > n ? " …" : "");
}

interface PreviewProps {
  proposal: SplitProposal;
  busy: boolean;
  onAccept: () => void;
  onReject: () => void;
  onDismiss: () => void;
}

function ProposalPreview({ proposal, busy, onAccept, onReject, onDismiss }: PreviewProps) {
  if (proposal.status === "failed") {
    return (
      <div className="ws-proposal">
        <strong>Smart split failed</strong> ({proposal.model}, {formatUsd(proposal.cost_usd)})
        <p className="warn small">{proposal.error}</p>
        <button className="small-btn" onClick={onDismiss}>
          Dismiss
        </button>
      </div>
    );
  }
  if (proposal.running || proposal.result.length === 0) {
    return (
      <div className="ws-proposal">
        <span className="spinner" aria-hidden /> Smart split running ({proposal.model})…{" "}
        <button className="small-btn" onClick={onReject} disabled={busy}>
          Cancel
        </button>
      </div>
    );
  }
  return (
    <div className="ws-proposal">
      <header className="row spread">
        <span>
          <strong>Smart Split Preview</strong> · v{proposal.script_version} · {formatUsd(proposal.cost_usd)} ·{" "}
          {proposal.estimated_total_s}s
        </span>
        <span className="row">
          <button className="primary small-btn" onClick={onAccept} disabled={busy}>
            Accept ({proposal.result.length})
          </button>
          <button className="small-btn" onClick={onReject} disabled={busy}>
            Reject
          </button>
        </span>
      </header>
      {proposal.warnings.map((w) => (
        <div key={w} className="warn small">
          {w}
        </div>
      ))}
      <ol>
        {proposal.result.map((s, i) => (
          <li key={i}>
            <div className="small">
              <strong>{s.target_duration_s}s</strong> (≈{s.estimate_s}s) ·{" "}
              {s.cast.map((c) => c.name).join(", ") || "no cast"} · {s.location?.name ?? "no location"}
            </div>
            <pre className="ws-pre">{s.script_slice}</pre>
            <details>
              <summary className="small muted">camera &amp; action body</summary>
              <pre className="ws-pre">{s.body}</pre>
            </details>
          </li>
        ))}
      </ol>
    </div>
  );
}
