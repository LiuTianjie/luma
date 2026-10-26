import { apiPost } from "./apiClient";
import type { NodeDiskCleanupResult } from "./types";

// Ask the node agent to clean unused images and old build cache. Control keeps
// this a dry run unless `dryRun` is explicitly false.
export function runNodeDiskCleanup(token: string, node: string, dryRun: boolean): Promise<NodeDiskCleanupResult> {
  return apiPost<NodeDiskCleanupResult>("/v1/nodes/disk-cleanup", token, { node, dryRun });
}
