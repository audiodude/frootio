// Voice sample panel: current sample + history, and the three sources
// (upload, clip from a take's vocals stem, Chatterbox HD), all landing in the clipper when >5s.

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, formatUsd, mediaUrl } from "../../api";
import type {
  CharacterDetail,
  GenerationOptions,
  GenJob,
  Queued,
  VocalTake,
  VoiceIngest,
  VoiceSample,
} from "../../types/library";
import VoiceClipper, { type ClipRange } from "./VoiceClipper";
import { ErrorLine, JobList } from "./LibraryWidgets";

type ClipTarget = { kind: "source"; path: string; title: string } | { kind: "take"; take: VocalTake };

function describe(v: VoiceSample): string {
  switch (v.source) {
    case "chatterbox":
      return `Chatterbox ${v.tts_voice ?? ""}: “${v.tts_text ?? ""}”`;
    case "take_clip":
      return `take #${v.source_take_id} vocals ${v.clip_start_s?.toFixed(2)}–${v.clip_end_s?.toFixed(2)}s`;
    case "import":
      return "imported";
    default:
      return v.clip_start_s !== null ? `upload ${v.clip_start_s.toFixed(2)}–${v.clip_end_s?.toFixed(2)}s` : "upload";
  }
}

export default function VoicePanel({ character }: { character: CharacterDetail }) {
  const qc = useQueryClient();
  const base = `/characters/${character.id}`;
  const jobsKey = ["jobs", "character", character.id];
  const refresh = () => qc.invalidateQueries({ queryKey: ["characters"] });
  const [clip, setClip] = useState<ClipTarget | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [preset, setPreset] = useState("");
  const [line, setLine] = useState("");
  const [showTakes, setShowTakes] = useState(false);

  const presets = useQuery({
    queryKey: ["generation-options"],
    queryFn: () => api.get<GenerationOptions>("/characters/generation-options"),
  });
  const takes = useQuery({
    queryKey: ["takes", "vocals"],
    queryFn: () => api.get<VocalTake[]>("/characters/voice/takes"),
    enabled: showTakes,
  });
  const jobs = useQuery({ queryKey: jobsKey, queryFn: () => api.get<GenJob[]>(`${base}/jobs`) });

  const upload = useMutation({
    mutationFn: (file: File) => {
      const form = new FormData();
      form.append("file", file);
      return api.post<VoiceIngest>(`${base}/voice/upload`, form);
    },
    onSuccess: (res, file) => {
      void refresh();
      if (res.needs_clip && res.source) {
        setClip({ kind: "source", path: res.source.path, title: `${file.name} (${res.source.duration_s.toFixed(1)}s)` });
        setNotice("The upload is longer than 5s: pick a range.");
      } else {
        setNotice("Voice sample saved.");
      }
    },
  });
  const save = useMutation({
    mutationFn: ({ target, range }: { target: ClipTarget; range: ClipRange }) =>
      api.post(`${base}/voice/clip`, {
        start_s: Number(range.start.toFixed(3)),
        end_s: Number(range.end.toFixed(3)),
        ...(target.kind === "take" ? { take_id: target.take.id } : { source_path: target.path }),
      }),
    onSuccess: () => {
      void refresh();
      setClip(null);
      setNotice("Voice sample saved.");
    },
  });
  const pointer = useMutation({
    mutationFn: (voiceId: number | null) => api.put(`${base}/voice`, { voice_id: voiceId }),
    onSuccess: () => void refresh(),
  });
  const tts = useMutation({
    mutationFn: () => api.post<Queued>(`${base}/voice/generate`, { voice: preset, text: line }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: jobsKey });
      setNotice("Chatterbox job queued.");
    },
  });

  const rate = presets.data?.usd_per_1k_chars ?? 0;
  const pendingClips = (jobs.data ?? []).filter(
    (j) => j.kind === "tts_gen" && j.status === "done" && j.result?.needs_clip === true,
  );

  return (
    <section className="panel">
      <h2>Voice sample</h2>
      <p className="muted small">2–5s. Sent as the character's audio ref; without one the character is described by text only.</p>
      {character.voice ? (
        <div className="row">
          <audio controls src={mediaUrl(character.voice.path)} />
          <span>
            {character.voice.duration_s.toFixed(2)}s · {describe(character.voice)}
          </span>
          <button onClick={() => pointer.mutate(null)}>Clear</button>
        </div>
      ) : (
        <p className="muted">No voice sample.</p>
      )}
      {notice && <p className="ok small">{notice}</p>}

      <div className="row">
        <label className="button">
          {upload.isPending ? "Uploading…" : "Upload audio"}
          <input
            type="file"
            accept="audio/*,video/*"
            hidden
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) upload.mutate(f);
              e.target.value = "";
            }}
          />
        </label>
        <button onClick={() => setShowTakes((v) => !v)}>{showTakes ? "Hide takes" : "Clip from a take…"}</button>
      </div>
      <ErrorLine error={upload.error ?? pointer.error} />

      {showTakes && (
        <div className="take-picker">
          {takes.isLoading && <p className="muted">Loading takes…</p>}
          {takes.data?.length === 0 && <p className="muted">No takes with a vocals stem yet.</p>}
          {takes.data?.map((t) => (
            <button
              key={t.id}
              className="take-option"
              onClick={() => {
                setClip({ kind: "take", take: t });
                setNotice(null);
              }}
            >
              {t.poster_path && <img src={mediaUrl(t.poster_path)} alt="" />}
              <span className="small">
                {t.series_name} ep {t.episode_number} · take #{t.id}
              </span>
            </button>
          ))}
        </div>
      )}

      {clip && (
        <VoiceClipper
          key={clip.kind === "take" ? `take-${clip.take.id}` : clip.path}
          path={clip.kind === "take" ? clip.take.vocals_path : clip.path}
          title={clip.kind === "take" ? `Take #${clip.take.id} vocals (ep ${clip.take.episode_number})` : clip.title}
          saving={save.isPending}
          error={save.error}
          onSave={(range) => save.mutate({ target: clip, range })}
          onCancel={() => setClip(null)}
        />
      )}

      <div className="panel inset">
        <h3>Generate with Chatterbox HD</h3>
        <div className="row">
          <select value={preset} onChange={(e) => setPreset(e.target.value)}>
            <option value="">Preset voice…</option>
            {presets.data?.voices.map((v) => (
              <option key={v}>{v}</option>
            ))}
          </select>
          <input
            className="grow"
            value={line}
            maxLength={500}
            placeholder="Test line, about 3–4s when spoken"
            onChange={(e) => setLine(e.target.value)}
          />
          <button disabled={!preset || !line.trim() || tts.isPending} onClick={() => tts.mutate()}>
            Generate · est. {formatUsd((line.length / 1000) * rate)}
          </button>
        </div>
        <ErrorLine error={tts.error} />
        {pendingClips.map((j) => {
          const src = j.result?.source as { path: string; duration_s: number } | undefined;
          if (!src) return null;
          return (
            <p key={j.id} className="small">
              Chatterbox job #{j.id} returned {src.duration_s.toFixed(1)}s.{" "}
              <button
                onClick={() =>
                  setClip({ kind: "source", path: src.path, title: `Chatterbox ${String(j.payload.voice)} (job #${j.id})` })
                }
              >
                Clip it
              </button>
            </p>
          );
        })}
        <JobList jobsKey={jobsKey} path={`${base}/jobs`} kind="tts_gen" />
      </div>

      {character.voices.length > 0 && (
        <details>
          <summary>History ({character.voices.length})</summary>
          <ul className="voice-history">
            {character.voices.map((v) => (
              <li key={v.id} className="row">
                <audio controls preload="none" src={mediaUrl(v.path)} />
                <span className="small">
                  {v.duration_s.toFixed(2)}s · {describe(v)} · {v.created_at.slice(0, 16).replace("T", " ")}
                </span>
                {v.id === character.voice_id ? (
                  <span className="badge">current</span>
                ) : (
                  <button onClick={() => pointer.mutate(v.id)}>Use</button>
                )}
                <button onClick={() => setClip({ kind: "source", path: v.path, title: `Re-clip sample #${v.id}` })}>
                  Re-clip
                </button>
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}
