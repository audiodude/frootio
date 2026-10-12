import { useEffect, useRef, useState, type RefObject } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, formatUsd } from "../../api";
import type { AssemblyTake, Overdub } from "../../types/assembly";
import type { Episode } from "../../types/workspace";

/** Single dialogue lines generated in a character's cloned voice and placed on this take. */
export default function OverdubPanel({
  take,
  episodeId,
  videoRef,
  currentTime,
}: {
  take: AssemblyTake;
  episodeId: number;
  videoRef: RefObject<HTMLVideoElement | null>;
  currentTime: number;
}) {
  const queryClient = useQueryClient();
  const episode = useQuery({
    queryKey: ["episode", episodeId],
    queryFn: () => api.get<Episode>(`/episodes/${episodeId}`),
  });
  const cast = (episode.data?.cast ?? []).filter((c) => !c.archived);
  const [characterId, setCharacterId] = useState<number | null>(null);
  const [text, setText] = useState("");
  const [exaggeration, setExaggeration] = useState(0.5);
  const chosen = cast.find((c) => c.id === characterId) ?? null;

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ["assembly", episodeId] });
  const create = useMutation({
    mutationFn: () =>
      api.post<Overdub>(`/takes/${take.id}/overdubs`, {
        character_id: characterId,
        text,
        exaggeration,
        offset_s: Number(currentTime.toFixed(3)),
      }),
    onSuccess: () => {
      setText("");
      void invalidate();
    },
  });

  return (
    <div className="overdubs">
      <div className="ws-label small">Fix-in-post lines</div>
      {take.overdubs.map((o) => (
        <OverdubRow key={o.id} o={o} videoRef={videoRef} currentTime={currentTime} onChange={invalidate} />
      ))}
      <div className="trim-row">
        <select value={characterId ?? ""} onChange={(e) => setCharacterId(e.target.value ? Number(e.target.value) : null)}>
          <option value="">Character…</option>
          {cast.map((c) => (
            <option key={c.id} value={c.id} disabled={!c.has_voice}>
              {c.name}
              {c.has_voice ? "" : " (no voice sample)"}
            </option>
          ))}
        </select>
        <input
          className="overdub-text"
          placeholder="The line, e.g. THE OVEN IS ON FIRE!"
          value={text}
          maxLength={300}
          onChange={(e) => setText(e.target.value)}
        />
        <label title="Chatterbox emotion exaggeration (0.25–2)">
          Emotion
          <input
            type="number"
            min={0.25}
            max={2}
            step={0.05}
            value={exaggeration}
            onChange={(e) => setExaggeration(Number(e.target.value))}
          />
        </label>
        <button
          className="primary"
          disabled={!chosen?.has_voice || !text.trim() || create.isPending}
          onClick={() => create.mutate()}
          title="Generates the line in the character's voice and places it at the playhead"
        >
          {create.isPending ? "Generating…" : `Generate at ${currentTime.toFixed(2)}s`}
        </button>
        {create.error && (
          <span className="warn small">
            {create.error instanceof ApiError ? create.error.detail : String(create.error)}
          </span>
        )}
      </div>
    </div>
  );
}

function OverdubRow({
  o,
  videoRef,
  currentTime,
  onChange,
}: {
  o: Overdub;
  videoRef: RefObject<HTMLVideoElement | null>;
  currentTime: number;
  onChange: () => void;
}) {
  const audioRef = useRef<HTMLAudioElement>(null);
  const [offset, setOffset] = useState(o.offset_s);
  const [volume, setVolume] = useState(o.volume);
  useEffect(() => {
    setOffset(o.offset_s);
    setVolume(o.volume);
  }, [o.offset_s, o.volume]);
  const dirty = Math.abs(offset - o.offset_s) > 1e-3 || Math.abs(volume - o.volume) > 1e-3;

  const save = useMutation({
    mutationFn: () => api.patch(`/overdubs/${o.id}`, { offset_s: offset, volume }),
    onSuccess: onChange,
  });
  const replace = useMutation({
    mutationFn: (replace_voice: boolean) => api.patch(`/overdubs/${o.id}`, { replace_voice }),
    onSuccess: onChange,
  });
  const remove = useMutation({
    mutationFn: () => api.post(`/overdubs/${o.id}/archive`),
    onSuccess: onChange,
  });

  // Play the take from 1s before the line with the line on top. The browser only has the
  // take's full mix, so a punch-in mutes the whole take (fx too) for the line; the render
  // silences only the voice.
  const audition = () => {
    const v = videoRef.current;
    const a = audioRef.current;
    if (!v || !a) return;
    const lead = Math.min(1, offset);
    const wasMuted = v.muted;
    v.currentTime = offset - lead;
    a.currentTime = 0;
    a.volume = Math.min(1, volume);
    void v.play();
    window.setTimeout(() => {
      if (o.replace_voice) v.muted = true;
      void a.play();
    }, lead * 1000);
    if (o.replace_voice) window.setTimeout(() => (v.muted = wasMuted), (lead + (o.duration_s ?? 0)) * 1000);
  };

  return (
    <div className="trim-row overdub-row">
      <strong>{o.character_name}</strong>
      <span className="overdub-quote">“{o.text}”</span>
      {o.status === "queued" && (
        <span className="muted small" role="status">
          <span className="spinner" aria-hidden /> generating…
        </span>
      )}
      {o.status === "failed" && <span className="warn small">failed: {o.error}</span>}
      {o.audio_url && <audio ref={audioRef} src={o.audio_url} preload="auto" />}
      <label>
        at
        <input type="number" step={0.01} min={0} value={offset} onChange={(e) => setOffset(Number(e.target.value))} />s
      </label>
      <button onClick={() => setOffset(Number(currentTime.toFixed(3)))}>⟵ playhead</button>
      <label>
        vol
        <input type="number" step={0.05} min={0} max={2} value={volume} onChange={(e) => setVolume(Number(e.target.value))} />
      </label>
      <label title="Punch-in: silence the take's own voice while this line plays (foley and ambience keep playing)">
        <input
          type="checkbox"
          checked={o.replace_voice}
          disabled={replace.isPending}
          onChange={(e) => replace.mutate(e.target.checked)}
        />
        replace voice
      </label>
      <button disabled={o.status !== "done"} onClick={audition}>
        ▶ Audition
      </button>
      <button className="primary" disabled={!dirty || save.isPending} onClick={() => save.mutate()}>
        Save
      </button>
      <button disabled={remove.isPending} onClick={() => remove.mutate()}>
        Remove
      </button>
      {o.cost_usd !== null && <span className="muted small">{formatUsd(o.cost_usd)}</span>}
      {[save.error, replace.error, remove.error].map(
        (e, i) =>
          e && (
            <span key={i} className="warn small">
              {e instanceof ApiError ? e.detail : String(e)}
            </span>
          ),
      )}
    </div>
  );
}
