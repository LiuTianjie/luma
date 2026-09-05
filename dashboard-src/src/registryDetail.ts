import type { RegistryInventory, RegistryManifest } from "./registryManagementApi";

export type RegistryImageResult =
  | { status: "ready"; image: RegistryManifest }
  | { status: "pending" | "missing"; image: null };

/** Search every matching page: a shared digest may occur in many repositories. */
export async function findRegistryImage(
  key: string,
  loadPage: (offset: number) => Promise<RegistryInventory>,
  signal: AbortSignal,
): Promise<RegistryImageResult> {
  let offset = 0;
  while (true) {
    signal.throwIfAborted();
    const page = await loadPage(offset);
    signal.throwIfAborted();
    const image = page.entries?.find((item) => `${item.repository}@${item.digest}` === key);
    if (image) return { status: "ready", image };
    if (page.scanPending) return { status: "pending", image: null };
    const nextOffset = (page.page?.offset ?? offset) + (page.page?.limit || page.entries?.length || 0);
    if (!page.page?.hasMore || nextOffset <= offset) return { status: "missing", image: null };
    offset = nextOffset;
  }
}
