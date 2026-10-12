import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, formatUsd } from "../../api";
import type { LocationDetail } from "../../types/library";
import { ErrorLine, GeneratePanel, ImageGrid, ImageUpload, InlineEdit, JobList } from "../characters/LibraryWidgets";

const MAX_REFS = 3;

export default function LocationPage() {
  const id = Number(useParams().id);
  const qc = useQueryClient();
  const key = ["locations", "detail", id];
  const base = `/locations/${id}`;
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
  if (!loc) return <p className="muted">Loading…</p>;
  const refIds = loc.ref_images.map((r) => r.id);

  return (
    <section className="page">
      <p className="small">
        <Link to="/locations">← Locations</Link>
      </p>
      <div className="row">
        <h1 className="grow">
          <InlineEdit value={loc.name} onSave={(name) => patch.mutate({ name })} />
        </h1>
        {loc.archived && <span className="badge">archived</span>}
        <button onClick={() => patch.mutate({ archived: !loc.archived })}>{loc.archived ? "Unarchive" : "Archive"}</button>
      </div>
      <span className="muted small">
        slug {loc.slug} · spent {formatUsd(loc.spend_usd)}
        {loc.series.length > 0 && <> · in {loc.series.map((s) => s.name).join(", ")}</>}
      </span>
      <label className="stack">
        <span>Description (the prompt's location text)</span>
        <InlineEdit multiline value={loc.description} onSave={(description) => patch.mutate({ description })} />
      </label>
      <ErrorLine error={patch.error ?? refs.error} />

      <section className="panel">
        <div className="row spread">
          <h2>Images</h2>
          <ImageUpload base={base} invalidate={["locations"]} />
        </div>
        <p className="muted small">
          Up to {MAX_REFS} ref images (optional) are sent to the video model, in order. Others stay in the library.
        </p>
        <ImageGrid
          images={loc.images}
          badge={(img) =>
            refIds.includes(img.id) ? <span className="badge">ref {refIds.indexOf(img.id) + 1}</span> : null
          }
          actions={(img) =>
            refIds.includes(img.id) ? (
              <button onClick={() => refs.mutate(refIds.filter((r) => r !== img.id))}>Remove ref</button>
            ) : (
              <button disabled={refIds.length >= MAX_REFS} onClick={() => refs.mutate([...refIds, img.id])}>
                Use as ref
              </button>
            )
          }
        />
        <GeneratePanel base={base} ownImages={loc.images} jobsKey={["jobs", "location", id]} />
        <JobList jobsKey={["jobs", "location", id]} path={`${base}/jobs`} kind="image_gen" />
      </section>
    </section>
  );
}
