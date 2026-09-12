import { ArrowLeft, FileQuestion } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Empty, EmptyContent, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty";
import type { Lang } from "../types";

export function NotFound({ lang, onHome }: { lang: Lang; onHome: () => void }) {
  const zh = lang === "zh";
  return (
    <Empty className="min-h-64 border">
      <EmptyHeader>
        <EmptyMedia variant="icon">
          <FileQuestion />
        </EmptyMedia>
        <EmptyTitle role="heading" aria-level={1}>{zh ? "页面不存在" : "Page not found"}</EmptyTitle>
        <EmptyDescription>{zh ? "这个路径没有对应的控制台页面。" : "This path does not match a dashboard page."}</EmptyDescription>
      </EmptyHeader>
      <EmptyContent>
        <Button variant="outline" onClick={onHome}>
          <ArrowLeft data-icon="inline-start" />
          {zh ? "返回总览" : "Back to overview"}
        </Button>
      </EmptyContent>
    </Empty>
  );
}
