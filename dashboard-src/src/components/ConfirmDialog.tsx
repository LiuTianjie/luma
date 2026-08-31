import { AlertTriangle, Trash2 } from "lucide-react";
import { createPortal } from "react-dom";
import { useCallback, useState, type ReactNode } from "react";
import { OverlayShell } from "../useOverlay";
import type { Lang } from "../types";

export type ConfirmTone = "danger" | "neutral";

export type ConfirmRequest = {
  title: string;
  /** Plain-language consequence: what changes, and whether it can be undone. */
  body: ReactNode;
  /** Label for the action button. Defaults to a generic confirm. */
  confirmLabel?: string;
  /** Extra warning line, rendered in the tone's alert style. */
  warning?: string;
  tone?: ConfirmTone;
};

const ROOT = typeof document === "undefined" ? null : document.body;

// Confirmation dialog for destructive actions, replacing window.confirm.
//
// The native dialog has three problems here: it looks nothing like the rest of
// the console, it cannot show structured context (which services reference this
// image, how many replicas get recreated), and it cannot mark the confirm button
// as destructive. This matches the registry delete dialog, which already did it
// properly, and inherits Escape / focus-trap / focus-restore from useOverlay —
// with focus landing on Cancel, so Enter never fires the destructive action.
export function ConfirmDialog({
  lang,
  request,
  busy = false,
  onConfirm,
  onCancel,
}: {
  lang: Lang;
  request: ConfirmRequest;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  if (!ROOT) return null;
  const zh = lang === "zh";
  const tone = request.tone ?? "danger";
  const Icon = tone === "danger" ? Trash2 : AlertTriangle;
  return createPortal(
    <OverlayShell<HTMLElement>
      className="confirm-dialog-backdrop"
      onClose={() => { if (!busy) onCancel(); }}
    >
      {(overlayRef) => (
        <section
          className={`confirm-dialog tone-${tone}`}
          ref={overlayRef}
          role="dialog"
          aria-modal="true"
          aria-labelledby="confirm-dialog-title"
          onClick={(event) => event.stopPropagation()}
        >
          <div className="confirm-dialog-icon" aria-hidden="true"><Icon size={20} /></div>
          <h2 id="confirm-dialog-title">{request.title}</h2>
          <div className="confirm-dialog-body">{request.body}</div>
          {request.warning ? (
            <p className="confirm-dialog-warning">
              <AlertTriangle size={15} aria-hidden="true" />
              {request.warning}
            </p>
          ) : null}
          <div className="confirm-dialog-actions">
            <button type="button" className="ghost" disabled={busy} onClick={onCancel}>
              {zh ? "取消" : "Cancel"}
            </button>
            <button
              type="button"
              className={tone === "danger" ? "danger" : "primary"}
              disabled={busy}
              onClick={onConfirm}
            >
              {busy
                ? (zh ? "处理中…" : "Working…")
                : request.confirmLabel || (zh ? "确认" : "Confirm")}
            </button>
          </div>
        </section>
      )}
    </OverlayShell>,
    ROOT,
  );
}

// Promise-based driver so a call site reads like the `window.confirm` it replaces:
//
//   if (!(await confirm({ title, body }))) return;
//
// `element` must be rendered by the component that owns the hook.
export function useConfirm(lang: Lang) {
  const [state, setState] = useState<{ request: ConfirmRequest; resolve: (ok: boolean) => void } | null>(null);

  const confirm = useCallback(
    (request: ConfirmRequest) => new Promise<boolean>((resolve) => setState({ request, resolve })),
    [],
  );

  const settle = (ok: boolean) => {
    setState((current) => {
      current?.resolve(ok);
      return null;
    });
  };

  const element = state ? (
    <ConfirmDialog
      lang={lang}
      request={state.request}
      onConfirm={() => settle(true)}
      onCancel={() => settle(false)}
    />
  ) : null;

  return { confirm, element };
}
