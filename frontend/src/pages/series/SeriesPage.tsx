import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, formatUsd, mediaUrl } from "../../api";
import type { Character, EpisodeSummary, Location, SeriesDetail } from "../../types/library";
import { ErrorLine, InlineEdit, Thumb } from "../characters/LibraryWidgets";

export default function SeriesPage() {
  const id = Number(useParams().id);
  const qc = useQueryClient();
  const key = ["series", "detail", id];
  const base = `/series/${id}`;
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
  if (!s) return <p className="muted">Loading…</p>;

  return (
    <section className="page">
      <p className="small">
        <Link to="/">← Series</Link>
      </p>
      <div className="row">
        <h1 className="grow">
          <InlineEdit value={s.name} onSave={(name) => patch.mutate({ name })} />
        </h1>
        <span title={Object.entries(s.spend.by_kind).map(([k, v]) => `${k} ${formatUsd(v)}`).join(" · ")}>spent {formatUsd(s.spend_usd)}</span>
        {s.archived && <span className="badge">archived</span>}
        <button onClick={() => patch.mutate({ archived: !s.archived })}>{s.archived ? "Unarchive" : "Archive"}</button>
      </div>
      <span className="muted small">slug {s.slug}</span>
      <label className="stack">
        <span>Style preamble (prompt part 1, inherited by every snippet)</span>
        <InlineEdit
          multiline
          value={s.style_preamble}
          placeholder="e.g. 3D animated cartoon, Pixar style, bright saturated colors…"
          onSave={(style_preamble) => patch.mutate({ style_preamble })}
        />
      </label>
      <ErrorLine error={patch.error} />

      <Episodes series={s} onSaved={onSaved} />
      <Cast series={s} onSaved={onSaved} />
      <Locations series={s} onSaved={onSaved} />
      <Music series={s} onSaved={onSaved} />
    </section>
  );
}

type SectionProps = { series: SeriesDetail; onSaved: (s: SeriesDetail) => void };

