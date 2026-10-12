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

/** Snippet board: split (L0 / L2 preview), add, drag reorder, archive, Archived drawer. */
export default function SnippetBoard({ episode, board, selectedId, onSelect, scriptDirty }: Props) {
  const queryClient = useQueryClient();
  const episodeId = episode.id;
  const [level, setLevel] = useState<0 | 2>(0);
  const [model, setModel] = useState(episode.openrouter_model);
  const [error, setError] = useState<string | null>(null);
  const [l0Result, setL0Result] = useState<Level0Result | null>(null);
  const [dragId, setDragId] = useState<number | null>(null);
  const [dropIndex, setDropIndex] = useState<number | null>(null);
  const [dismissedProposal, setDismissedProposal] = useState<number | null>(null);

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ["snippets"] });
  const onError = (err: Error) => setError(err.message);

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
        onSelect(null);
      } else {
        setL0Result(null);
        setDismissedProposal(null);
      }
      void invalidate();
      void queryClient.invalidateQueries({ queryKey: ["spend"] });
    },
    onError,
  });

  const runSplit = () => {
    if (scriptDirty && !window.confirm("The script has unsaved changes. Split uses the last saved version. Continue?"))
      return;
    if (level === 0 && board.active.length > 0 &&
      !window.confirm(`Archive the ${board.active.length} snippets on the board and replace them?`))
      return;
    split.mutate();
  };

  const decide = useMutation({
    mutationFn: ({ id, action }: { id: number; action: "accept" | "reject" }) =>
      api.post(`/split-proposals/${id}/${action}`),
    onSuccess: () => {
      setError(null);
      onSelect(null);
      void invalidate();
    },
    onError,
  });

  const add = useMutation({
    mutationFn: () => api.post<Snippet>(`/episodes/${episodeId}/snippets`),
    onSuccess: (s) => {
      onSelect(s.id);
      void invalidate();
    },
    onError,
  });

  const action = useMutation({
    mutationFn: ({ id, verb }: { id: number; verb: "archive" | "restore" | "copy" }) =>
      api.post<Snippet>(`/snippets/${id}/${verb}`),
    onSuccess: (s, { id, verb }) => {
      if (verb === "copy") onSelect(s.id);
      if (verb === "archive" && id === selectedId) onSelect(null);
      void invalidate();
    },
    onError,
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
      onError(err);
    },
    onSettled: () => void invalidate(),
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
        <h2>Snippets</h2>
        <button onClick={() => add.mutate()} disabled={add.isPending}>
          + Add
        </button>
      </header>

      <div className="ws-split">
        <label>
          Split
          <select value={level} onChange={(e) => setLevel(Number(e.target.value) as 0 | 2)}>
            <option value={0}>Level 0 (no LLM)</option>
            <option value={2}>Level 2 (OpenRouter)</option>
          </select>
        </label>
        {level === 2 && (
          <input
            className="ws-model"
            value={model}
            onChange={(e) => setModel(e.target.value)}
            title="OpenRouter model"
            placeholder="OpenRouter model"
          />
        )}
        <button onClick={runSplit} disabled={split.isPending || proposal?.running === true}>
          {split.isPending ? "Splitting…" : "Split"}
        </button>
      </div>

      <div className="ws-total">
        Estimated total <strong>{board.estimated_total_s}s</strong> ({board.active.length} snippets)
        {board.length_warning && <span className="warn"> · {board.length_warning}</span>}
      </div>
      {error && (
        <p className="warn" onClick={() => setError(null)}>
          {error}
        </p>
      )}
      {l0Result && (
        <div className="ws-note">
          Level 0 split: {l0Result.snippet_ids.length} snippets, script ≈{l0Result.script_estimate_s.toFixed(1)}s,
          total {l0Result.estimated_total_s}s.
          {l0Result.warnings.map((w) => (
            <div key={w} className="warn">
              {w}
            </div>
          ))}
          <button className="link" onClick={() => setL0Result(null)}>
            dismiss
          </button>
        </div>
      )}

      {showProposal && proposal && (
        <ProposalPreview
          proposal={proposal}
          busy={decide.isPending}
          onAccept={() => {
            if (board.active.length === 0 ||
              window.confirm(`Archive the ${board.active.length} snippets on the board and use this split?`))
              decide.mutate({ id: proposal.id, action: "accept" });
          }}
          onReject={() => decide.mutate({ id: proposal.id, action: "reject" })}
          onDismiss={() => setDismissedProposal(proposal.id)}
        />
      )}

      <ol className="ws-cards" onDragLeave={() => setDropIndex(null)}>
        {board.active.map((s, i) => (
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
              s.id === dragId ? "dragging" : "",
              dropIndex === i ? "drop-before" : "",
              dropIndex === i + 1 && i === board.active.length - 1 ? "drop-after" : "",
            ].join(" ")}
            onClick={() => onSelect(s.id)}
          >
            <div className="ws-card-top">
              <span className="ws-handle" title="Drag to reorder">
                ⋮⋮
              </span>
              <strong>#{i + 1}</strong>
              <span>{s.target_duration_s}s</span>
              {s.continue_from_previous && !s.is_first && <span className="badge">↪ continue</span>}
              {s.stale && <span className="badge warn-badge">stale</span>}
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
              {s.take_count > 0 && <span className="badge">{s.take_count} takes</span>}
              <button
                className="link ws-card-archive"
                title="Delete (archive)"
                onClick={(e) => {
                  e.stopPropagation();
                  action.mutate({ id: s.id, verb: "archive" });
                }}
              >
                archive
              </button>
            </div>
            <div className="ws-muted ws-card-meta">
              {s.cast.map((c) => c.name).join(", ") || "no cast"} · {s.location_name ?? "no location"}
            </div>
            <div className="ws-card-text">{firstLines(s.script_slice || s.body) || <em>empty</em>}</div>
          </li>
        ))}
        {board.active.length === 0 && <li className="ws-muted">No snippets. Split the script or add one.</li>}
      </ol>

      <details className="ws-archived">
        <summary>Archived ({board.archived.length})</summary>
        <ul>
          {board.archived.map((s) => (
            <li key={s.id}>
              <div>
                <strong>{s.target_duration_s}s</strong>{" "}
                <span className="ws-muted">
                  {s.origin} · {s.take_count} takes · archived {s.archived_at ? new Date(s.archived_at).toLocaleString() : ""}
                </span>
              </div>
              <div className="ws-card-text">{firstLines(s.script_slice || s.body) || <em>empty</em>}</div>
              <button onClick={() => action.mutate({ id: s.id, verb: "restore" })}>Restore</button>{" "}
              <button onClick={() => action.mutate({ id: s.id, verb: "copy" })}>Copy into new snippet</button>{" "}
              <button className="link" onClick={() => onSelect(s.id)}>
                view
              </button>
            </li>
          ))}
        </ul>
      </details>
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
        <strong>Level 2 split failed</strong> ({proposal.model}, {formatUsd(proposal.cost_usd)})
        <p className="warn">{proposal.error}</p>
        <button onClick={onDismiss}>Dismiss</button>
      </div>
    );
  }
  if (proposal.running || proposal.result.length === 0) {
    return (
      <div className="ws-proposal">
        Level 2 split running with {proposal.model}…{" "}
        <button onClick={onReject} disabled={busy}>
          Cancel
        </button>
      </div>
    );
  }
  return (
    <div className="ws-proposal">
      <header>
        <strong>Level 2 preview</strong> · v{proposal.script_version} · {proposal.model} ·{" "}
        {formatUsd(proposal.cost_usd)} · total {proposal.estimated_total_s}s
      </header>
      {proposal.warnings.map((w) => (
        <div key={w} className="warn">
          {w}
        </div>
      ))}
      <ol>
        {proposal.result.map((s, i) => (
          <li key={i}>
            <div>
              <strong>{s.target_duration_s}s</strong> (slice ≈{s.estimate_s}s) ·{" "}
              {s.cast.map((c) => c.name).join(", ") || "no cast"} · {s.location?.name ?? "no location"}
            </div>
            <pre className="ws-pre">{s.script_slice}</pre>
            <details>
              <summary>body</summary>
              <pre className="ws-pre">{s.body}</pre>
            </details>
          </li>
        ))}
      </ol>
      <button onClick={onAccept} disabled={busy}>
        Accept
      </button>{" "}
      <button onClick={onReject} disabled={busy}>
        Reject
      </button>
    </div>
  );
}
