import { AlertCircle } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import type { Lang } from "../types";

export function ErrorBanner({ errors, lang = "zh" }: { errors: string[]; lang?: Lang }) {
  if (!errors.length) return null;
  return (
    <Alert variant="destructive">
      <AlertCircle />
      <AlertTitle>{lang === "zh" ? "发生错误" : "Something went wrong"}</AlertTitle>
      <AlertDescription className="min-w-0 wrap-anywhere">
        {errors.length === 1 ? errors[0] : (
          <ul className="flex list-disc flex-col gap-1 pl-4">
            {errors.map((message, index) => <li key={`${index}:${message}`}>{message}</li>)}
          </ul>
        )}
      </AlertDescription>
    </Alert>
  );
}
