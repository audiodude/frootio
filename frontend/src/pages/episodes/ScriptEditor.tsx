import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api";
import type { LintReport, ScriptVersion, ScriptVersionMeta } from "../../types/workspace";

const LINT_DEBOUNCE_MS = 400;

interface Props {
  episodeId: number;
  onDirtyChange: (dirty: boolean) => void;
  activeSlice?: string | null;
  onCollapse?: () => void;
}

/** Script editor: explicit Save / Ctrl+S, version picker, lint warnings in a gutter, active snippet highlight. */
export default function ScriptEditor({ episodeId, onDirtyChange, activeSlice, onCollapse }: Props) {
  const queryClient = useQueryClient();
  const [viewVersion, setViewVersion] = useState<number | null>(null); // null = latest
  const script = useQuery({
    queryKey: ["script", episodeId, viewVersion],
    queryFn: () =>
      api.get<ScriptVersion>(
        `/episodes/${episodeId}/script${viewVersion === null ? "" : `?version=${viewVersion}`}`,
      ),
  });
  const versions = useQuery({
    queryKey: ["script-versions", episodeId],
    queryFn: () => api.get<ScriptVersionMeta[]>(`/episodes/${episodeId}/script/versions`),
  });

  const [text, setText] = useState<string>("");
  const [base, setBase] = useState<string | null>(null);
  const [lint, setLint] = useState<LintReport | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const gutterRef = useRef<HTMLDivElement>(null);

  const loadedKey = script.data ? `${script.data.id ?? "none"}` : null;
  useEffect(() => {
    if (!script.data) return;
    setText(script.data.text);
    setBase(script.data.text);
    setLint(script.data);
  }, [loadedKey]);

  const dirty = base !== null && text !== base;
  useEffect(() => onDirtyChange(dirty), [dirty, onDirtyChange]);

  useEffect(() => {
    if (base === null || text === base) return;
    const handle = window.setTimeout(() => {
      api
        .post<LintReport>(`/episodes/${episodeId}/script/lint`, { text })
        .then(setLint)
        .catch(() => undefined);
    }, LINT_DEBOUNCE_MS);
    return () => window.clearTimeout(handle);
  }, [text, base, episodeId]);

  const save = useMutation({
    mutationFn: () => api.post<ScriptVersion>(`/episodes/${episodeId}/script`, { text }),
    onSuccess: (saved) => {
      setBase(saved.text);
      setLint(saved);
      setViewVersion(null);
      setMessage(saved.created ? `Saved v${saved.version}` : "Up to date");
      queryClient.setQueryData(["script", episodeId, null], saved);
      void queryClient.invalidateQueries({ queryKey: ["script-versions", episodeId] });
    },
    onError: (err: Error) => setMessage(`Save failed: ${err.message}`),
  });

  const saveRef = useRef(() => {});
  saveRef.current = () => {
    if (!save.isPending) save.mutate();
  };
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
        e.preventDefault();
        saveRef.current();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const lines = useMemo(() => text.split("\n"), [text]);
  const warningsByLine = useMemo(() => {
    const map = new Map<number, string[]>();
    for (const w of lint?.warnings ?? []) map.set(w.line, [...(map.get(w.line) ?? []), w.message]);
    return map;
  }, [lint]);

  // Identify which lines belong to the currently selected snippet's script_slice
  // Identify contiguous line span belonging to the currently selected snippet's script_slice
  const activeLines = useMemo(() => {
    const set = new Set<number>();
    if (!activeSlice || !activeSlice.trim() || !text) return set;
    const sliceLines = activeSlice
      .split("\n")
      .map((l) => l.trim())
      .filter(Boolean);
    if (sliceLines.length === 0) return set;
    const matched: number[] = [];
    lines.forEach((line, idx) => {
      const trimmed = line.trim();
      if (trimmed && sliceLines.includes(trimmed)) {
        matched.push(idx + 1);
      }
    });
    const first = matched[0];
    const last = matched[matched.length - 1];
    if (first !== undefined && last !== undefined) {
      for (let ln = first; ln <= last; ln++) set.add(ln);
    }
    return set;
  }, [activeSlice, lines, text]);
  const pickVersion = (value: string) => {
    if (dirty && !window.confirm("Discard unsaved script changes?")) return;
    setViewVersion(value === "latest" ? null : Number(value));
  };

  const jumpToLine = (line: number) => {
    const ta = textareaRef.current;
    if (!ta) return;
    const offset = lines.slice(0, line - 1).reduce((n, l) => n + l.length + 1, 0);
    ta.focus();
    ta.setSelectionRange(offset, offset + (lines[line - 1]?.length ?? 0));
    const lineHeight = parseFloat(getComputedStyle(ta).lineHeight) || 20;
    ta.scrollTop = Math.max(0, (line - 4) * lineHeight);
  };

  const latestVersion = versions.data?.[0]?.version ?? null;
  const viewingOld = viewVersion !== null && viewVersion !== latestVersion;

  return (
    <section className="ws-script">
      <header className="ws-head">
        <h2>Script</h2>
        {dirty ? (
          <span className="ws-status-dot warn" title="Unsaved changes">
            ● unsaved
          </span>
        ) : (
          latestVersion !== null && (
            <span className="ws-status-dot ok" title="Saved">
              ● v{latestVersion}
            </span>
          )
        )}
        <select
          className="ws-version-select"
          value={viewVersion === null ? "latest" : String(viewVersion)}
          onChange={(e) => pickVersion(e.target.value)}
          title="Script version history"
        >
          <option value="latest">latest{latestVersion !== null ? ` (v${latestVersion})` : ""}</option>
          {versions.data?.map((v) => (
            <option key={v.id} value={v.version}>
              v{v.version} · {v.source}
            </option>
          ))}
        </select>
        <button
          className={dirty ? "primary small-btn" : "small-btn"}
          onClick={() => saveRef.current()}
          disabled={!dirty || save.isPending}
          title="Save script (Ctrl+S)"
        >
          {save.isPending ? "Saving…" : "Save"}
        </button>
        {onCollapse && (
          <button
            type="button"
            className="small-btn icon-btn"
            onClick={onCollapse}
            title="Collapse Script drawer to give Storyboard more room"
          >
            ⟪ Hide
          </button>
        )}
      </header>
      {viewingOld && (
        <p className="ws-note">Viewing v{viewVersion}. Saving creates a new version with this text.</p>
      )}
      {script.isError && <p className="warn">{(script.error as Error).message}</p>}
      <div className="ws-editor">
        <div className="ws-gutter" ref={gutterRef} aria-hidden>
          {lines.map((_, i) => {
            const lineNum = i + 1;
            const msgs = warningsByLine.get(lineNum);
            const isActive = activeLines.has(lineNum);
            return (
              <div
                key={i}
                className={[
                  "ws-gutter-line",
                  msgs ? "has-warning" : "",
                  isActive ? "in-active-slice" : "",
                ]
                  .filter(Boolean)
                  .join(" ")}
                title={msgs ? msgs.join("\n") : isActive ? "In selected scene" : undefined}
              >
                {msgs ? "⚠" : ""}
                {lineNum}
              </div>
            );
          })}
        </div>
        <textarea
          ref={textareaRef}
          value={text}
          onChange={(e) => {
            setText(e.target.value);
            setMessage(null);
          }}
          onScroll={(e) => {
            if (gutterRef.current) gutterRef.current.scrollTop = e.currentTarget.scrollTop;
          }}
          spellCheck={false}
          wrap="off"
          placeholder={"[Speaker]: line with *direction*\nProse lines describe action.\n-----  (hard break)"}
        />
      </div>
      <footer className="ws-script-foot">
        {lint && (
          <span className="ws-muted">
            ≈{lint.estimate_s.toFixed(1)}s · {lint.dialogue_lines} lines ({lint.dialogue_s.toFixed(1)}s speech +{" "}
            {lint.prose_s.toFixed(1)}s action)
          </span>
        )}
        {message && <span className="ok small">{message}</span>}
      </footer>
      {lint && lint.warnings.length > 0 && (
        <ul className="ws-lint">
          {lint.warnings.map((w) => (
            <li key={`${w.line}-${w.kind}`}>
              <button className="link" onClick={() => jumpToLine(w.line)}>
                L{w.line}
              </button>{" "}
              <span className={w.kind === "unresolved" ? "warn" : "ws-muted"}>{w.message}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
