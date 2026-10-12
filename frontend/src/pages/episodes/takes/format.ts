import { ApiError } from "../../../api";
import type { TakeStatus } from "../../../types/takes";

/** Message to show for a failed query/mutation (the server's `detail` for API errors). */
export function errorDetail(error: Error | null | undefined): string | null {
  if (!error) return null;
  return error instanceof ApiError ? error.detail : error.message;
}

export const STATUS_LABEL: Record<TakeStatus, string> = {
  queued: "queued",
  running: "running",
  processing: "processing",
  done: "done",
  failed: "failed",
  cancelled: "cancelled",
  download_failed: "download failed",
  processing_failed: "processing failed",
};
