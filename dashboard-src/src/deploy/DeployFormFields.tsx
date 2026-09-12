import { useId, type ComponentProps, type ReactNode } from "react";
import { Plus, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from "@/components/ui/empty";
import { Field, FieldContent, FieldDescription, FieldError, FieldGroup, FieldLabel, FieldLegend, FieldSet } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { SelectControl } from "../components/primitives";
import { Textarea } from "@/components/ui/textarea";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { cn } from "@/lib/utils";
import type { Lang } from "../types";
import type { KeyValueRow } from "./types";

/** Deployment-specific field compositions share labels, help and validation. */
export function DeployFormSection({ id, title, description, children }: {
  id?: string;
  title: string;
  description?: ReactNode;
  children: ReactNode;
}) {
  const titleId = useId();
  return <Card id={id} className="min-w-0 scroll-mt-6" aria-labelledby={titleId}>
    <CardHeader>
      <CardTitle id={titleId}>{title}</CardTitle>
      {description ? <CardDescription>{description}</CardDescription> : null}
    </CardHeader>
    <CardContent className="@container flex flex-col gap-6">{children}</CardContent>
  </Card>;
}

type FieldCopy = { label: string; description?: ReactNode; error?: string; wide?: boolean };

export function DeployTextField({ label, description, error, wide, ...props }: FieldCopy & ComponentProps<typeof Input>) {
  const generatedId = useId();
  const id = props.id || generatedId;
  const describedBy = [description && `${id}-help`, error && `${id}-error`].filter(Boolean).join(" ") || undefined;
  return <Field className={cn(wide && "@md:col-span-2")} data-disabled={props.disabled || undefined} data-invalid={Boolean(error) || undefined}>
    <FieldLabel htmlFor={id}>{label}</FieldLabel>
    <Input {...props} id={id} aria-invalid={Boolean(error) || undefined} aria-describedby={describedBy} />
    {description ? <FieldDescription id={`${id}-help`}>{description}</FieldDescription> : null}
    {error ? <FieldError id={`${id}-error`}>{error}</FieldError> : null}
  </Field>;
}

export function DeployTextareaField({ label, description, error, wide, ...props }: FieldCopy & ComponentProps<typeof Textarea>) {
  const generatedId = useId();
  const id = props.id || generatedId;
  const describedBy = [description && `${id}-help`, error && `${id}-error`].filter(Boolean).join(" ") || undefined;
  return <Field className={cn(wide && "@md:col-span-2")} data-disabled={props.disabled || undefined} data-invalid={Boolean(error) || undefined}>
    <FieldLabel htmlFor={id}>{label}</FieldLabel>
    <Textarea {...props} id={id} aria-invalid={Boolean(error) || undefined} aria-describedby={describedBy} />
    {description ? <FieldDescription id={`${id}-help`}>{description}</FieldDescription> : null}
    {error ? <FieldError id={`${id}-error`}>{error}</FieldError> : null}
  </Field>;
}

export function DeploySelectField({ label, description, error, wide, value, onChange, options, disabled }: FieldCopy & {
  value: string;
  onChange: (value: string) => void;
  options: Array<{ value: string; label: string | undefined; disabled?: boolean }>;
  disabled?: boolean;
}) {
  const id = useId();
  const describedBy = [description && `${id}-help`, error && `${id}-error`].filter(Boolean).join(" ") || undefined;
  const items = options.map((option) => ({ ...option, label: option.label || option.value }));
  return <Field className={cn(wide && "@md:col-span-2")} data-disabled={disabled || undefined} data-invalid={Boolean(error) || undefined}>
    <FieldLabel htmlFor={id}>{label}</FieldLabel>
    <SelectControl id={id} value={value} options={items} disabled={disabled} onChange={onChange} className="min-w-0" aria-invalid={Boolean(error) || undefined} aria-describedby={describedBy} />
    {description ? <FieldDescription id={`${id}-help`}>{description}</FieldDescription> : null}
    {error ? <FieldError id={`${id}-error`}>{error}</FieldError> : null}
  </Field>;
}

export function DeployCheckboxField({ label, description, checked, onCheckedChange, disabled }: {
  label: string;
  description?: ReactNode;
  checked: boolean;
  onCheckedChange: (value: boolean) => void;
  disabled?: boolean;
}) {
  const id = useId();
  return <Field orientation="horizontal" data-disabled={disabled || undefined}>
    <Checkbox id={id} checked={checked} onCheckedChange={onCheckedChange} disabled={disabled} aria-describedby={description ? `${id}-help` : undefined} />
    <FieldContent>
      <FieldLabel htmlFor={id}>{label}</FieldLabel>
      {description ? <FieldDescription id={`${id}-help`}>{description}</FieldDescription> : null}
    </FieldContent>
  </Field>;
}

export function DeployEnvironmentFields({ lang, rows, onChange, label }: {
  lang: Lang;
  rows: KeyValueRow[];
  onChange: (rows: KeyValueRow[]) => void;
  label: string;
}) {
  const zh = lang === "zh";
  const prefix = useId();
  const updateRow = (id: string, next: Partial<KeyValueRow>) => onChange(rows.map((row) => row.id === id ? { ...row, ...next } : row));
  const addRow = (kind: KeyValueRow["kind"]) => onChange([...rows, { id: `env-${Date.now()}`, key: "", value: "", kind }]);
  return <FieldSet>
    <FieldLegend>{label}</FieldLegend>
    <FieldDescription>{zh ? <>普通变量会写入配置；密钥使用 ${"{NAME}"} 引用，明文请先存入 Luma Control。</> : <>Plain variables are written to the configuration. Reference secrets with ${"{NAME}"}; store plaintext in Luma Control first.</>}</FieldDescription>
    <div className="flex flex-wrap gap-2">
      <Button type="button" variant="outline" size="sm" onClick={() => addRow("plain")}><Plus data-icon="inline-start" />{zh ? "添加变量" : "Add variable"}</Button>
      <Button type="button" variant="outline" size="sm" onClick={() => addRow("secret")}><Plus data-icon="inline-start" />{zh ? "添加密钥引用" : "Add secret reference"}</Button>
    </div>
    {rows.length ? <FieldGroup>{rows.map((row, index) => <FieldSet key={row.id}>
      <FieldLegend variant="label">{zh ? `变量 ${index + 1}` : `Variable ${index + 1}`}</FieldLegend>
      <FieldGroup className="grid grid-cols-1 items-start gap-4 @md:grid-cols-2">
        <DeployTextField label={zh ? "变量名" : "Name"} value={row.key} onChange={(event) => updateRow(row.id, { key: event.target.value })} placeholder="NAME" error={!row.key.trim() && row.value.trim() ? (zh ? "请输入变量名" : "Enter a variable name") : undefined} />
        <DeployTextField label={row.kind === "secret" ? (zh ? "密钥引用" : "Secret reference") : (zh ? "变量值" : "Value")} value={row.value} onChange={(event) => updateRow(row.id, { value: event.target.value })} placeholder={row.kind === "secret" ? "${SECRET_NAME}" : "value"} error={row.kind === "secret" && row.key.trim() && !/^\$\{[A-Za-z_][A-Za-z0-9_]*\}$/.test(row.value.trim()) ? (zh ? "使用 ${NAME} 格式引用密钥" : "Reference a secret with ${NAME}") : undefined} />
        <Field>
          <FieldLabel id={`${prefix}-${row.id}-kind`}>{zh ? "变量类型" : "Variable type"}</FieldLabel>
          <ToggleGroup aria-labelledby={`${prefix}-${row.id}-kind`} variant="outline" value={[row.kind || "plain"]} onValueChange={(values) => { if (values[0]) updateRow(row.id, { kind: values[0] as KeyValueRow["kind"] }); }}>
            <ToggleGroupItem value="plain">{zh ? "普通变量" : "Plain"}</ToggleGroupItem>
            <ToggleGroupItem value="secret">{zh ? "密钥引用" : "Secret"}</ToggleGroupItem>
          </ToggleGroup>
        </Field>
        <div className="flex justify-end @md:self-end">
          <Button type="button" variant="ghost" size="sm" aria-label={zh ? `删除变量 ${row.key || index + 1}` : `Remove variable ${row.key || index + 1}`} onClick={() => onChange(rows.filter((item) => item.id !== row.id))}><Trash2 data-icon="inline-start" />{zh ? "删除" : "Remove"}</Button>
        </div>
      </FieldGroup>
    </FieldSet>)}</FieldGroup> : <Empty>
      <EmptyHeader><EmptyTitle>{zh ? "暂无环境变量" : "No environment variables"}</EmptyTitle><EmptyDescription>{zh ? "添加变量或引用已保存的密钥。" : "Add a variable or reference a saved secret."}</EmptyDescription></EmptyHeader>
    </Empty>}
  </FieldSet>;
}
