import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, formatUsd, mediaUrl } from "../../api";
import type { Character, EpisodeSummary, Location, SeriesDetail } from "../../types/library";
import { ErrorLine, InlineEdit, Thumb } from "../characters/LibraryWidgets";

type Tab = "episodes" | "cast" | "locations" | "music";

export default function SeriesPage() {
  const id = Number(useParams().id);
  const qc = useQueryClient();
  const key = ["series", "detail", id];
  const base = `/series/${id}`;
  const [tab, setTab] = useState<Tab>("episodes");
  const [showNewEp, setShowNewEp] = useState(false);

  const series = useQuery({ queryKey: key, queryFn: () => api.get<SeriesDetail>(base) });
  const onSaved = (s: SeriesDetail) => {
    qc.setQueryData(key, s);
    void qc.invalidateQueries({ queryKey: ["series", "list"] });
  };
  const patch = useMutation({
    mutationFn: (body: Record<string, unknown>) => api.patch<SeriesDetail>(base, body),
    onSuccess: onSaved,
  });

  const s = series.data;
  if (series.error) return <ErrorLine error={series.error} />;
  if (!s) return <p className="muted page">Loading…</p>;

  return (
    <section className="page series-hub">
      <header className="hub-hero">
        <div className="hub-hero-left">
          <h1 className="hub-title">
            <InlineEdit value={s.name} onSave={(name) => patch.mutate({ name })} />
          </h1>
          <span
            className="hub-pill"
            title={Object.entries(s.spend.by_kind)
              .map(([k, v]) => `${k} ${formatUsd(v)}`)
              .join(" · ")}
          >
            {formatUsd(s.spend_usd)} spent
          </span>
          <span className="muted small">slug: {s.slug}</span>
          {s.archived && <span className="badge warn-badge">archived</span>}
        </div>
        <div className="row">
          <button
            className="small-btn"
            onClick={() => patch.mutate({ archived: !s.archived })}
          >
            {s.archived ? "Unarchive" : "Archive series"}
          </button>
          <button
            className="primary"
            onClick={() => {
              setTab("episodes");
              setShowNewEp((v) => !v);
            }}
          >
            + New Episode
          </button>
        </div>
      </header>
      <ErrorLine error={patch.error} />

      <nav className="hub-tabs" role="tablist">
        <button
          type="button"
          role="tab"
          aria-selected={tab === "episodes"}
          className={tab === "episodes" ? "hub-tab active" : "hub-tab"}
          onClick={() => setTab("episodes")}
        >
          Episodes ({s.episodes.length})
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === "cast"}
          className={tab === "cast" ? "hub-tab active" : "hub-tab"}
          onClick={() => setTab("cast")}
        >
          Cast &amp; Voices ({s.cast.length})
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === "locations"}
          className={tab === "locations" ? "hub-tab active" : "hub-tab"}
          onClick={() => setTab("locations")}
        >
          Locations ({s.locations.length})
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === "music"}
          className={tab === "music" ? "hub-tab active" : "hub-tab"}
          onClick={() => setTab("music")}
        >
          Music &amp; Style Preamble ({s.music.length})
        </button>
      </nav>

      {tab === "episodes" && (
        <>
          <Episodes
            series={s}
            onSaved={onSaved}
            showNew={showNewEp}
            onCloseNew={() => setShowNewEp(false)}
          />
          {s.cast.length > 0 && (
            <div className="hub-cast-strip">
              <div className="row spread">
                <h3 className="small-heading">Active Series Cast</h3>
                <button type="button" className="link small" onClick={() => setTab("cast")}>
                  Manage cast ({s.cast.length}) →
                </button>
              </div>
              <div className="cast-mini-grid">
                {s.cast.map((c) => (
                  <Link key={c.id} to={`/characters/${c.id}`} className="cast-mini-card">
                    <Thumb image={c.main_image} size={56} />
                    <div className="cast-mini-info">
                      <strong>{c.name}</strong>
                      <span className={c.voice ? "ok small" : "muted small"}>
                        {c.voice ? `🎙 Voice ready (${c.voice.duration_s.toFixed(1)}s)` : "No voice sample"}
                      </span>
                      {c.aliases.length > 0 && (
                        <span className="muted small clamp">Aliases: {c.aliases.join(", ")}</span>
                      )}
                    </div>
                  </Link>
                ))}
              </div>
            </div>
          )}
        </>
      )}

      {tab === "cast" && <Cast series={s} onSaved={onSaved} />}
      {tab === "locations" && <Locations series={s} onSaved={onSaved} />}
      {tab === "music" && (
        <div className="stack">
          <section className="panel">
            <h2>Style Preamble (Prompt Part 1)</h2>
            <p className="muted small">
              Inherited by every scene in this series unless overridden on a specific snippet.
            </p>
            <InlineEdit
              multiline
              value={s.style_preamble}
              placeholder="e.g. 3D animated cartoon, Pixar style, bright saturated colors…"
              onSave={(style_preamble) => patch.mutate({ style_preamble })}
            />
          </section>
          <Music series={s} onSaved={onSaved} />
        </div>
      )}
    </section>
  );
}

