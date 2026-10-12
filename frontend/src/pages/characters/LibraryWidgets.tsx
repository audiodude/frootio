// Widgets shared by the character and location pages: image grid with upload,
// nano-banana generation, generation job list, and small form helpers.

import { useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api, formatUsd, mediaUrl } from "../../api";
import type { Character, GenerationOptions, GenJob, LibraryImage, Queued } from "../../types/library";

export function errorText(error: unknown): string {
  if (error instanceof ApiError) return error.detail;
  if (error instanceof Error) return error.message;
  return String(error);
}

export function ErrorLine({ error }: { error: unknown }) {
  if (!error) return null;
  return <p className="error">{errorText(error)}</p>;
}

export function Thumb({ image, size = 120 }: { image: LibraryImage | null; size?: number }) {
  if (!image) return <div className="thumb empty" style={{ width: size, height: size }} />;
  return (
    <img
      className="thumb"
      src={mediaUrl(image.path)}
      alt=""
      style={{ width: size, height: size }}
      title={`${image.width ?? "?"}×${image.height ?? "?"} · ${image.source}`}
    />
  );
}

/** Upload one or more image files to `${base}/images`. */
export function ImageUpload({ base, invalidate }: { base: string; invalidate: unknown[] }) {
  const qc = useQueryClient();
  const upload = useMutation({
    mutationFn: (files: FileList) => {
      const form = new FormData();
      for (const f of Array.from(files)) form.append("files", f);
      return api.post(`${base}/images`, form);
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: invalidate }),
  });
  return (
    <label className="button">
      {upload.isPending ? "Uploading…" : "Upload images"}
      <input
        type="file"
        accept="image/png,image/jpeg,image/webp"
        multiple
        hidden
        onChange={(e) => {
          if (e.target.files?.length) upload.mutate(e.target.files);
          e.target.value = "";
        }}
      />
      <ErrorLine error={upload.error} />
    </label>
  );
}

/** Grid of images; `actions` renders per-image buttons. */
export function ImageGrid({
  images,
  badge,
  actions,
}: {
  images: LibraryImage[];
  badge?: (image: LibraryImage) => ReactNode;
  actions?: (image: LibraryImage) => ReactNode;
}) {
  if (images.length === 0) return <p className="muted">No images yet.</p>;
  return (
    <div className="image-grid">
      {images.map((img) => (
        <figure key={img.id} className="image-card">
          <a href={mediaUrl(img.path)} target="_blank" rel="noreferrer">
            <Thumb image={img} size={160} />
          </a>
          <figcaption>
            <span className="muted small">
              {img.source}
              {img.width && img.height ? ` · ${img.width}×${img.height}` : ""}
            </span>
            {badge?.(img)}
            {img.gen_prompt && (
              <span className="muted small clamp" title={img.gen_prompt}>
                “{img.gen_prompt}”
              </span>
            )}
            <span className="row">{actions?.(img)}</span>
          </figcaption>
        </figure>
      ))}
    </div>
  );
}

/**
 * nano-banana generation: prompt alone (fal-ai/nano-banana) or with input images from
 * the library (fal-ai/nano-banana/edit), e.g. another character's main as a style anchor.
 */
