import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api } from "../../api";
import type { Character, CharacterDetail } from "../../types/library";
import { ErrorLine, Thumb } from "./LibraryWidgets";

export default function CharacterListPage() {
  const navigate = useNavigate();
  const [showArchived, setShowArchived] = useState(false);
  const [name, setName] = useState("");
  const list = useQuery({
    queryKey: ["characters", "list", showArchived],
    queryFn: () => api.get<Character[]>(`/characters?include_archived=${showArchived}`),
  });
  const create = useMutation({
    mutationFn: () => api.post<CharacterDetail>("/characters", { name }),
    onSuccess: (c) => navigate(`/characters/${c.id}`),
  });

  return (
    <section className="page">
      <div className="row spread">
        <h1>Characters</h1>
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
        <input value={name} placeholder="New character name" onChange={(e) => setName(e.target.value)} />
        <button type="submit" disabled={!name.trim() || create.isPending}>
          Create
        </button>
      </form>
      <ErrorLine error={create.error ?? list.error} />
      <div className="card-grid">
        {list.data?.map((c) => (
          <Link key={c.id} to={`/characters/${c.id}`} className={c.archived ? "card archived" : "card"}>
            <Thumb image={c.main_image} size={140} />
            <strong>{c.name}</strong>
            <span className="muted small">
              {c.aliases.length ? c.aliases.join(", ") : "no aliases"}
              {c.voice ? ` · voice ${c.voice.duration_s.toFixed(1)}s` : " · no voice"}
            </span>
            {c.series.length > 0 && <span className="muted small">{c.series.map((s) => s.name).join(", ")}</span>}
            {c.archived && <span className="badge">archived</span>}
          </Link>
        ))}
      </div>
      {list.data?.length === 0 && <p className="muted">No characters yet. Create one or run the import.</p>}
    </section>
  );
}
