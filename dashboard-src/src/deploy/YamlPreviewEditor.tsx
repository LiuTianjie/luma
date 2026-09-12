import { useId, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Card, CardAction, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import type { Lang } from "../types";

export function YamlPreviewEditor({ mode, lang = "zh", serviceYaml, composeYaml, sidecarYaml, onServiceYamlChange, onComposeYamlChange, onSidecarYamlChange }: {
  mode: "service" | "compose";
  lang?: Lang;
  serviceYaml: string;
  composeYaml: string;
  sidecarYaml: string;
  onServiceYamlChange: (value: string) => void;
  onComposeYamlChange: (value: string) => void;
  onSidecarYamlChange: (value: string) => void;
}) {
  const id = useId();
  const [active, setActive] = useState<"compose" | "sidecar">("compose");
  const zh = lang === "zh";
  const documents = mode === "service"
    ? [{ key: "service", name: "service.yaml", value: serviceYaml, onChange: onServiceYamlChange }]
    : [{ key: "compose", name: "docker-compose.yml", value: composeYaml, onChange: onComposeYamlChange }, { key: "sidecar", name: "luma.compose.yml", value: sidecarYaml, onChange: onSidecarYamlChange }];
  const activeDocument = documents.find((document) => document.key === active) || documents[0];
  return <Card className="min-w-0">
    <CardHeader><CardTitle>{zh ? "配置文件" : "Configuration files"}</CardTitle><CardDescription>{zh ? "校验和部署均使用当前文件内容。" : "Validation and deployment use the current documents."}</CardDescription><CardAction><Badge variant="outline">YAML</Badge></CardAction></CardHeader>
    <CardContent>
      <Tabs value={mode === "service" ? "service" : active} onValueChange={(value) => { if (value === "compose" || value === "sidecar") setActive(value); }} className="min-w-0 gap-4">
        <TabsList className="max-w-full overflow-x-auto" aria-label={zh ? "配置文件" : "Configuration files"}>
          {documents.map((document) => <TabsTrigger key={document.key} value={document.key}>{document.name}</TabsTrigger>)}
        </TabsList>
        {documents.map((document) => <TabsContent key={document.key} value={document.key}>
          <FieldGroup><Field><FieldLabel className="sr-only" htmlFor={`${id}-${document.key}`}>{document.name}</FieldLabel><Textarea id={`${id}-${document.key}`} className="h-96 max-h-[70vh] min-h-64 resize-y" value={document.value} onChange={(event) => document.onChange(event.target.value)} spellCheck={false} autoCapitalize="off" autoCorrect="off" wrap="off" /></Field></FieldGroup>
        </TabsContent>)}
      </Tabs>
    </CardContent>
    <CardFooter className="justify-between gap-4"><span>{activeDocument.value.split("\n").length} {zh ? "行" : "lines"}</span><Badge variant="outline">UTF-8</Badge></CardFooter>
  </Card>;
}
