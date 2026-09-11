import { AlertCircle } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";

export function ErrorBanner({ errors }: { errors: string[] }) {
  if (!errors.length) return null;
  return (
    <div className="flex flex-col gap-2">
      {errors.map((message) => (
        <Alert key={message} variant="destructive">
          <AlertCircle />
          <AlertTitle>Error</AlertTitle>
          <AlertDescription>{message}</AlertDescription>
        </Alert>
      ))}
    </div>
  );
}
