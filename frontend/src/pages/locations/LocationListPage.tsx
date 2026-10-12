import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api } from "../../api";
import type { Location, LocationDetail } from "../../types/library";
import { ErrorLine, Thumb } from "../characters/LibraryWidgets";

export default function LocationListPage() {
  const navigate = useNavigate();
  const [showArchived, setShowArchived] = useState(false);
  const [name, setName] = useState("");
  const list = useQuery({
    queryKey: ["locations", "list", showArchived],
    queryFn: () => api.get<Location[]>(`/locations?include_archived=${showArchived}`),
  });
  const create = useMutation({
    mutationFn: () => api.post<LocationDetail>("/locations", { name }),
    onSuccess: (loc) => navigate(`/locations/${loc.id}`),
  });

  return (
    <section className="page">
      <div className="row spread">
        <h1>Locations</h1>
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
        <input value={name} placeholder="New location name" onChange={(e) => setName(e.target.value)} />
        <button type="submit" disabled={!name.trim() || create.isPending}>
          Create
        </button>
      </form>
      <ErrorLine error={create.error ?? list.error} />
      <div className="card-grid">
        {list.data?.map((loc) => (
          <Link key={loc.id} to={`/locations/${loc.id}`} className={loc.archived ? "card archived" : "card"}>
            <Thumb image={loc.ref_images[0] ?? null} size={140} />
            <strong>{loc.name}</strong>
            <span className="muted small clamp">{loc.description || "no description"}</span>
            <span className="muted small">{loc.ref_images.length} ref image(s)</span>
            {loc.archived && <span className="badge">archived</span>}
          </Link>
        ))}
      </div>
      {list.data?.length === 0 && <p className="muted">No locations yet.</p>}
    </section>
  );
}
