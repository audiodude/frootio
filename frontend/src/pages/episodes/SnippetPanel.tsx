import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, formatUsd } from "../../api";
import {
  DERIVED_PARTS,
  PART_NAMES,
  type BuiltPrompt,
  type Episode,
  type PartName,
  type Snippet,
} from "../../types/workspace";
import { TakesPanel } from "./TakesPanel";

const PART_LABELS: Record<PartName, string> = {
  style: "Style preamble",
  legend: "Refs legend",
  location: "Location text",
  body: "Body",
  tail: "Tail",
};

const PART_SOURCES: Record<string, string> = {
  style: "series style preamble",
  legend: "cast, character descriptions and location images",
  location: "location description",
};

interface Props {
  episode: Episode;
  snippet: Snippet;
  index: number | null; // board position (null when archived)
  onClose: () => void;
}

/** Side panel for the selected snippet: fields, five prompt parts, joined preview, takes. */
export default function SnippetPanel({ episode, snippet, index, onClose }: Props) {
  const queryClient = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const id = snippet.id;
  const promptKey = ["snippets", "prompt", id];
  const built = useQuery({
    queryKey: promptKey,
    queryFn: () => api.get<BuiltPrompt>(`/snippets/${id}/prompt`),
  });

  const onError = (err: Error) => setError(err.message);
  const patch = useMutation({
    mutationFn: (body: Record<string, unknown>) => api.patch<Snippet>(`/snippets/${id}`, body),
    onSuccess: () => {
      setError(null);
      void queryClient.invalidateQueries({ queryKey: ["snippets"] });
    },
    onError,
  });
  const putParts = useMutation({
    mutationFn: (parts: Partial<Record<PartName, { text?: string; overridden: boolean }>>) =>
      api.put<BuiltPrompt>(`/snippets/${id}/parts`, { parts }),
    onSuccess: (data) => {
      setError(null);
      queryClient.setQueryData(promptKey, data);
      void queryClient.invalidateQueries({ queryKey: ["snippets"] });
    },
    onError,
  });
  const restore = useMutation({
    mutationFn: () => api.post<Snippet>(`/snippets/${id}/restore`),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["snippets"] }),
    onError,
  });

  const castIds = snippet.cast.map((c) => c.id);
  const castChoices = episode.cast.filter((c) => !c.archived || castIds.includes(c.id));
  const locationChoices = episode.locations.filter((l) => !l.archived || l.id === snippet.location_id);

  return (
    <aside className="ws-panel">
      <header className="ws-head">
        <h2>
          {index !== null ? `Snippet #${index + 1}` : "Archived snippet"}{" "}
          <span className="ws-muted">({snippet.origin}, id {id})</span>
        </h2>
        <button className="link" onClick={onClose}>
          close
        </button>
      </header>
      {snippet.archived && (
        <p className="ws-note">
          Archived. <button onClick={() => restore.mutate()}>Restore to the end of the board</button>
        </p>
      )}
      {error && (
        <p className="warn" onClick={() => setError(null)}>
          {error}
        </p>
      )}
      {snippet.warnings.map((w) => (
        <p key={w} className="warn">
          ⚠ {w}
        </p>
      ))}

      <div className="ws-fields">
        <div className="ws-field">
          <span className="ws-label">Cast</span>
          <div className="ws-chips">
            {castChoices.map((c) => {
              const pos = castIds.indexOf(c.id);
              return (
                <button
                  key={c.id}
                  className={pos >= 0 ? "chip on" : "chip"}
                  onClick={() =>
                    patch.mutate({ cast_ids: pos >= 0 ? castIds.filter((x) => x !== c.id) : [...castIds, c.id] })
                  }
                  title={pos >= 0 ? `Legend order ${pos + 1}; click to remove` : "Add to cast"}
                >
                  {pos >= 0 && <span className="chip-n">{pos + 1}</span>}
                  {c.name}
                  {c.has_voice && " 🎙"}
                  {c.archived && " (archived)"}
                </button>
              );
            })}
            {castChoices.length === 0 && <span className="ws-muted">The series has no cast yet.</span>}
          </div>
        </div>
        <label className="ws-field">
          <span className="ws-label">Location</span>
          <select
            value={snippet.location_id ?? ""}
            onChange={(e) => patch.mutate({ location_id: e.target.value === "" ? null : Number(e.target.value) })}
          >
            <option value="">(none)</option>
            {locationChoices.map((l) => (
              <option key={l.id} value={l.id}>
                {l.name}
                {l.ref_image_count ? ` (${l.ref_image_count} ref images)` : ""}
              </option>
            ))}
          </select>
        </label>
        <label className="ws-field">
          <span className="ws-label">Target duration</span>
          <select
            value={snippet.target_duration_s}
            onChange={(e) => patch.mutate({ target_duration_s: Number(e.target.value) })}
          >
            {Array.from({ length: 11 }, (_, i) => i + 5).map((n) => (
              <option key={n} value={n}>
                {n}s
              </option>
            ))}
          </select>
          {snippet.estimate_s !== null && <span className="ws-muted"> slice ≈{snippet.estimate_s.toFixed(1)}s</span>}
        </label>
        <div className="ws-field">
          <label title={snippet.is_first ? "Disabled on the first snippet" : undefined}>
            <input
              type="checkbox"
              checked={snippet.continue_from_previous && !snippet.is_first}
              disabled={snippet.is_first || snippet.archived}
              onChange={(e) => patch.mutate({ continue_from_previous: e.target.checked })}
            />{" "}
            Continue from previous
          </label>
          {snippet.stale && (
            <span
              className="badge warn-badge"
              title="The previous snippet's selected take, its trim-out, or the predecessor changed since this snippet's selected take was generated"
            >
              stale
            </span>
          )}
        </div>
        {snippet.blocked_reason && <p className="warn">Generate blocked: {snippet.blocked_reason}</p>}
        <div className="ws-field ws-field-col">
          <span className="ws-label">Script slice (caption source)</span>
          <DraftText value={snippet.script_slice} rows={4} onCommit={(t) => patch.mutate({ script_slice: t })} />
        </div>
      </div>

      <h3>Prompt</h3>
      {built.isError && <p className="warn">{(built.error as Error).message}</p>}
      {built.data &&
        PART_NAMES.map((name) => {
          const part = built.data.parts[name];
          const derived = DERIVED_PARTS.includes(name);
          return (
            <div key={name} className="ws-part">
              <div className="ws-part-head">
                <strong>{PART_LABELS[name]}</strong>
                {derived && (
                  <span
                    className={part.overridden ? "badge" : "badge muted-badge"}
                    title={part.overridden ? "Your text, frozen" : `Rebuilt from the ${PART_SOURCES[name]}`}
                  >
                    {part.overridden ? "overridden" : "inherited"}
                  </span>
                )}
                {part.source_changed && (
                  <span className="badge warn-badge" title={`The ${PART_SOURCES[name]} changed since you overrode this`}>
                    source changed
                  </span>
                )}
                {derived && part.overridden && (
                  <button className="link" onClick={() => putParts.mutate({ [name]: { overridden: false } })}>
                    Reset to inherited
                  </button>
                )}
                <span className="ws-muted">{part.text.length} chars</span>
              </div>
              <DraftText
                value={part.text}
                rows={name === "body" ? 10 : 3}
                placeholder={derived && !part.overridden ? "(empty source)" : undefined}
                onCommit={(text) => putParts.mutate({ [name]: { text, overridden: true } })}
              />
              {name === "body" && <RewriteBody snippetId={id} model={episode.openrouter_model} onApply={(text) => putParts.mutate({ body: { text, overridden: true } })} />}
            </div>
          );
        })}

      {built.data && (
        <div className="ws-joined">
          <div className="ws-part-head">
            <strong>Joined prompt</strong>
            <span className={built.data.char_count > built.data.max_chars ? "warn" : "ws-muted"}>
              {built.data.char_count.toLocaleString()} / {built.data.max_chars.toLocaleString()}
            </span>
          </div>
          {built.data.errors.map((e) => (
            <p key={e} className="warn">
              {e}
            </p>
          ))}
          <pre className="ws-pre">{built.data.joined}</pre>
          <div className="ws-muted">
            Refs ({built.data.refs.filter((r) => r.kind === "image").length} images,{" "}
            {built.data.refs.filter((r) => r.kind === "audio").length} audio):{" "}
            {built.data.refs.map((r) => `${r.kind === "image" ? "🖼" : "🎙"} ${r.label}`).join(" · ") || "none"}
          </div>
        </div>
      )}

      <TakesPanel snippetId={id} />
    </aside>
  );
}

