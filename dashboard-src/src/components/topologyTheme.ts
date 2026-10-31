import { useEffect, useState } from "react";

/** Canvas graphs cannot resolve CSS variables or OKLCH colors themselves. */
function readTopologyTheme() {
  const styles = getComputedStyle(document.documentElement);
  const canvas = document.createElement("canvas");
  canvas.width = 1;
  canvas.height = 1;
  const context = canvas.getContext("2d", { willReadFrequently: true });
  // Translucent tokens (borders, hairlines) are composited over the card so the
  // graph strokes match what the same token looks like elsewhere on the page.
  const color = (token: string, over = "--card") => {
    const value = styles.getPropertyValue(token).trim();
    if (!context) return value;
    context.clearRect(0, 0, 1, 1);
    context.fillStyle = styles.getPropertyValue(over).trim() || "transparent";
    context.fillRect(0, 0, 1, 1);
    context.fillStyle = value;
    context.fillRect(0, 0, 1, 1);
    const [red, green, blue, alpha] = context.getImageData(0, 0, 1, 1).data;
    return `rgba(${red}, ${green}, ${blue}, ${alpha / 255})`;
  };
  return {
    foreground: color("--foreground"),
    card: color("--card"),
    border: color("--border"),
    // Canvas draws alpha colours as-is, so graph strokes use opaque tokens.
    hairline: color("--input"),
    canvas: color("--canvas"),
    subtle: color("--subtle-foreground"),
    muted: color("--muted"),
    mutedForeground: color("--muted-foreground"),
    primary: color("--primary"),
    accent: color("--accent"),
    destructive: color("--destructive"),
    fontMono: styles.getPropertyValue("--font-mono").trim(),
    fontSans: styles.getPropertyValue("--font-sans").trim() || "system-ui, sans-serif",
  };
}

export type TopologyTheme = ReturnType<typeof readTopologyTheme>;

export function useTopologyTheme(theme: "light" | "dark") {
  const [tokens, setTokens] = useState(readTopologyTheme);
  useEffect(() => {
    const refresh = () => setTokens(readTopologyTheme());
    // The application applies its theme in an effect. Observe that commit as
    // well, so graph colors never lag one theme change behind the UI.
    const observer = new MutationObserver(refresh);
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["class", "data-theme", "style"] });
    refresh();
    return () => observer.disconnect();
  }, [theme]);
  return tokens;
}
