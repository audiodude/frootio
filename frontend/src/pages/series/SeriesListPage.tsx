import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api, formatUsd } from "../../api";
import type { SeriesDetail, SeriesSummary } from "../../types/library";
import { ErrorLine } from "../characters/LibraryWidgets";

export default function SeriesListPage() {
  const navigate = useNavigate();
  const [showArchived, setShowArchived] = useState(false);
  const [name, setName] = useState("");
  const list = useQuery({
    queryKey: ["series", "list", showArchived],
    queryFn: () => api.get<SeriesSummary[]>(`/series?include_archived=${showArchived}`),
  });
  const create = useMutation({
    mutationFn: () => api.post<SeriesDetail>("/series", { name }),
    onSuccess: (s) => navigate(`/series/${s.id}`),
  });

  return (
    <section className="page series-hub">
      <header className="hub-hero">
        <div className="hub-hero-left">
          <h1 className="hub-title">Series</h1>
          <label className="small muted">
            <input type="checkbox" checked={showArchived} onChange={(e) => setShowArchived(e.target.checked)} /> show
            archived
          </label>
        </div>
        <form
          className="row"
          onSubmit={(e) => {
            e.preventDefault();
            if (name.trim()) create.mutate();
          }}
        >
          <input value={name} placeholder="New series name…" onChange={(e) => setName(e.target.value)} />
          <button type="submit" className="primary" disabled={!name.trim() || create.isPending}>
            + Create Series
          </button>
        </form>
      </header>

      <ErrorLine error={create.error ?? list.error} />

      <div className="ep-cards-grid">
        {list.data?.map((s) => (
          <Link
            key={s.id}
            to={`/series/${s.id}`}
            className={s.archived ? "ep-card series-card archived" : "ep-card series-card"}
          >
            <div className="ep-card-top">
              <strong className="ep-card-title">{s.name}</strong>
              <span className="hub-pill">{formatUsd(s.spend_usd)} spent</span>
            </div>
            {s.style_preamble ? (
              <div className="muted small clamp">{s.style_preamble}</div>
            ) : (
              <div className="muted small">No style preamble set</div>
            )}
            <div className="row spread" style={{ marginTop: "auto" }}>
              <span className="small muted">
                <strong>{s.episode_count}</strong> episodes · <strong>{s.cast_count}</strong> cast ·{" "}
                <strong>{s.location_count}</strong> locations
              </span>
              {s.archived ? <span className="badge">archived</span> : <span className="small accent-text">Open →</span>}
            </div>
          </Link>
        ))}
      </div>

      {list.data?.length === 0 && (
        <div className="ws-empty-board muted">No series yet. Create one above to get started.</div>
      )}
    </section>
  );
}