interface DraftProps {
  value: string;
  rows: number;
  placeholder?: string;
  onCommit: (text: string) => void;
}

/** Textarea holding a local draft; commits on blur (or Ctrl+Enter) when changed. */
function DraftText({ value, rows, placeholder, onCommit }: DraftProps) {
  const [draft, setDraft] = useState(value);
  const [editing, setEditing] = useState(false);
  const discard = useRef(false); // Escape: drop the draft instead of committing on blur
  useEffect(() => {
    if (!editing) setDraft(value);
  }, [value, editing]);
  return (
    <textarea
      className={draft !== value ? "ws-draft dirty" : "ws-draft"}
      value={draft}
      rows={rows}
      placeholder={placeholder}
      onFocus={() => setEditing(true)}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={() => {
        setEditing(false);
        if (discard.current) {
          discard.current = false;
          setDraft(value);
        } else if (draft !== value) onCommit(draft);
      }}
      onKeyDown={(e) => {
        if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) e.currentTarget.blur();
        if (e.key === "Escape") {
          discard.current = true;
          e.currentTarget.blur();
        }
      }}
    />
  );
}

interface RewriteProps {
  snippetId: number;
  model: string;
  onApply: (text: string) => void;
}

/** Per-snippet L2: ask the LLM for a new body; preview, then apply or discard. */
function RewriteBody({ snippetId, model, onApply }: RewriteProps) {
  const [open, setOpen] = useState(false);
  const [instructions, setInstructions] = useState("");
  const [modelName, setModelName] = useState(model);
  const rewrite = useMutation({
    mutationFn: () =>
      api.post<{ body: string; cost_usd: number; model: string }>(`/snippets/${snippetId}/rewrite-body`, {
        model: modelName,
        instructions,
      }),
  });
  if (!open) {
    return (
      <button className="link" onClick={() => setOpen(true)}>
        Rewrite body with LLM…
      </button>
    );
  }
  return (
    <div className="ws-rewrite">
      <input value={modelName} onChange={(e) => setModelName(e.target.value)} title="OpenRouter model" />
      <input
        value={instructions}
        onChange={(e) => setInstructions(e.target.value)}
        placeholder="Optional instructions (e.g. more close-ups)"
      />
      <button onClick={() => rewrite.mutate()} disabled={rewrite.isPending}>
        {rewrite.isPending ? "Rewriting…" : "Rewrite"}
      </button>{" "}
      <button className="link" onClick={() => setOpen(false)}>
        cancel
      </button>
      {rewrite.isError && <p className="warn">{rewrite.error.message}</p>}
      {rewrite.data && (
        <div>
          <div className="ws-muted">
            {rewrite.data.model} · {formatUsd(rewrite.data.cost_usd)}
          </div>
          <pre className="ws-pre">{rewrite.data.body}</pre>
          <button
            onClick={() => {
              onApply(rewrite.data.body);
              rewrite.reset();
              setOpen(false);
            }}
          >
            Use this body
          </button>{" "}
          <button onClick={() => rewrite.reset()}>Discard</button>
        </div>
      )}
    </div>
  );
}
