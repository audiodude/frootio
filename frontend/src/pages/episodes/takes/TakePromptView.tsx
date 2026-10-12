import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../../api";
import { PART_NAMES, type TakePrompt } from "../../../types/takes";
import { errorDetail } from "./format";

/** The exact prompt a take was generated from, with "Load into editor". */
export default function TakePromptView({ takeId, selected }: { takeId: number; selected: boolean }) {
  const queryClient = useQueryClient();
  const prompt = useQuery({
    queryKey: ["take-prompt", takeId], // not under "takes": never invalidated by events
    queryFn: () => api.get<TakePrompt>(`/takes/${takeId}/prompt`),
    staleTime: Infinity, // what was sent never changes
  });
  const load = useMutation({
    mutationFn: (p: TakePrompt) =>
      // Restores all five parts as overridden so the editor reproduces exactly what was sent.
      api.put(`/snippets/${p.snippet_id}/parts`, {
        parts: Object.fromEntries(PART_NAMES.map((name) => [name, { text: p.parts[name].text, overridden: true }])),
      }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["snippets"] }),
  });

  if (prompt.isLoading) return <div className="muted">Loading prompt…</div>;
  if (prompt.error) return <div className="warn">{errorDetail(prompt.error)}</div>;
  const p = prompt.data;
  if (!p) return null;

  return (
    <details className="take-prompt" open={selected}>
      <summary>
        Prompt of take #{p.take_id}
        {selected && " (selected)"} · {p.char_count.toLocaleString()} chars
      </summary>
      <div className="take-prompt-actions">
        <button
          disabled={load.isPending}
          onClick={() => {
            if (window.confirm("Replace this snippet's five prompt parts with this take's prompt (as overridden)?")) {
              load.mutate(p);
            }
          }}
        >
          {load.isPending ? "Loading…" : "Load into editor"}
        </button>
        {load.isSuccess && <span className="muted">Loaded into the editor.</span>}
        {load.error && <span className="warn">{errorDetail(load.error)}</span>}
      </div>
      {PART_NAMES.map((name) => (
        <div key={name} className="take-prompt-part">
          <div className="part-label">
            {name}
            {(name === "style" || name === "legend" || name === "location") && (
              <span className="muted"> · {p.parts[name].overridden ? "overridden" : "inherited"}</span>
            )}
          </div>
          <pre>{p.parts[name].text || <span className="muted">(empty)</span>}</pre>
        </div>
      ))}
      <div className="part-label">refs</div>
      <ul className="take-refs">
        {p.refs.map((r, i) => (
          <li key={`${r.kind}-${i}`}>
            {r.kind} · {r.label} · <code>{r.path}</code>
            {r.tokens !== undefined && <span className="muted"> · {r.tokens} tokens</span>}
          </li>
        ))}
        {p.start_frame && (
          <li>
            start frame from take #{p.start_frame.take_id} at {p.start_frame.time_s?.toFixed(2)}s ·{" "}
            <a href={p.start_frame.url} target="_blank" rel="noreferrer">
              image
            </a>
          </li>
        )}
      </ul>
      <div className="part-label">joined (sent)</div>
      <pre className="joined">{p.joined}</pre>
    </details>
  );
}