function Episodes({ series, onSaved }: SectionProps) {
  const qc = useQueryClient();
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
    onSuccess: async () => {
      setNumber("");
      setTitle("");
      onSaved(await api.get<SeriesDetail>(`/series/${series.id}`));
      void qc.invalidateQueries({ queryKey: ["spend"] });
    },
  });
  const nextNumber = Math.max(0, ...series.episodes.map((e) => e.number)) + 1;

  return (
    <section className="panel">
      <h2>Episodes</h2>
      <table className="table">
        <thead>
          <tr>
            <th>#</th>
            <th>Title</th>
            <th>Default location</th>
            <th>Takes/click</th>
            <th>Script versions</th>
            <th>Snippets</th>
            <th>Spend / cap</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {series.episodes.map((e) => (
            <tr key={e.id}>
              <td>
                <Link to={`/episodes/${e.id}`}>{e.number}</Link>
              </td>
              <td>
                <Link to={`/episodes/${e.id}`}>{e.title || <span className="muted">untitled</span>}</Link>
              </td>
              <td>{e.default_location_name ?? <span className="muted">none</span>}</td>
              <td>{e.take_count}</td>
              <td>{e.script_versions}</td>
              <td>{e.snippets}</td>
              <td>
                {formatUsd(e.spend_usd)} / {formatUsd(e.spend_cap_usd)}
              </td>
              <td>
                <Link to={`/episodes/${e.id}/assembly`}>assembly</Link>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <form
        className="row wrap"
        onSubmit={(e) => {
          e.preventDefault();
          create.mutate();
        }}
      >
        <input
          type="number"
          min={0}
          className="narrow"
          value={number}
          placeholder={String(nextNumber)}
          onChange={(e) => setNumber(e.target.value)}
        />
        <input value={title} placeholder="Title (optional)" onChange={(e) => setTitle(e.target.value)} />
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
        <button type="submit" disabled={create.isPending}>
          Add episode
        </button>
      </form>
      <ErrorLine error={create.error} />
    </section>
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
      <h2>Cast</h2>
      <div className="card-grid">
        {series.cast.map((c) => (
          <div key={c.id} className={c.archived ? "card archived" : "card"}>
            <Link to={`/characters/${c.id}`}>
              <Thumb image={c.main_image} size={110} />
            </Link>
            <Link to={`/characters/${c.id}`}>
              <strong>{c.name}</strong>
            </Link>
            <span className="muted small">{c.aliases.join(", ") || "no aliases"}</span>
            <span className="muted small">{c.voice ? `voice ${c.voice.duration_s.toFixed(1)}s` : "no voice"}</span>
            {c.archived && <span className="badge">archived</span>}
            <button className="small" onClick={() => remove.mutate(c.id)}>
              Remove
            </button>
          </div>
        ))}
      </div>
      <div className="row">
        <select value={pick} onChange={(e) => setPick(e.target.value)}>
          <option value="">Cast a character…</option>
          {available.map((c) => (
            <option key={c.id} value={c.id}>
              {c.name}
            </option>
          ))}
        </select>
        <button disabled={!pick || add.isPending} onClick={() => add.mutate(Number(pick))}>
          Cast
        </button>
        <Link to="/characters" className="small">
          manage characters
        </Link>
      </div>
      <ErrorLine error={add.error ?? remove.error} />
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
      <h2>Locations</h2>
      {series.locations.length === 0 && <p className="muted">No locations in this series.</p>}
      <div className="card-grid">
        {series.locations.map((l) => (
          <div key={l.id} className={l.archived ? "card archived" : "card"}>
            <Link to={`/locations/${l.id}`}>
              <Thumb image={l.ref_images[0] ?? null} size={110} />
            </Link>
            <Link to={`/locations/${l.id}`}>
              <strong>{l.name}</strong>
            </Link>
            <span className="muted small clamp">{l.description || "no description"}</span>
            {l.archived && <span className="badge">archived</span>}
            <button className="small" onClick={() => remove.mutate(l.id)}>
              Remove
            </button>
          </div>
        ))}
      </div>
      <div className="row">
        <select value={pick} onChange={(e) => setPick(e.target.value)}>
          <option value="">Add a location…</option>
          {available.map((l) => (
            <option key={l.id} value={l.id}>
              {l.name}
            </option>
          ))}
        </select>
        <button disabled={!pick || add.isPending} onClick={() => add.mutate(Number(pick))}>
          Add
        </button>
        <Link to="/locations" className="small">
          manage locations
        </Link>
      </div>
      <ErrorLine error={add.error ?? remove.error} />
    </section>
  );
}

function Music({ series, onSaved }: SectionProps) {
  const upload = useMutation({
    mutationFn: (files: FileList) => {
      const form = new FormData();
      for (const f of Array.from(files)) form.append("files", f);
      return api.post(`/series/${series.id}/music`, form);
    },
    onSuccess: async () => onSaved(await api.get<SeriesDetail>(`/series/${series.id}`)),
  });
  return (
    <section className="panel">
      <div className="row spread">
        <h2>Music library</h2>
        <label className="button">
          {upload.isPending ? "Uploading…" : "Upload tracks"}
          <input
            type="file"
            accept="audio/*"
            multiple
            hidden
            onChange={(e) => {
              if (e.target.files?.length) upload.mutate(e.target.files);
              e.target.value = "";
            }}
          />
        </label>
      </div>
      <ErrorLine error={upload.error} />
      {series.music.length === 0 && <p className="muted">No tracks.</p>}
      <ul className="music">
        {series.music.map((t) => (
          <li key={t.id} className="row">
            <strong>{t.name}</strong>
            <span className="muted small">{t.duration_s ? `${t.duration_s.toFixed(1)}s` : ""}</span>
            <audio controls preload="none" src={mediaUrl(t.path)} />
          </li>
        ))}
      </ul>
    </section>
  );
}
