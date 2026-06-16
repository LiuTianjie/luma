export function YamlPreviewEditor({
  mode,
  serviceYaml,
  composeYaml,
  sidecarYaml,
  onServiceYamlChange,
  onComposeYamlChange,
  onSidecarYamlChange,
}: {
  mode: "service" | "compose";
  serviceYaml: string;
  composeYaml: string;
  sidecarYaml: string;
  onServiceYamlChange: (value: string) => void;
  onComposeYamlChange: (value: string) => void;
  onSidecarYamlChange: (value: string) => void;
}) {
  if (mode === "service") {
    return (
      <section className="deploy-yaml-grid deploy-yaml-editor-grid single">
        <label className="deploy-yaml-editor">
          <span>service.yaml</span>
          <textarea value={serviceYaml} onChange={(event) => onServiceYamlChange(event.target.value)} spellCheck={false} />
        </label>
      </section>
    );
  }
  return (
    <section className="deploy-yaml-grid deploy-yaml-editor-grid">
      <label className="deploy-yaml-editor">
        <span>docker-compose.yml</span>
        <textarea value={composeYaml} onChange={(event) => onComposeYamlChange(event.target.value)} spellCheck={false} />
      </label>
      <label className="deploy-yaml-editor">
        <span>luma.compose.yml</span>
        <textarea value={sidecarYaml} onChange={(event) => onSidecarYamlChange(event.target.value)} spellCheck={false} />
      </label>
    </section>
  );
}
