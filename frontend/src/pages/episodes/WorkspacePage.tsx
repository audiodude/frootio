import { useCallback, useEffect, useState } from "react";
import { useBlocker, useParams, useSearchParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, useServerEvent } from "../../api";
import type { Board, Episode } from "../../types/workspace";
import EpisodeHeader from "./EpisodeHeader";
import ScriptEditor from "./ScriptEditor";
import SnippetBoard from "./SnippetBoard";
import SnippetPanel from "./SnippetPanel";
import "./workspace.css";

/** Episode studio workspace: collapsible script | visual storyboard | tabbed scene & takes inspector. */
export default function WorkspacePage() {
  const episodeId = Number(useParams().id);
  const queryClient = useQueryClient();
  const [params, setParams] = useSearchParams();
  const selectedId = params.get("snippet") ? Number(params.get("snippet")) : null;
  const [scriptDirty, setScriptDirty] = useState(false);
  const [scriptCollapsed, setScriptCollapsed] = useState(false);

  const episode = useQuery({
    queryKey: ["episode", episodeId],
    queryFn: () => api.get<Episode>(`/episodes/${episodeId}`),
  });
  const board = useQuery({
    queryKey: ["snippets", episodeId],
    queryFn: () => api.get<Board>(`/episodes/${episodeId}/snippets`),
  });

  useServerEvent((event) => {
    if (event.type === "spend" && event.episode_id === episodeId)
      void queryClient.invalidateQueries({ queryKey: ["episode", episodeId] });
  });

  // Unsaved script: confirm on tab close/reload and on in-app navigation.
  useEffect(() => {
    if (!scriptDirty) return;
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [scriptDirty]);
  const blocker = useBlocker(
    ({ currentLocation, nextLocation }) => scriptDirty && currentLocation.pathname !== nextLocation.pathname,
  );
  useEffect(() => {
    if (blocker.state !== "blocked") return;
    if (window.confirm("The script has unsaved changes. Leave without saving?")) blocker.proceed();
    else blocker.reset();
  }, [blocker]);

  const select = useCallback(
    (id: number | null) =>
      setParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          if (id === null) next.delete("snippet");
          else next.set("snippet", String(id));
          return next;
        },
        { replace: true },
      ),
    [setParams],
  );

  // Auto-select the first active snippet if none is selected and snippets exist
  useEffect(() => {
    const first = board.data?.active[0];
    if (selectedId === null && first) {
      select(first.id);
    }
  }, [selectedId, board.data, select]);

  if (episode.isError) return <p className="warn page">{(episode.error as Error).message}</p>;
  if (!episode.data || !board.data) return <p className="page">Loading…</p>;
  const ep = episode.data;
  const all = [...board.data.active, ...board.data.archived];
  const selected = all.find((s) => s.id === selectedId) ?? null;
  const activeIndex = board.data.active.findIndex((s) => s.id === selectedId);

  const gridClass = [
    "ws-grid",
    selected ? "with-panel" : "",
    scriptCollapsed ? "script-collapsed" : "",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <div className="ws">
      <EpisodeHeader
        episodeId={episodeId}
        episode={ep}
        episodeNumber={ep.number}
        episodeTitle={ep.title}
        mode="workspace"
      />
      <div className={gridClass}>
        {scriptCollapsed ? (
          <aside className="ws-script-rail" onClick={() => setScriptCollapsed(false)} title="Expand Script Editor">
            <button type="button" className="ws-rail-btn">
              ⟫
            </button>
            <span className="ws-rail-label">
              Script {scriptDirty ? "● unsaved" : ""}
            </span>
          </aside>
        ) : (
          <ScriptEditor
            episodeId={episodeId}
            onDirtyChange={setScriptDirty}
            activeSlice={selected?.script_slice ?? null}
            onCollapse={() => setScriptCollapsed(true)}
          />
        )}
        <SnippetBoard
          episode={ep}
          board={board.data}
          selectedId={selectedId}
          onSelect={select}
          scriptDirty={scriptDirty}
        />
        {selected ? (
          <SnippetPanel
            key={selected.id}
            episode={ep}
            snippet={selected}
            index={activeIndex >= 0 ? activeIndex : null}
            onClose={() => select(null)}
          />
        ) : (
          <aside className="ws-panel ws-panel-empty">
            <div className="ws-empty-state">
              <h3>No scene selected</h3>
              <p className="muted">
                Click any scene card in the Storyboard to inspect its cast, action prompt, and 9:16 video takes, or
                split your script to generate scenes automatically.
              </p>
            </div>
          </aside>
        )}
      </div>
    </div>
  );
}
