import { useState } from "react";
import type { Lang } from "../types";

export function YamlPreviewEditor({
  mode, lang = "zh", serviceYaml, composeYaml, sidecarYaml,
  onServiceYamlChange, onComposeYamlChange, onSidecarYamlChange,
}: {
  mode: "service" | "compose";
  lang?: Lang;
  serviceYaml: string;
  composeYaml: string;
  sidecarYaml: string;
  onServiceYamlChange: (value: string) => void;
  onComposeYamlChange: (value: string) => void;
  onSidecarYamlChange: (value: string) => void;
}) {
  const [active, setActive] = useState<"compose" | "sidecar">("compose");
  const name = mode === "service" ? "service.yaml" : active === "compose" ? "docker-compose.yml" : "luma.compose.yml";
  const value = mode === "service" ? serviceYaml : active === "compose" ? composeYaml : sidecarYaml;
  const onChange = mode === "service" ? onServiceYamlChange : active === "compose" ? onComposeYamlChange : onSidecarYamlChange;
  return <section className="workbench-code-editor">
    <header>
      {mode === "compose" ? <nav aria-label={lang === "zh" ? "配置文件" : "Configuration files"}>
        <button type="button" aria-current={active === "compose" ? "page" : undefined} onClick={() => setActive("compose")}>docker-compose.yml</button>
        <button type="button" aria-current={active === "sidecar" ? "page" : undefined} onClick={() => setActive("sidecar")}>luma.compose.yml</button>
      </nav> : <strong>{name}</strong>}
      <span>YAML</span>
    </header>
    <textarea aria-label={name} value={value} onChange={(event) => onChange(event.target.value)} spellCheck={false} autoCapitalize="off" autoCorrect="off" />
    <footer><span>{value.split("\n").length} {lang === "zh" ? "行" : "lines"}</span><span>UTF-8</span></footer>
  </section>;
}
