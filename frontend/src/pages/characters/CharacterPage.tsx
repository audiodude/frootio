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
  const [rightTab, setRightTab] = useState<"images" | "voice" | "generate">("images");
  const [newAlias, setNewAlias] = useState("");

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

  const c = character.data;
  if (character.error) return <ErrorLine error={character.error} />;
  if (!c) return <p className="muted page">Loading…</p>;

  return (
    <section className="page series-hub">
      <div className="row spread">
        <Link to="/characters" className="small muted">
          ← All Characters
        </Link>
        <div className="row">
          <span className="hub-pill">{formatUsd(c.spend_usd)} spent</span>
          {c.archived && <span className="badge warn-badge">archived</span>}
          <button className="small-btn" onClick={() => patch.mutate({ archived: !c.archived })}>
            {c.archived ? "Unarchive" : "Archive"}
          </button>
        </div>
      </div>

      <div className="asset-split-grid">
        {/* LEFT COLUMN: Character Identity Sheet */}
        <aside className="panel asset-identity-col">
          <div className="asset-identity-top">
            <Thumb image={c.main_image} size={130} />
            <div className="stack grow" style={{ minWidth: 0 }}>
              <h1 className="hub-title">
                <InlineEdit value={c.name} onSave={(name) => patch.mutate({ name })} />
              </h1>
              <span className="muted small">
                slug: {c.slug}
                {c.series.length > 0 && <> · cast in {c.series.map((s) => s.name).join(", ")}</>}
              </span>
              <div className="row wrap">
                <span className={c.main_image ? "badge ok-badge" : "badge muted-badge"}>
                  {c.main_image ? "🖼 Main ref image set" : "No main image"}
                </span>
                <span className={c.voice ? "badge ok-badge" : "badge muted-badge"}>
                  {c.voice ? `🎙 Voice (${c.voice.duration_s.toFixed(1)}s)` : "No voice sample"}
                </span>
              </div>
            </div>
          </div>

          <label className="stack">
            <span className="small muted">Visual Description (used in prompt refs legend)</span>
            <InlineEdit
              multiline
              value={c.description}
              placeholder="Describe the character's appearance, outfit, and key traits…"
              onSave={(description) => patch.mutate({ description })}
            />
          </label>

          <div className="stack">
            <span className="small muted">
              Speaker Aliases (case-insensitive tags matched in scripts)
            </span>
            <div className="row wrap">
              {c.aliases.map((a) => (
                <span key={a} className="chip on">
                  {a}
                  <button
                    className="link"
                    style={{ marginLeft: 4 }}
                    onClick={() => aliases.mutate(c.aliases.filter((x) => x !== a))}
                  >
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
                <input
                  className="small-input"
                  value={newAlias}
                  placeholder="+ Add alias"
                  onChange={(e) => setNewAlias(e.target.value)}
                />
                {newAlias.trim() && (
                  <button type="submit" className="small-btn primary">
                    Add
                  </button>
                )}
              </form>
            </div>
          </div>
          <ErrorLine error={patch.error ?? aliases.error} />
        </aside>

        {/* RIGHT COLUMN: Tabbed Asset Workspace */}
        <div className="asset-workspace-col">
          <nav className="hub-tabs" role="tablist">
            <button
              type="button"
              role="tab"
              aria-selected={rightTab === "images"}
              className={rightTab === "images" ? "hub-tab active" : "hub-tab"}
              onClick={() => setRightTab("images")}
            >
              Reference Images ({c.images.length})
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={rightTab === "voice"}
              className={rightTab === "voice" ? "hub-tab active" : "hub-tab"}
              onClick={() => setRightTab("voice")}
            >
              Voice Sample &amp; Clipper
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={rightTab === "generate"}
              className={rightTab === "generate" ? "hub-tab active" : "hub-tab"}
              onClick={() => setRightTab("generate")}
            >
              ✨ Generate Images (nano-banana)
            </button>
          </nav>

          {rightTab === "images" && (
            <section className="panel">
              <div className="row spread">
                <div>
                  <h2>Character Images</h2>
                  <p className="muted small" style={{ margin: 0 }}>
                    The <strong>main</strong> image is sent as the character's visual reference to H3.
                  </p>
                </div>
                <div className="row">
                  <ImageUpload base={base} invalidate={["characters"]} />
                  <button className="small-btn" onClick={() => setRightTab("generate")}>
                    ✨ Generate new…
                  </button>
                </div>
              </div>
              <ErrorLine error={setMain.error} />
              <ImageGrid
                images={c.images}
                badge={(img) => (img.id === c.main_image_id ? <span className="badge ok-badge">★ Main Ref</span> : null)}
                actions={(img) =>
                  img.id !== c.main_image_id && (
                    <button
                      className="small-btn"
                      disabled={setMain.isPending}
                      onClick={() => setMain.mutate(img.id)}
                    >
                      Set as main
                    </button>
                  )
                }
              />
            </section>
          )}

          {rightTab === "voice" && <VoicePanel character={c} />}

          {rightTab === "generate" && (
            <div className="stack">
              <GeneratePanel base={base} ownImages={c.images} jobsKey={["jobs", "character", id]} />
              <JobList jobsKey={["jobs", "character", id]} path={`${base}/jobs`} kind="image_gen" />
            </div>
          )}
        </div>
      </div>
    </section>
  );
}
