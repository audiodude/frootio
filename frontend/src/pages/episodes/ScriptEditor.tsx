import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api";
import type { LintReport, ScriptVersion, ScriptVersionMeta } from "../../types/workspace";

const LINT_DEBOUNCE_MS = 400;

interface Props {
  episodeId: number;
  onDirtyChange: (dirty: boolean) => void;
}

/** Script editor: explicit Save / Ctrl+S, version picker, lint warnings in a gutter. */
export default function ScriptEditor({ episodeId, onDirtyChange }: Props) {
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
  const [base, setBase] = useState<string | null>(null); // text of the loaded version
  const [lint, setLint] = useState<LintReport | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const gutterRef = useRef<HTMLDivElement>(null);

  // Load the fetched version into the editor (only when nothing unsaved would be lost).
  const loadedKey = script.data ? `${script.data.id ?? "none"}` : null;
  useEffect(() => {
    if (!script.data) return;
    setText(script.data.text);
    setBase(script.data.text);
    setLint(script.data);
  }, [loadedKey]);

  const dirty = base !== null && text !== base;
  useEffect(() => onDirtyChange(dirty), [dirty, onDirtyChange]);

  // Re-lint unsaved text (debounced) against the series cast.
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
      setMessage(saved.created ? `Saved as version ${saved.version}` : "No changes since the latest version");
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
        <select
          value={viewVersion === null ? "latest" : String(viewVersion)}
          onChange={(e) => pickVersion(e.target.value)}
          title="Script versions"
        >
          <option value="latest">latest{latestVersion !== null ? ` (v${latestVersion})` : ""}</option>
          {versions.data?.map((v) => (
            <option key={v.id} value={v.version}>
              v{v.version} · {v.source} · {new Date(v.created_at).toLocaleString()}
            </option>
          ))}
        </select>
        <button onClick={() => saveRef.current()} disabled={!dirty || save.isPending} title="Save (Ctrl+S)">
          {save.isPending ? "Saving…" : "Save"}
        </button>
        {dirty && <span className="warn">unsaved</span>}
      </header>
      {viewingOld && (
        <p className="ws-note">Viewing v{viewVersion}. Saving creates a new version with this text.</p>
      )}
      {script.isError && <p className="warn">{(script.error as Error).message}</p>}
      <div className="ws-editor">
        <div className="ws-gutter" ref={gutterRef} aria-hidden>
          {lines.map((_, i) => {
            const msgs = warningsByLine.get(i + 1);
            return (
              <div key={i} className={msgs ? "ws-gutter-line has-warning" : "ws-gutter-line"} title={msgs?.join("\n")}>
                {msgs ? "⚠" : ""}
                {i + 1}
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
          <span>
            ≈{lint.estimate_s.toFixed(1)}s ({lint.dialogue_lines} dialogue lines, {lint.dialogue_s.toFixed(1)}s dialogue
            + {lint.prose_s.toFixed(1)}s prose)
          </span>
        )}
        {message && <span className="ws-muted">{message}</span>}
      </footer>
      {lint && lint.warnings.length > 0 && (
        <ul className="ws-lint">
          {lint.warnings.map((w) => (
            <li key={`${w.line}-${w.kind}`}>
              <button className="link" onClick={() => jumpToLine(w.line)}>
                line {w.line}
              </button>{" "}
              <span className={w.kind === "unresolved" ? "warn" : "ws-muted"}>{w.message}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