type SectionProps = { series: SeriesDetail; onSaved: (s: SeriesDetail) => void };

function Episodes({
  series,
  onSaved,
  showNew,
  onCloseNew,
}: SectionProps & { showNew: boolean; onCloseNew: () => void }) {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [number, setNumber] = useState("");
  const [title, setTitle] = useState("");
  const [locationId, setLocationId] = useState("");
  const [takeCount, setTakeCount] = useState(1);
  const [cap, setCap] = useState(20);

  const create = useMutation({
    mutationFn: () =>
      api.post<EpisodeSummary>(`/series/${series.id}/episodes`, {
        number: number ? Number(number) : null,
        title,
        default_location_id: locationId ? Number(locationId) : null,
        take_count: takeCount,
        spend_cap_usd: cap,
      }),
    onSuccess: async (ep) => {
      setNumber("");
      setTitle("");
      onCloseNew();
      onSaved(await api.get<SeriesDetail>(`/series/${series.id}`));
      void qc.invalidateQueries({ queryKey: ["spend"] });
      navigate(`/episodes/${ep.id}`);
    },
  });
  const nextNumber = Math.max(0, ...series.episodes.map((e) => e.number)) + 1;

  return (
    <div className="hub-episodes">
      {showNew && (
        <form
          className="panel new-ep-form"
          onSubmit={(e) => {
            e.preventDefault();
            create.mutate();
          }}
        >
          <div className="row spread">
            <strong>Create Episode</strong>
            <button type="button" className="link small muted" onClick={onCloseNew}>
              cancel
            </button>
          </div>
          <div className="row wrap">
            <label>
              Ep #{" "}
              <input
                type="number"
                min={0}
                className="narrow"
                value={number}
                placeholder={String(nextNumber)}
                onChange={(e) => setNumber(e.target.value)}
              />
            </label>
            <input
              className="grow"
              autoFocus
              value={title}
              placeholder="Episode title (e.g. The Oven Is On Fire)"
              onChange={(e) => setTitle(e.target.value)}
            />
            <select value={locationId} onChange={(e) => setLocationId(e.target.value)}>
              <option value="">No default location</option>
              {series.locations
                .filter((l) => !l.archived)
                .map((l) => (
                  <option key={l.id} value={l.id}>
                    {l.name}
                  </option>
                ))}
            </select>
            <label>
              Takes/click{" "}
              <input
                type="number"
                min={1}
                className="narrow"
                value={takeCount}
                onChange={(e) => setTakeCount(Math.max(1, Number(e.target.value)))}
              />
            </label>
            <label>
              Cap ${" "}
              <input
                type="number"
                min={1}
                step={1}
                className="narrow"
                value={cap}
                onChange={(e) => setCap(Number(e.target.value))}
              />
            </label>
            <button type="submit" className="primary" disabled={create.isPending}>
              {create.isPending ? "Creating…" : "Create & Open →"}
            </button>
          </div>
          <ErrorLine error={create.error} />
        </form>
      )}

      {series.episodes.length === 0 ? (
        <div className="ws-empty-board muted">
          No episodes yet. Click “+ New Episode” above to start writing.
        </div>
      ) : (
        <div className="ep-cards-grid">
          {series.episodes.map((e) => {
            const pct = Math.min(100, Math.round((e.spend_usd / Math.max(0.01, e.spend_cap_usd)) * 100));
            const selCount = e.selected_snippets ?? 0;
            const exported = Boolean(e.latest_export_url);
            return (
              <article key={e.id} className="ep-card">
                <div className="ep-card-top">
                  <Link to={`/episodes/${e.id}`} className="ep-card-title">
                    Ep {e.number} · {e.title || <span className="muted">Untitled</span>}
                  </Link>
                  {exported ? (
                    <span className="badge ok-badge">✓ Exported</span>
                  ) : e.snippets > 0 ? (
                    <span className={selCount === e.snippets ? "badge ok-badge" : "badge warn-badge"}>
                      {selCount}/{e.snippets} Takes
                    </span>
                  ) : (
                    <span className="badge muted-badge">Draft</span>
                  )}
                </div>

                <div className="muted small">
                  Default set: {e.default_location_name ?? "none"} · {e.snippets} scenes · Script v{e.script_versions}
                </div>

                <div className="ep-card-foot">
                  <div className="ep-card-spend">
                    <span className="small muted">
                      Spend: <strong>{formatUsd(e.spend_usd)}</strong> / {formatUsd(e.spend_cap_usd)} cap
                    </span>
                    <div className="ep-spend-track">
                      <div
                        className={pct >= 90 ? "ep-spend-fill warn" : "ep-spend-fill"}
                        style={{ width: `${pct}%` }}
                      />
                    </div>
                  </div>
                  <div className="ep-card-btns">
                    <Link to={`/episodes/${e.id}`} className="button small-btn">
                      Storyboard
                    </Link>
                    <Link to={`/episodes/${e.id}/assembly`} className="button small-btn accent-outline">
                      Assembly →
                    </Link>
                    {e.latest_export_url && (
                      <a
                        href={e.latest_export_url}
                        target="_blank"
                        rel="noreferrer"
                        className="button small-btn"
                        title="Download latest exported MP4"
                      >
                        ⬇ MP4
                      </a>
                    )}
                  </div>
                </div>
              </article>
            );
          })}
        </div>
      )}
    </div>
  );
}

