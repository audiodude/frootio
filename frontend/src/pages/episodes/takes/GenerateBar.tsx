import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, formatUsd } from "../../../api";
import type { GenerateEstimate, Take } from "../../../types/takes";
import { errorDetail } from "./format";

const MAX_N = 20;

/** Sticky Generate bar at the bottom of the inspector: Count, Seed, and Primary CTA with cost. */
export default function GenerateBar({ snippetId }: { snippetId: number }) {
  const queryClient = useQueryClient();
  const [nOverride, setNOverride] = useState<number | null>(null);
  const [pinSeed, setPinSeed] = useState<boolean | null>(null);
  const [seedText, setSeedText] = useState<string | null>(null);

  useEffect(() => {
    setNOverride(null);
    setPinSeed(null);
    setSeedText(null);
  }, [snippetId]);

  const nParam = nOverride === null ? "" : `?n=${nOverride}`;
  const estimate = useQuery({
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
        <label className="gen-opt" title="Number of takes to generate">
          <span>Count</span>
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
        <label className="gen-opt" title="Pin starting random seed">
          <input type="checkbox" checked={pinned} onChange={(e) => setPinSeed(e.target.checked)} />
          <span>Seed</span>
        </label>
        {pinned && (
          <input
            className={seedInvalid ? "invalid seed-input" : "seed-input"}
            inputMode="numeric"
            placeholder="seed #"
            value={seedValue}
            onChange={(e) => setSeedText(e.target.value.trim())}
          />
        )}
        <button
          className="primary gen-cta"
          disabled={disabled}
          onClick={() => generate.mutate()}
          title={
            est
              ? `${est.duration_s}s × $0.08 + ref tokens (${est.ref_tokens}) = ${formatUsd(est.per_take_usd)}/take`
              : undefined
          }
        >
          {generate.isPending
            ? "Generating…"
            : `Generate ${n} Take${n === 1 ? "" : "s"}${est ? ` (${formatUsd(est.total_usd)})` : ""}`}
        </button>
      </div>
      {est?.start_frame && (
        <div className="muted small">↳ Continues from take #{est.start_frame.take_id} end frame</div>
      )}
      {estimate.error && <div className="warn small">{errorDetail(estimate.error)}</div>}
      {blocked && <div className="warn small">Blocked: {blocked}</div>}
      {capError && <div className="warn small">{capError}</div>}
      {error && <div className="warn small">{error}</div>}
    </div>
  );
}
