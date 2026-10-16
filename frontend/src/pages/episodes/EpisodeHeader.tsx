import { useEffect, useRef, useState, type ReactNode } from "react";
import { Link, NavLink } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, formatUsd } from "../../api";
import type { Episode } from "../../types/workspace";

interface Props {
  episodeId: number;
  episode?: Episode;
  seriesId?: number;
  seriesName?: string;
  episodeNumber: number;
  episodeTitle: string;
  mode: "workspace" | "assembly";
  rightExtra?: ReactNode;
}

/**
 * Unified Episode Studio Header:
 * - Breadcrumb: Series / Ep # · Editable Title
 * - Center segmented mode switcher: 1. Storyboard & Takes | 2. Assembly & Mix
 * - Right: Visual budget meter + ⚙ Defaults popover + optional mode actions
 */
export default function EpisodeHeader({
  episodeId,
  episode,
  seriesId,
  seriesName,
  episodeNumber,
  episodeTitle,
  mode,
  rightExtra,
}: Props) {
  const queryClient = useQueryClient();
  const [openDefaults, setOpenDefaults] = useState(false);
  const [editingTitle, setEditingTitle] = useState(false);
  const [titleDraft, setTitleDraft] = useState(episodeTitle);
  const popoverRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setTitleDraft(episodeTitle);
  }, [episodeTitle]);

  useEffect(() => {
    if (!openDefaults) return;
    const onDown = (e: MouseEvent) => {
      if (popoverRef.current && !popoverRef.current.contains(e.target as Node)) {
        setOpenDefaults(false);
      }
    };
    window.addEventListener("mousedown", onDown);
    return () => window.removeEventListener("mousedown", onDown);
  }, [openDefaults]);

  const patchEpisode = useMutation({
    mutationFn: (body: Partial<Pick<Episode, "title" | "default_location_id" | "take_count" | "spend_cap_usd">>) =>
      api.patch<Episode>(`/episodes/${episodeId}`, body),
    onSuccess: (ep) => {
      queryClient.setQueryData(["episode", episodeId], ep);
      void queryClient.invalidateQueries({ queryKey: ["assembly", episodeId] });
      void queryClient.invalidateQueries({ queryKey: ["series"] });
    },
  });

  const commitTitle = () => {
    setEditingTitle(false);
    const trimmed = titleDraft.trim();
    if (trimmed !== episodeTitle) {
      patchEpisode.mutate({ title: trimmed });
    }
  };

  const sId = episode?.series_id ?? seriesId;
  const sName = episode?.series.name ?? seriesName ?? "Series";
  const spendTotal = episode?.spend.total_usd ?? 0;
  const spendVideo = episode?.spend.video_usd ?? 0;
  const spendCap = episode?.spend_cap_usd ?? 20;
  const pct = Math.min(100, Math.round((spendVideo / Math.max(0.01, spendCap)) * 100));

  return (
    <header className="ep-header">
      <div className="ep-breadcrumb">
        {sId ? (
          <Link to={`/series/${sId}`} className="ep-series-link">
            {sName}
          </Link>
        ) : (
          <span className="ep-series-link">{sName}</span>
        )}
        <span className="ep-sep">/</span>
        <span className="ep-num">Ep {episodeNumber}</span>
        <span className="ep-sep">·</span>
        {editingTitle ? (
          <input
            className="ep-title-input"
            autoFocus
            value={titleDraft}
            placeholder="Untitled episode"
            onChange={(e) => setTitleDraft(e.target.value)}
            onBlur={commitTitle}
            onKeyDown={(e) => {
              if (e.key === "Enter") commitTitle();
              if (e.key === "Escape") {
                setTitleDraft(episodeTitle);
                setEditingTitle(false);
              }
            }}
          />
        ) : (
          <button
            type="button"
            className="ep-title-btn"
            title="Click to rename episode"
            onClick={() => setEditingTitle(true)}
          >
            {episodeTitle || <span className="muted">Untitled</span>}
          </button>
        )}
      </div>

      <nav className="ep-mode-switch" aria-label="Episode workflow mode">
        <NavLink
          to={`/episodes/${episodeId}`}
          end
          className={mode === "workspace" ? "ep-mode-tab active" : "ep-mode-tab"}
        >
          1. Storyboard &amp; Takes
        </NavLink>
        <NavLink
          to={`/episodes/${episodeId}/assembly`}
          className={mode === "assembly" ? "ep-mode-tab active" : "ep-mode-tab"}
        >
          2. Assembly &amp; Mix
        </NavLink>
      </nav>

      <div className="ep-actions">
        {episode && (
          <div
            className="ep-budget-pill"
            title={`Total spent: ${formatUsd(spendTotal)} · Video spend: ${formatUsd(spendVideo)} of ${formatUsd(spendCap)} cap`}
          >
            <span className="ep-budget-label">Budget</span>
            <strong className="ep-budget-val">
              {formatUsd(spendVideo)} <span className="muted">/ {formatUsd(spendCap)}</span>
            </strong>
            <div className="ep-budget-bar">
              <div
                className={pct >= 90 ? "ep-budget-fill warn" : "ep-budget-fill"}
                style={{ width: `${pct}%` }}
              />
            </div>
          </div>
        )}

        {episode && (
          <div className="ep-defaults-wrap" ref={popoverRef}>
            <button
              type="button"
              className={openDefaults ? "ep-defaults-btn active" : "ep-defaults-btn"}
              onClick={() => setOpenDefaults((v) => !v)}
            >
              ⚙ Defaults
            </button>
            {openDefaults && (
              <div className="ep-defaults-popover">
                <div className="ep-popover-title">Episode Defaults</div>
                <label className="ep-popover-field">
                  <span>Default location</span>
                  <select
                    value={episode.default_location_id ?? ""}
                    onChange={(e) =>
                      patchEpisode.mutate({
                        default_location_id: e.target.value === "" ? null : Number(e.target.value),
                      })
                    }
                  >
                    <option value="">(none)</option>
                    {episode.locations
                      .filter((l) => !l.archived || l.id === episode.default_location_id)
                      .map((l) => (
                        <option key={l.id} value={l.id}>
                          {l.name}
                        </option>
                      ))}
                  </select>
                </label>
                <label className="ep-popover-field">
                  <span>Takes per click</span>
                  <input
                    type="number"
                    min={1}
                    max={8}
                    value={episode.take_count}
                    onChange={(e) => {
                      const n = Number(e.target.value);
                      if (n >= 1) patchEpisode.mutate({ take_count: n });
                    }}
                  />
                </label>
                <label className="ep-popover-field">
                  <span>Video spend cap ($)</span>
                  <input
                    type="number"
                    min={1}
                    step={1}
                    value={episode.spend_cap_usd}
                    onChange={(e) => {
                      const n = Number(e.target.value);
                      if (n > 0) patchEpisode.mutate({ spend_cap_usd: n });
                    }}
                  />
                </label>
                <div className="ep-popover-foot muted small">
                  Total episode spend (incl. LLM/TTS): {formatUsd(episode.spend.total_usd)}
                </div>
                {patchEpisode.isError && <div className="warn small">{patchEpisode.error.message}</div>}
              </div>
            )}
          </div>
        )}

        {rightExtra}
      </div>
    </header>
  );
}
