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
    <section className="page">
      <div className="row spread">
        <h1>Series</h1>
        <label className="small">
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
        <input value={name} placeholder="New series name" onChange={(e) => setName(e.target.value)} />
        <button type="submit" disabled={!name.trim() || create.isPending}>
          Create
        </button>
      </form>
      <ErrorLine error={create.error ?? list.error} />
      <table className="table">
        <thead>
          <tr>
            <th>Series</th>
            <th>Episodes</th>
            <th>Cast</th>
            <th>Locations</th>
            <th>Spend</th>
          </tr>
        </thead>
        <tbody>
          {list.data?.map((s) => (
            <tr key={s.id} className={s.archived ? "archived" : undefined}>
              <td>
                <Link to={`/series/${s.id}`}>{s.name}</Link>
                {s.archived && <span className="badge">archived</span>}
              </td>
              <td>{s.episode_count}</td>
              <td>{s.cast_count}</td>
              <td>{s.location_count}</td>
              <td>{formatUsd(s.spend_usd)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {list.data?.length === 0 && (
        <p className="muted">
          No series yet. Create one above to get started.
        </p>
      )}
    </section>
  );
}
