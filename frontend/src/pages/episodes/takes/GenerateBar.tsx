import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, formatUsd } from "../../../api";
import type { GenerateEstimate, Take } from "../../../types/takes";
import { errorDetail } from "./format";

const MAX_N = 20;

/** Generate button with take count (N), optional pinned seed, estimated cost and block/cap messages. */
export default function GenerateBar({ snippetId }: { snippetId: number }) {
  const queryClient = useQueryClient();
  const [nOverride, setNOverride] = useState<number | null>(null);
  const [pinSeed, setPinSeed] = useState<boolean | null>(null);
  const [seedText, setSeedText] = useState<string | null>(null);

  // Overrides are per snippet: reset when switching snippets.
  useEffect(() => {
    setNOverride(null);
    setPinSeed(null);
    setSeedText(null);
  }, [snippetId]);

  const nParam = nOverride === null ? "" : `?n=${nOverride}`;
  const estimate = useQuery({
    // "snippets" prefix: refreshed on snippet edits (prompt/refs), takes and spend changes.
    queryKey: ["snippets", snippetId, "estimate", nOverride],
    queryFn: () => api.get<GenerateEstimate>(`/snippets/${snippetId}/estimate${nParam}`),
  });
  const est = estimate.data;
  const n = nOverride ?? est?.default_n ?? 1;
  const pinned = pinSeed ?? est?.pinned_seed != null;
  const seedValue = seedText ?? (est?.pinned_seed != null ? String(est.pinned_seed) : "");
  const seed = pinned && /^\d+$/.test(seedValue) ? Number(seedValue) : null;
  const seedInvalid = pinned && seed === null;

  const generate = useMutation({
    mutationFn: () => api.post<{ takes: Take[] }>(`/snippets/${snippetId}/generate`, { n, seed }),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["takes", snippetId] });
      void queryClient.invalidateQueries({ queryKey: ["snippets", snippetId, "estimate"] });
    },
  });

  const blocked = est?.blocked ?? null;
  const capError = est?.cap_error ?? null;
  const error = errorDetail(generate.error);
  const disabled = !est || !!blocked || !!capError || seedInvalid || generate.isPending;

  return (
    <div className="generate-bar">
      <div className="generate-row">
        <button className="primary" disabled={disabled} onClick={() => generate.mutate()}>
          {generate.isPending ? "Generating…" : `Generate ${n} take${n === 1 ? "" : "s"}`}
        </button>
        <label title="Takes for this click (default from the episode)">
          N{" "}
          <input
            type="number"
            min={1}
            max={MAX_N}
            value={n}
            onChange={(e) => {
              const v = Math.round(Number(e.target.value));
              if (v >= 1 && v <= MAX_N) setNOverride(v);
            }}
          />
        </label>
        <label title="Pinned seed: the N takes use seed, seed+1, …">
          <input type="checkbox" checked={pinned} onChange={(e) => setPinSeed(e.target.checked)} /> seed
        </label>
        {pinned && (
          <input
            className={seedInvalid ? "invalid seed-input" : "seed-input"}
            inputMode="numeric"
            placeholder="seed"
            value={seedValue}
            onChange={(e) => setSeedText(e.target.value.trim())}
          />
        )}
        <span className="estimate" title={est ? estimateTitle(est) : undefined}>
          {estimate.isLoading && "estimating…"}
          {est && `≈ ${formatUsd(est.total_usd)}`}
          {est && n > 1 && <span className="muted"> ({formatUsd(est.per_take_usd)} each)</span>}
        </span>
      </div>
      {est && (
        <div className="muted small">
          {est.duration_s}s · {est.ref_tokens} ref tokens · episode video {formatUsd(est.cap.video_spend_usd)}
          {est.cap.in_flight_usd > 0 && ` + ${formatUsd(est.cap.in_flight_usd)} in flight`} of{" "}
          {formatUsd(est.cap.cap_usd)} cap
          {est.start_frame && ` · continues from take #${est.start_frame.take_id}`}
        </div>
      )}
      {estimate.error && <div className="warn">{errorDetail(estimate.error)}</div>}
      {blocked && <div className="warn">Blocked: {blocked}</div>}
      {capError && <div className="warn">{capError}</div>}
      {error && <div className="warn">{error}</div>}
    </div>
  );
}

function estimateTitle(est: GenerateEstimate): string {
  return `${est.duration_s}s × $0.08 + max(0, ${est.ref_tokens} − 4096) × $0.02/1000 = ${formatUsd(est.per_take_usd)} per take`;
}