function Cast({ series, onSaved }: SectionProps) {
  const [pick, setPick] = useState("");
  const all = useQuery({ queryKey: ["characters", "list", false], queryFn: () => api.get<Character[]>("/characters") });
  const add = useMutation({
    mutationFn: (cid: number) => api.put<SeriesDetail>(`/series/${series.id}/cast/${cid}`),
    onSuccess: (s) => {
      setPick("");
      onSaved(s);
    },
  });
  const remove = useMutation({
    mutationFn: (cid: number) => api.delete<SeriesDetail>(`/series/${series.id}/cast/${cid}`),
    onSuccess: onSaved,
  });
  const castIds = new Set(series.cast.map((c) => c.id));
  const available = (all.data ?? []).filter((c) => !castIds.has(c.id));

  return (
    <section className="panel">
      <div className="row spread">
        <h2>Series Cast</h2>
        <div className="row">
          <select value={pick} onChange={(e) => setPick(e.target.value)}>
            <option value="">Add existing character…</option>
            {available.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
          </select>
          <button className="primary small-btn" disabled={!pick || add.isPending} onClick={() => add.mutate(Number(pick))}>
            Add to Cast
          </button>
          <Link to="/characters" className="button small-btn">
            Character Library →
          </Link>
        </div>
      </div>
      <ErrorLine error={add.error ?? remove.error} />
      {series.cast.length === 0 && <p className="muted">No characters cast in this series yet.</p>}
      <div className="card-grid">
        {series.cast.map((c) => (
          <div key={c.id} className={c.archived ? "card archived" : "card"}>
            <Link to={`/characters/${c.id}`}>
              <Thumb image={c.main_image} size={130} />
            </Link>
            <Link to={`/characters/${c.id}`}>
              <strong>{c.name}</strong>
            </Link>
            <span className="muted small">{c.aliases.join(", ") || "no aliases"}</span>
            <span className={c.voice ? "ok small" : "muted small"}>
              {c.voice ? `🎙 voice ${c.voice.duration_s.toFixed(1)}s` : "no voice"}
            </span>
            {c.archived && <span className="badge">archived</span>}
            <button className="small-btn" onClick={() => remove.mutate(c.id)}>
              Remove from series
            </button>
          </div>
        ))}
      </div>
    </section>
  );
}

function Locations({ series, onSaved }: SectionProps) {
  const [pick, setPick] = useState("");
  const all = useQuery({ queryKey: ["locations", "list", false], queryFn: () => api.get<Location[]>("/locations") });
  const add = useMutation({
    mutationFn: (lid: number) => api.put<SeriesDetail>(`/series/${series.id}/locations/${lid}`),
    onSuccess: (s) => {
      setPick("");
      onSaved(s);
    },
  });
  const remove = useMutation({
    mutationFn: (lid: number) => api.delete<SeriesDetail>(`/series/${series.id}/locations/${lid}`),
    onSuccess: onSaved,
  });
  const ids = new Set(series.locations.map((l) => l.id));
  const available = (all.data ?? []).filter((l) => !ids.has(l.id));

  return (
    <section className="panel">
      <div className="row spread">
        <h2>Series Locations</h2>
        <div className="row">
          <select value={pick} onChange={(e) => setPick(e.target.value)}>
            <option value="">Add existing location…</option>
            {available.map((l) => (
              <option key={l.id} value={l.id}>
                {l.name}
              </option>
            ))}
          </select>
          <button className="primary small-btn" disabled={!pick || add.isPending} onClick={() => add.mutate(Number(pick))}>
            Add Location
          </button>
          <Link to="/locations" className="button small-btn">
            Location Library →
          </Link>
        </div>
      </div>
      <ErrorLine error={add.error ?? remove.error} />
      {series.locations.length === 0 && <p className="muted">No locations in this series.</p>}
      <div className="card-grid">
        {series.locations.map((l) => (
          <div key={l.id} className={l.archived ? "card archived" : "card"}>
            <Link to={`/locations/${l.id}`}>
              <Thumb image={l.ref_images[0] ?? null} size={130} />
            </Link>
            <Link to={`/locations/${l.id}`}>
              <strong>{l.name}</strong>
            </Link>
            <span className="muted small clamp">{l.description || "no description"}</span>
            {l.archived && <span className="badge">archived</span>}
            <button className="small-btn" onClick={() => remove.mutate(l.id)}>
              Remove from series
            </button>
          </div>
        ))}
      </div>
    </section>
  );
}

function Music({ series, onSaved }: SectionProps) {
  const upload = useMutation({
    mutationFn: (files: File[]) => {
      const form = new FormData();
      for (const f of files) form.append("files", f);
      return api.post(`/series/${series.id}/music`, form);
    },
    onSuccess: async () => onSaved(await api.get<SeriesDetail>(`/series/${series.id}`)),
  });
  return (
    <section className="panel">
      <div className="row spread">
        <h2>Music Library ({series.music.length})</h2>
        <label className="button small-btn">
          {upload.isPending ? "Uploading…" : "+ Upload audio tracks"}
          <input
            type="file"
            accept="audio/*"
            multiple
            hidden
            onChange={(e) => {
              const picked = e.target.files ? Array.from(e.target.files) : [];
              e.target.value = "";
              if (picked.length) upload.mutate(picked);
            }}
          />
        </label>
      </div>
      <ErrorLine error={upload.error} />
      {series.music.length === 0 && <p className="muted">No music tracks uploaded yet.</p>}
      <ul className="music">
        {series.music.map((t) => (
          <li key={t.id} className="row spread music-row">
            <span>
              <strong>{t.name}</strong>{" "}
              <span className="muted small">{t.duration_s ? `(${t.duration_s.toFixed(1)}s)` : ""}</span>
            </span>
            <audio controls preload="none" src={mediaUrl(t.path)} />
          </li>
        ))}
      </ul>
    </section>
  );
}