export function GeneratePanel({
  base,
  ownImages,
  jobsKey,
}: {
  base: string;
  ownImages: LibraryImage[];
  jobsKey: unknown[];
}) {
  const qc = useQueryClient();
  const [prompt, setPrompt] = useState("");
  const [n, setN] = useState(2);
  const [aspect, setAspect] = useState("");
  const [inputs, setInputs] = useState<string[]>([]);
  const options = useQuery({
    queryKey: ["generation-options"],
    queryFn: () => api.get<GenerationOptions>("/characters/generation-options"),
  });
  const rate = options.data?.nano_banana_usd_per_image ?? 0;
  const aspects = ["", ...(options.data?.aspect_ratios ?? []).filter((a) => a !== "auto")];
  const maxImages = options.data?.max_images ?? 4;
  const chars = useQuery({
    queryKey: ["characters", "list", false],
    queryFn: () => api.get<Character[]>("/characters"),
  });
  const anchors: { path: string; label: string }[] = [];
  for (const c of chars.data ?? []) {
    if (c.main_image) anchors.push({ path: c.main_image.path, label: `${c.name} (main)` });
  }
  for (const img of ownImages) {
    if (!anchors.some((a) => a.path === img.path)) anchors.push({ path: img.path, label: `this · #${img.id}` });
  }
  const gen = useMutation({
    mutationFn: () =>
      api.post<Queued>(`${base}/images/generate`, {
        prompt,
        num_images: n,
        input_paths: inputs,
        aspect_ratio: aspect || (inputs.length ? "auto" : null),
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: jobsKey }),
  });

  return (
    <div className="panel">
      <h3>Generate with nano-banana</h3>
      <textarea
        rows={3}
        value={prompt}
        placeholder="Describe the image. With input images, describe the edit (e.g. 'a kiwi character in the same 3D style')."
        onChange={(e) => setPrompt(e.target.value)}
      />
      <div className="row">
        <label>
          Candidates{" "}
          <select value={n} onChange={(e) => setN(Number(e.target.value))}>
            {Array.from({ length: maxImages }, (_, i) => i + 1).map((v) => (
              <option key={v}>{v}</option>
            ))}
          </select>
        </label>
        <label>
          Aspect{" "}
          <select value={aspect} onChange={(e) => setAspect(e.target.value)}>
            {aspects.map((a) => (
              <option key={a} value={a}>
                {a || (inputs.length ? "auto" : "1:1 (default)")}
              </option>
            ))}
          </select>
        </label>
        <span className="muted">
          {inputs.length ? "fal-ai/nano-banana/edit" : "fal-ai/nano-banana"} · est. {formatUsd(n * rate)}
        </span>
      </div>
      <details>
        <summary>Input images ({inputs.length} selected)</summary>
        <div className="anchor-grid">
          {anchors.map((a) => (
            <label key={a.path} className={inputs.includes(a.path) ? "anchor selected" : "anchor"}>
              <input type="checkbox" checked={inputs.includes(a.path)} onChange={() =>
                  setInputs((cur) => (cur.includes(a.path) ? cur.filter((p) => p !== a.path) : [...cur, a.path]))
                } />
              <img src={mediaUrl(a.path)} alt="" />
              <span className="small">{a.label}</span>
            </label>
          ))}
        </div>
      </details>
      <button disabled={!prompt.trim() || gen.isPending} onClick={() => gen.mutate()}>
        {gen.isPending ? "Queuing…" : `Generate ${n}`}
      </button>
      <ErrorLine error={gen.error} />
    </div>
  );
}

/** Recent image/voice generation jobs for an owner; refreshed by `job` events. */
export function JobList({ jobsKey, path, kind }: { jobsKey: unknown[]; path: string; kind: GenJob["kind"] }) {
  const jobs = useQuery({ queryKey: jobsKey, queryFn: () => api.get<GenJob[]>(path) });
  const recent = (jobs.data ?? []).filter((j) => j.kind === kind).slice(0, 6);
  if (recent.length === 0) return null;
  return (
    <ul className="jobs">
      {recent.map((j) => (
        <li key={j.id}>
          <span className={`status ${j.status}`}>{j.status}</span>{" "}
          {j.kind === "image_gen" ? "image" : "voice"} job #{j.id}
          {typeof j.payload.prompt === "string" && <span className="muted"> · “{j.payload.prompt}”</span>}
          {typeof j.payload.text === "string" && (
            <span className="muted">
              {" "}
              · {String(j.payload.voice)}: “{j.payload.text}”
            </span>
          )}
          {j.error && <span className="error"> · {j.error}</span>}
        </li>
      ))}
    </ul>
  );
}

/** Text field that saves on blur/Enter when changed. */
export function InlineEdit({
  value,
  onSave,
  multiline,
  placeholder,
}: {
  value: string;
  onSave: (v: string) => void;
  multiline?: boolean;
  placeholder?: string;
}) {
  const [draft, setDraft] = useState(value);
  const [base, setBase] = useState(value);
  if (base !== value) {
    setBase(value);
    setDraft(value);
  }
  const commit = () => {
    if (draft !== value) onSave(draft);
  };
  return multiline ? (
    <textarea
      rows={4}
      value={draft}
      placeholder={placeholder}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={commit}
    />
  ) : (
    <input
      value={draft}
      placeholder={placeholder}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => e.key === "Enter" && commit()}
    />
  );
}
