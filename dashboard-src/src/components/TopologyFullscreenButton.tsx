import { useEffect, useState } from "react";
import { Expand, Shrink } from "lucide-react";
import { toast } from "@/components/ui/toast";
import type cytoscape from "cytoscape";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { Lang } from "../types";
import "./TopologyFullscreenButton.css";

export function TopologyFullscreenButton({ cy, lang }: { cy: cytoscape.Core | null; lang: Lang }) {
  const [fullscreen, setFullscreen] = useState(false);
  const zh = lang === "zh";

  useEffect(() => {
    const canvas = cy?.container()?.closest(".topology-canvas");
    if (!cy || !canvas) return;
    const sync = () => setFullscreen(document.fullscreenElement === canvas);
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && document.fullscreenElement === canvas) {
        void document.exitFullscreen().catch(() => {});
      }
    };
    sync();
    document.addEventListener("fullscreenchange", sync);
    document.addEventListener("keydown", handleKeyDown);
    // Fullscreen and window resizing both change the actual drawing area.
    let frame = 0;
    const observer = new ResizeObserver(() => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        if (!cy.destroyed()) {
          cy.resize();
          cy.fit(undefined, 48);
        }
      });
    });
    observer.observe(canvas);
    return () => {
      document.removeEventListener("fullscreenchange", sync);
      document.removeEventListener("keydown", handleKeyDown);
      observer.disconnect();
      cancelAnimationFrame(frame);
    };
  }, [cy]);

  const toggle = async () => {
    const canvas = cy?.container()?.closest<HTMLElement>(".topology-canvas");
    if (!canvas) return;
    try {
      if (document.fullscreenElement === canvas) await document.exitFullscreen();
      else await canvas.requestFullscreen();
    } catch {
      toast.add({ type: "error", title: zh ? "无法切换全屏" : "Unable to switch fullscreen", description: zh ? "请检查浏览器是否允许全屏。" : "Check your browser's fullscreen permissions." });
    }
  };
  const label = fullscreen ? (zh ? "退出全屏" : "Exit fullscreen") : (zh ? "全屏" : "Fullscreen");
  return <Tooltip>
    <TooltipTrigger render={<Button variant="outline" size="icon-sm" type="button" aria-label={label} title={label} aria-pressed={fullscreen} disabled={!cy} onClick={toggle} />}>
      {fullscreen ? <Shrink data-icon="inline-start" /> : <Expand data-icon="inline-start" />}
    </TooltipTrigger>
    <TooltipContent>{label}</TooltipContent>
  </Tooltip>;
}
