import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, formatUsd } from "../../api";
import type { LocationDetail } from "../../types/library";
import { ErrorLine, GeneratePanel, ImageGrid, ImageUpload, InlineEdit, JobList, Thumb } from "../characters/LibraryWidgets";

const MAX_REFS = 3;

export default function LocationPage() {
  const id = Number(useParams().id);
  const qc = useQueryClient();
  const key = ["locations", "detail", id];
  const base = `/locations/${id}`;
  const [rightTab, setRightTab] = useState<"images" | "generate">("images");

  const location = useQuery({ queryKey: key, queryFn: () => api.get<LocationDetail>(base) });
  const onSaved = (loc: LocationDetail) => {
    qc.setQueryData(key, loc);
    void qc.invalidateQueries({ queryKey: ["locations"] });
  };
  const patch = useMutation({
    mutationFn: (body: Record<string, unknown>) => api.patch<LocationDetail>(base, body),
    onSuccess: onSaved,
  });
  const refs = useMutation({
    mutationFn: (imageIds: number[]) => api.put<LocationDetail>(`${base}/refs`, { image_ids: imageIds }),
    onSuccess: onSaved,
  });

  const loc = location.data;
  if (location.error) return <ErrorLine error={location.error} />;
  if (!loc) return <p className="muted page">Loading…</p>;
  const refIds = loc.ref_images.map((r) => r.id);

  return (
    <section className="page series-hub">
      <div className="row spread">
        <Link to="/locations" className="small muted">
          ← All Locations
        </Link>
        <div className="row">
          <span className="hub-pill">{formatUsd(loc.spend_usd)} spent</span>
          {loc.archived && <span className="badge warn-badge">archived</span>}
          <button className="small-btn" onClick={() => patch.mutate({ archived: !loc.archived })}>
            {loc.archived ? "Unarchive" : "Archive"}
          </button>
        </div>
      </div>

      <div className="asset-split-grid">
        {/* LEFT COLUMN: Location Identity & Active Refs */}
        <aside className="panel asset-identity-col">
          <div className="asset-identity-top">
            <Thumb image={loc.ref_images[0] ?? null} size={160} />
            <div className="stack grow">
              <h1 className="hub-title">
                <InlineEdit value={loc.name} onSave={(name) => patch.mutate({ name })} />
              </h1>
              <span className="muted small">
                slug: {loc.slug}
                {loc.series.length > 0 && <> · in {loc.series.map((s) => s.name).join(", ")}</>}
              </span>
              <span className={refIds.length > 0 ? "badge ok-badge" : "badge muted-badge"}>
                {refIds.length} / {MAX_REFS} reference images active
              </span>
            </div>
          </div>

          <label className="stack">
            <span className="small muted">Location Description (prompt Part 3)</span>
            <InlineEdit
              multiline
              value={loc.description}
              placeholder="Describe the environment, lighting, props, and atmosphere…"
              onSave={(description) => patch.mutate({ description })}
            />
          </label>
          <ErrorLine error={patch.error ?? refs.error} />
        </aside>

        {/* RIGHT COLUMN: Tabbed Images / Generator */}
        <div className="asset-workspace-col">
          <nav className="hub-tabs" role="tablist">
            <button
              type="button"
              role="tab"
              aria-selected={rightTab === "images"}
              className={rightTab === "images" ? "hub-tab active" : "hub-tab"}
              onClick={() => setRightTab("images")}
            >
              Location Images ({loc.images.length})
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

          {rightTab === "images" ? (
            <section className="panel">
              <div className="row spread">
                <div>
                  <h2>Location Images</h2>
                  <p className="muted small" style={{ margin: 0 }}>
                    Up to {MAX_REFS} ref images are sent to H3 in order.
                  </p>
                </div>
                <div className="row">
                  <ImageUpload base={base} invalidate={["locations"]} />
                  <button className="small-btn" onClick={() => setRightTab("generate")}>
                    ✨ Generate new…
                  </button>
                </div>
              </div>
              <ImageGrid
                images={loc.images}
                badge={(img) =>
                  refIds.includes(img.id) ? (
                    <span className="badge ok-badge">★ Ref #{refIds.indexOf(img.id) + 1}</span>
                  ) : null
                }
                actions={(img) =>
                  refIds.includes(img.id) ? (
                    <button className="small-btn" onClick={() => refs.mutate(refIds.filter((r) => r !== img.id))}>
                      Remove ref
                    </button>
                  ) : (
                    <button
                      className="small-btn"
                      disabled={refIds.length >= MAX_REFS}
                      onClick={() => refs.mutate([...refIds, img.id])}
                    >
                      Use as ref
                    </button>
                  )
                }
              />
            </section>
          ) : (
            <div className="stack">
              <GeneratePanel base={base} ownImages={loc.images} jobsKey={["jobs", "location", id]} />
              <JobList jobsKey={["jobs", "location", id]} path={`${base}/jobs`} kind="image_gen" />
            </div>
          )}
        </div>
      </div>
    </section>
  );
}
