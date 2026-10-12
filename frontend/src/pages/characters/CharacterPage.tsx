import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, formatUsd } from "../../api";
import type { CharacterDetail } from "../../types/library";
import { ErrorLine, GeneratePanel, ImageGrid, ImageUpload, InlineEdit, JobList, Thumb } from "./LibraryWidgets";
import VoicePanel from "./VoicePanel";

export default function CharacterPage() {
  const id = Number(useParams().id);
  const qc = useQueryClient();
  const key = ["characters", "detail", id];
  const base = `/characters/${id}`;
  const character = useQuery({ queryKey: key, queryFn: () => api.get<CharacterDetail>(base) });
  const patch = useMutation({
    mutationFn: (body: Record<string, unknown>) => api.patch<CharacterDetail>(base, body),
    onSuccess: (c) => {
      qc.setQueryData(key, c);
      void qc.invalidateQueries({ queryKey: ["characters"] });
    },
  });
  const aliases = useMutation({
    mutationFn: (list: string[]) => api.put<CharacterDetail>(`${base}/aliases`, { aliases: list }),
    onSuccess: (c) => qc.setQueryData(key, c),
  });
  const setMain = useMutation({
    mutationFn: (imageId: number) => api.put<CharacterDetail>(`${base}/main-image`, { image_id: imageId }),
    onSuccess: (c) => {
      qc.setQueryData(key, c);
      void qc.invalidateQueries({ queryKey: ["characters"] });
    },
  });
  const [newAlias, setNewAlias] = useState("");

  const c = character.data;
  if (character.error) return <ErrorLine error={character.error} />;
  if (!c) return <p className="muted">Loading…</p>;

  return (
    <section className="page">
      <p className="small">
        <Link to="/characters">← Characters</Link>
      </p>
      <div className="row top">
        <Thumb image={c.main_image} size={220} />
        <div className="grow stack">
          <div className="row">
            <h1 className="grow">
              <InlineEdit value={c.name} onSave={(name) => patch.mutate({ name })} />
            </h1>
            {c.archived && <span className="badge">archived</span>}
            <button onClick={() => patch.mutate({ archived: !c.archived })}>{c.archived ? "Unarchive" : "Archive"}</button>
          </div>
          <span className="muted small">
            slug {c.slug} · spent {formatUsd(c.spend_usd)}
            {c.series.length > 0 && <> · cast in {c.series.map((s) => s.name).join(", ")}</>}
          </span>
          <label className="stack">
            <span>Description (used in the refs legend)</span>
            <InlineEdit multiline value={c.description} onSave={(description) => patch.mutate({ description })} />
          </label>
          <div className="stack">
            <span>
              Aliases <span className="muted small">(speaker tags; the name always matches, case-insensitive)</span>
            </span>
            <div className="row wrap">
              {c.aliases.map((a) => (
                <span key={a} className="chip">
                  {a}
                  <button className="link" onClick={() => aliases.mutate(c.aliases.filter((x) => x !== a))}>
                    ×
                  </button>
                </span>
              ))}
              <form
                className="row"
                onSubmit={(e) => {
                  e.preventDefault();
                  if (!newAlias.trim()) return;
                  aliases.mutate([...c.aliases, newAlias], { onSuccess: () => setNewAlias("") });
                }}
              >
                <input value={newAlias} placeholder="Add alias" onChange={(e) => setNewAlias(e.target.value)} />
                <button type="submit" disabled={!newAlias.trim()}>
                  Add
                </button>
              </form>
            </div>
          </div>
          <ErrorLine error={patch.error ?? aliases.error} />
        </div>
      </div>

      <VoicePanel character={c} />

      <section className="panel">
        <div className="row spread">
          <h2>Images</h2>
          <ImageUpload base={base} invalidate={["characters"]} />
        </div>
        <p className="muted small">The main image is the only one sent to the video model; the rest are library only.</p>
        <ErrorLine error={setMain.error} />
        <ImageGrid
          images={c.images}
          badge={(img) => (img.id === c.main_image_id ? <span className="badge">main</span> : null)}
          actions={(img) =>
            img.id !== c.main_image_id && (
              <button disabled={setMain.isPending} onClick={() => setMain.mutate(img.id)}>
                Set as main
              </button>
            )
          }
        />
        <GeneratePanel
          base={base}
          ownImages={c.images}
          jobsKey={["jobs", "character", id]}
        />
        <JobList jobsKey={["jobs", "character", id]} path={`${base}/jobs`} kind="image_gen" />
      </section>
    </section>
  );
}
