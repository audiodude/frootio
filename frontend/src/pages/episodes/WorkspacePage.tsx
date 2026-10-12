import { useCallback, useEffect, useState } from "react";
import { Link, useBlocker, useParams, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, formatUsd, useServerEvent } from "../../api";
import type { Board, Episode } from "../../types/workspace";
import ScriptEditor from "./ScriptEditor";
import SnippetBoard from "./SnippetBoard";
import SnippetPanel from "./SnippetPanel";
import "./workspace.css";

/** Episode workspace: script editor | snippet board | selected snippet panel. */
export default function WorkspacePage() {
  const episodeId = Number(useParams().id);
  const queryClient = useQueryClient();
  const [params, setParams] = useSearchParams();
  const selectedId = params.get("snippet") ? Number(params.get("snippet")) : null;
  const [scriptDirty, setScriptDirty] = useState(false);

  const episode = useQuery({
    queryKey: ["episode", episodeId],
    queryFn: () => api.get<Episode>(`/episodes/${episodeId}`),
  });
  const board = useQuery({
    queryKey: ["snippets", episodeId],
    queryFn: () => api.get<Board>(`/episodes/${episodeId}/snippets`),
  });
  const patchEpisode = useMutation({
    mutationFn: (body: Partial<Pick<Episode, "default_location_id" | "take_count">>) =>
      api.patch<Episode>(`/episodes/${episodeId}`, body),
    onSuccess: (ep) => queryClient.setQueryData(["episode", episodeId], ep),
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

  if (episode.isError) return <p className="warn page">{(episode.error as Error).message}</p>;
  if (!episode.data || !board.data) return <p className="page">Loading…</p>;
  const ep = episode.data;
  const all = [...board.data.active, ...board.data.archived];
  const selected = all.find((s) => s.id === selectedId) ?? null;
  const activeIndex = board.data.active.findIndex((s) => s.id === selectedId);

  return (
    <div className="ws">
      <header className="ws-top">
        <h1>
          <Link to={`/series/${ep.series_id}`}>{ep.series.name}</Link> · Episode {ep.number}
          {ep.title && ` · ${ep.title}`}
        </h1>
        <label>
          Default location{" "}
          <select
            value={ep.default_location_id ?? ""}
            onChange={(e) =>
              patchEpisode.mutate({ default_location_id: e.target.value === "" ? null : Number(e.target.value) })
            }
          >
            <option value="">(none)</option>
            {ep.locations
              .filter((l) => !l.archived || l.id === ep.default_location_id)
              .map((l) => (
                <option key={l.id} value={l.id}>
                  {l.name}
                </option>
              ))}
          </select>
        </label>
        <label>
          Takes per click{" "}
          <input
            type="number"
            min={1}
            max={8}
            value={ep.take_count}
            onChange={(e) => {
              const n = Number(e.target.value);
              if (n >= 1) patchEpisode.mutate({ take_count: n });
            }}
          />
        </label>
        <span className="ws-muted">
          spent {formatUsd(ep.spend.total_usd)} (video {formatUsd(ep.spend.video_usd)} / cap{" "}
          {formatUsd(ep.spend_cap_usd)})
        </span>
        <Link to={`/episodes/${episodeId}/assembly`}>Assembly →</Link>
        {patchEpisode.isError && <span className="warn">{patchEpisode.error.message}</span>}
      </header>
      <div className={selected ? "ws-grid with-panel" : "ws-grid"}>
        <ScriptEditor episodeId={episodeId} onDirtyChange={setScriptDirty} />
        <SnippetBoard
          episode={ep}
          board={board.data}
          selectedId={selectedId}
          onSelect={select}
          scriptDirty={scriptDirty}
        />
        {selected && (
          <SnippetPanel
            key={selected.id}
            episode={ep}
            snippet={selected}
            index={activeIndex >= 0 ? activeIndex : null}
            onClose={() => select(null)}
          />
        )}
      </div>
    </div>
  );
}
