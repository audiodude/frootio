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
  body: "Body (Action & Camera)",
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

/**
 * Tabbed Inspector for the selected snippet:
 * - Tab 1 ("Scene & Takes"): Cast, Location, Duration, Continue toggle, Body prompt + LLM Rewrite, Script Slice, and 9:16 Takes Grid.
 * - Tab 2 ("Prompt Parts & Refs"): All 5 prompt parts (including inherited Style, Legend, Location, Tail) + Joined Prompt & Ref inspector.
 */
export default function SnippetPanel({ episode, snippet, index, onClose }: Props) {
  const queryClient = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<"scene" | "prompt">("scene");
  const id = snippet.id;
  const promptKey = ["snippets", "prompt", id];
  const built = useQuery({
    queryKey: promptKey,
    queryFn: () => api.get<BuiltPrompt>(`/snippets/${id}/prompt`),
  });

  const patch = useMutation({
    mutationFn: (body: Record<string, unknown>) => api.patch<Snippet>(`/snippets/${id}`, body),
    onSuccess: () => {
      setError(null);
      void queryClient.invalidateQueries({ queryKey: ["snippets"] });
    },
    onError: (err: Error) => setError(err.message),
  });
  const putParts = useMutation({
    mutationFn: (parts: Partial<Record<PartName, { text?: string; overridden: boolean }>>) =>
      api.put<BuiltPrompt>(`/snippets/${id}/parts`, { parts }),
    onSuccess: (data) => {
      setError(null);
      queryClient.setQueryData(promptKey, data);
      void queryClient.invalidateQueries({ queryKey: ["snippets"] });
    },
    onError: (err: Error) => setError(err.message),
  });
  const restore = useMutation({
    mutationFn: () => api.post<Snippet>(`/snippets/${id}/restore`),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["snippets"] }),
    onError: (err: Error) => setError(err.message),
  });

  const castIds = snippet.cast.map((c) => c.id);
  const castChoices = episode.cast.filter((c) => !c.archived || castIds.includes(c.id));
  const locationChoices = episode.locations.filter((l) => !l.archived || l.id === snippet.location_id);

  const overriddenCount = built.data
    ? DERIVED_PARTS.filter((name) => built.data.parts[name]?.overridden).length
    : 0;
  const bodyText = built.data?.parts.body.text ?? snippet.body;

  return (
    <aside className="ws-panel">
      <header className="ws-panel-header">
        <div className="ws-panel-title">
          <h2>
            {index !== null ? `Scene #${index + 1}` : "Archived Scene"}
          </h2>
          <span className="ws-muted small">
            {snippet.origin} · id {id}
          </span>
        </div>

        <div className="ws-inspector-tabs" role="tablist">
          <button
            type="button"
            role="tab"
            aria-selected={activeTab === "scene"}
            className={activeTab === "scene" ? "ws-tab active" : "ws-tab"}
            onClick={() => setActiveTab("scene")}
          >
            Scene &amp; Takes
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={activeTab === "prompt"}
            className={activeTab === "prompt" ? "ws-tab active" : "ws-tab"}
            onClick={() => setActiveTab("prompt")}
          >
            Prompt Parts (5)
            {overriddenCount > 0 && <span className="ws-tab-badge">{overriddenCount}</span>}
          </button>
        </div>

        <button className="small-btn icon-btn" onClick={onClose} title="Close inspector">
          ✕
        </button>
      </header>

      {snippet.archived && (
        <div className="ws-note row spread">
          <span>This scene is archived.</span>
          <button className="small-btn primary" onClick={() => restore.mutate()}>
            Restore to Storyboard
          </button>
        </div>
      )}
      {error && (
        <p className="warn small" onClick={() => setError(null)}>
          {error}
        </p>
      )}
      {snippet.warnings.map((w) => (
        <p key={w} className="warn small">
          ⚠ {w}
        </p>
      ))}
      {snippet.blocked_reason && <p className="warn small">Generate blocked: {snippet.blocked_reason}</p>}

      {activeTab === "scene" ? (
        <div className="ws-tab-content">
          {/* Compact Scene Metadata Box */}
          <div className="ws-meta-box">
            <div className="ws-field">
              <span className="ws-label">Cast</span>
              <div className="ws-chips">
                {castChoices.map((c) => {
                  const pos = castIds.indexOf(c.id);
                  return (
                    <button
                      key={c.id}
                      type="button"
                      className={pos >= 0 ? "chip on" : "chip"}
                      onClick={() =>
                        patch.mutate({ cast_ids: pos >= 0 ? castIds.filter((x) => x !== c.id) : [...castIds, c.id] })
                      }
                      title={pos >= 0 ? `Legend order ${pos + 1}; click to remove` : "Add to cast"}
                    >
                      {pos >= 0 && <span className="chip-n">{pos + 1}</span>}
                      {c.name}
                      {c.has_voice && " 🎙"}
                      {c.archived && " (arch)"}
                    </button>
                  );
                })}
                {castChoices.length === 0 && <span className="ws-muted small">Series has no cast yet.</span>}
              </div>
            </div>

            <div className="ws-meta-row">
              <label className="ws-inline-field">
                <span className="ws-label-inline">Set</span>
                <select
                  value={snippet.location_id ?? ""}
                  onChange={(e) => patch.mutate({ location_id: e.target.value === "" ? null : Number(e.target.value) })}
                >
                  <option value="">(none)</option>
                  {locationChoices.map((l) => (
                    <option key={l.id} value={l.id}>
                      {l.name}
                      {l.ref_image_count ? ` (${l.ref_image_count}🖼)` : ""}
                    </option>
                  ))}
                </select>
              </label>

              <label className="ws-inline-field">
                <span className="ws-label-inline">Dur</span>
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
                {snippet.estimate_s !== null && (
                  <span className="ws-muted small" title="Estimated speech + action duration">
                    ≈{snippet.estimate_s.toFixed(1)}s
                  </span>
                )}
              </label>

              <label
                className="ws-inline-field checkbox"
                title={snippet.is_first ? "Disabled on the first snippet" : "Use previous scene's final frame"}
              >
                <input
                  type="checkbox"
                  checked={snippet.continue_from_previous && !snippet.is_first}
                  disabled={snippet.is_first || snippet.archived}
                  onChange={(e) => patch.mutate({ continue_from_previous: e.target.checked })}
                />
                <span>Continue prev</span>
                {snippet.stale && (
                  <span
                    className="badge warn-badge"
                    title="The previous snippet's selected take or trim-out changed since this snippet's selected take was generated"
                  >
                    stale
                  </span>
                )}
              </label>
            </div>
          </div>

          {/* Primary Scene Action & Camera (Body Prompt) */}
          <div className="ws-section">
            <div className="ws-section-head">
              <strong>Scene Action &amp; Camera (Body)</strong>
              <RewriteBody
                snippetId={id}
                model={episode.openrouter_model}
                onApply={(text) => putParts.mutate({ body: { text, overridden: true } })}
              />
            </div>
            <DraftText
              value={bodyText}
              rows={5}
              placeholder="Describe the camera framing, character motion, expressions, and dialogue delivery…"
              onCommit={(text) => putParts.mutate({ body: { text, overridden: true } })}
            />
          </div>

          {/* Collapsible Script Slice (Caption Source) */}
          <details className="ws-slice-details" open={Boolean(snippet.script_slice.trim())}>
            <summary className="ws-slice-summary">
              <span>Script Slice (Caption Source)</span>
              <span className="ws-muted small">
                {snippet.script_slice.trim()
                  ? `${snippet.script_slice.trim().split("\n").length} lines`
                  : "empty"}
              </span>
            </summary>
            <DraftText
              value={snippet.script_slice}
              rows={3}
              placeholder="Dialogue lines spoken in this scene (used for caption alignment)…"
              onCommit={(t) => patch.mutate({ script_slice: t })}
            />
          </details>

          {/* Takes & 9:16 Video Player */}
          <TakesPanel
            snippetId={id}
            promptSummary={
              built.data
                ? {
                    refsCount: built.data.refs.length,
                    charCount: built.data.char_count,
                    maxChars: built.data.max_chars,
                    onInspectPrompt: () => setActiveTab("prompt"),
                  }
                : undefined
            }
          />
        </div>
      ) : (
        <div className="ws-tab-content">
          <p className="ws-muted small">
            Each H3 take is generated from 5 joined parts. Derived parts (Style, Refs legend, Location) rebuild
            automatically unless you edit &amp; override them here.
          </p>
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
                      <span
                        className="badge warn-badge"
                        title={`The ${PART_SOURCES[name]} changed since you overrode this`}
                      >
                        source changed
                      </span>
                    )}
                    {derived && part.overridden && (
                      <button
                        className="link small"
                        onClick={() => putParts.mutate({ [name]: { overridden: false } })}
                      >
                        Reset to inherited
                      </button>
                    )}
                    <span className="ws-muted small">{part.text.length} chars</span>
                  </div>
                  <DraftText
                    value={part.text}
                    rows={name === "body" ? 6 : 3}
                    placeholder={derived && !part.overridden ? "(empty source)" : undefined}
                    onCommit={(text) => putParts.mutate({ [name]: { text, overridden: true } })}
                  />
                </div>
              );
            })}

          {built.data && (
            <div className="ws-joined">
              <div className="ws-part-head">
                <strong>Joined Prompt &amp; References</strong>
                <span className={built.data.char_count > built.data.max_chars ? "warn small" : "ws-muted small"}>
                  {built.data.char_count.toLocaleString()} / {built.data.max_chars.toLocaleString()} chars
                </span>
              </div>
              {built.data.errors.map((e) => (
                <p key={e} className="warn small">
                  {e}
                </p>
              ))}
              <div className="ws-refs-strip">
                {built.data.refs.map((r, idx) => (
                  <span key={`${r.sha}-${idx}`} className="badge">
                    {r.kind === "image" ? "🖼" : "🎙"} {r.label}
                  </span>
                ))}
                {built.data.refs.length === 0 && <span className="ws-muted small">No media refs attached</span>}
              </div>
              <pre className="ws-pre">{built.data.joined}</pre>
            </div>
          )}
        </div>
      )}
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
  const discard = useRef(false);
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
  const [showModel, setShowModel] = useState(false);
  const rewrite = useMutation({
    mutationFn: () =>
      api.post<{ body: string; cost_usd: number; model: string }>(`/snippets/${snippetId}/rewrite-body`, {
        model: modelName,
        instructions,
      }),
  });
  if (!open) {
    return (
      <button type="button" className="link small" onClick={() => setOpen(true)}>
        ✨ Rewrite with LLM
      </button>
    );
  }
  return (
    <div className="ws-rewrite-popover">
      <div className="row">
        <input
          className="grow"
          value={instructions}
          onChange={(e) => setInstructions(e.target.value)}
          placeholder="Optional direction (e.g. dramatic close-up, faster pacing)…"
        />
        <button
          type="button"
          className="small-btn icon-btn"
          title={`Model: ${modelName}`}
          onClick={() => setShowModel((v) => !v)}
        >
          ⚙
        </button>
        <button className="primary small-btn" onClick={() => rewrite.mutate()} disabled={rewrite.isPending}>
          {rewrite.isPending ? "Rewriting…" : "Rewrite"}
        </button>
        <button type="button" className="link small" onClick={() => setOpen(false)}>
          cancel
        </button>
      </div>
      {showModel && (
        <input
          value={modelName}
          onChange={(e) => setModelName(e.target.value)}
          title="OpenRouter model"
          placeholder="OpenRouter model"
        />
      )}
      {rewrite.isError && <p className="warn small">{rewrite.error.message}</p>}
      {rewrite.data && (
        <div className="ws-rewrite-result">
          <div className="ws-muted small">
            {rewrite.data.model} · {formatUsd(rewrite.data.cost_usd)}
          </div>
          <pre className="ws-pre">{rewrite.data.body}</pre>
          <div className="row">
            <button
              className="primary small-btn"
              onClick={() => {
                onApply(rewrite.data.body);
                rewrite.reset();
                setOpen(false);
              }}
            >
              Use this body
            </button>
            <button className="small-btn" onClick={() => rewrite.reset()}>
              Discard
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
