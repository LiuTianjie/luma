import { AlertTriangle, Trash2 } from "lucide-react";
import { useCallback, useState, type ReactNode } from "react";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogMedia,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Spinner } from "@/components/ui/spinner";
import type { Lang } from "../types";

export type ConfirmTone = "danger" | "neutral";

export type ConfirmRequest = {
  title: string;
  body: ReactNode;
  confirmLabel?: string;
  warning?: string;
  tone?: ConfirmTone;
};

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
  const zh = lang === "zh";
  const tone = request.tone ?? "danger";
  const Icon = tone === "danger" ? Trash2 : AlertTriangle;
  const bodyText = typeof request.body === "string" ? request.body : null;

  return (
    <AlertDialog open onOpenChange={(open) => { if (!open && !busy) onCancel(); }}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogMedia>
            <Icon />
          </AlertDialogMedia>
          <AlertDialogTitle>{request.title}</AlertDialogTitle>
          <AlertDialogDescription>
            {bodyText || request.warning || (zh ? "请确认后继续。" : "Please confirm to continue.")}
          </AlertDialogDescription>
        </AlertDialogHeader>
        {bodyText ? null : <div className="text-sm text-muted-foreground">{request.body}</div>}
        {request.warning && bodyText ? (
          <Alert variant="destructive">
            <AlertTriangle aria-hidden="true" />
            <AlertDescription>{request.warning}</AlertDescription>
          </Alert>
        ) : null}
        <AlertDialogFooter>
          <AlertDialogCancel disabled={busy}>{zh ? "取消" : "Cancel"}</AlertDialogCancel>
          <AlertDialogAction
            variant={tone === "danger" ? "destructive" : "default"}
            disabled={busy}
            onClick={(event) => {
              event.preventDefault();
              onConfirm();
            }}
          >
            {busy ? <Spinner aria-hidden="true" data-icon="inline-start" /> : null}
            {busy ? (zh ? "处理中…" : "Working…") : request.confirmLabel || (zh ? "确认" : "Confirm")}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

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
