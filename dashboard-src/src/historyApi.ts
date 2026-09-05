import { apiGet } from "./apiClient";
import { historyDetailPath, historyListPath, type HistoryDetail, type HistoryList, type HistoryPage, type HistorySelection } from "./historyModel";

async function historyGet<T>(path: string, token: string, signal?: AbortSignal): Promise<T> {
  const controller = new AbortController();
  const abort = () => controller.abort();
  signal?.addEventListener("abort", abort, { once: true });
  if (signal?.aborted) controller.abort();
  let timedOut = false;
  const timer = setTimeout(() => { timedOut = true; controller.abort(); }, 20_000);
  try { return await apiGet<T>(path, token, controller.signal); }
  catch (error) { if (timedOut) throw new Error("History request timed out; please retry"); throw error; }
  finally { clearTimeout(timer); signal?.removeEventListener("abort", abort); }
}

function validPage(page: HistoryPage | undefined): boolean {
  return Boolean(page && typeof page.hasMore === "boolean" && (page.nextCursor === null || typeof page.nextCursor === "string")
    && (!page.hasMore || page.nextCursor));
}

export async function fetchHistory(token: string, filters: string, cursor?: string | null, signal?: AbortSignal): Promise<HistoryList> {
  const result = await historyGet<HistoryList>(historyListPath(filters, cursor), token, signal);
  if (!Array.isArray(result.items) || !validPage(result.page)) throw new Error("Control returned an invalid history page");
  return result;
}

export async function fetchHistoryDetail(token: string, selection: HistorySelection, cursor?: string | null, signal?: AbortSignal): Promise<HistoryDetail> {
  const result = await historyGet<HistoryDetail>(historyDetailPath(selection, cursor), token, signal);
  if (!result.item || result.item.id !== selection.id || result.item.kind !== selection.kind || !Array.isArray(result.events) || !validPage(result.page)) {
    throw new Error("Control returned an invalid history detail page");
  }
  return result;
}
