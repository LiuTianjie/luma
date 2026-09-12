import { useId, useState, type ComponentProps } from "react";
import { CalendarIcon } from "lucide-react";
import { zhCN } from "react-day-picker/locale";
import { Button } from "@/components/ui/button";
import { Calendar } from "@/components/ui/calendar";
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Popover, PopoverContent, PopoverTitle, PopoverTrigger } from "@/components/ui/popover";
import { Separator } from "@/components/ui/separator";
import type { Lang } from "../types";

function parseLocalDateTime(value: string): Date | undefined {
  if (!value) return undefined;
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? date : undefined;
}

function toLocalDateTimeValue(date: Date): string {
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 16);
}

function formatDisplay(date: Date | undefined, lang: Lang): string {
  if (!date) return "";
  return date.toLocaleString(lang === "zh" ? "zh-CN" : undefined, {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

type DateTimePickerProps = {
  value: string;
  onChange: (value: string) => void;
  placeholder: string;
  lang: Lang;
  defaultTime?: string;
} & Pick<ComponentProps<typeof Button>, "id" | "aria-invalid" | "aria-describedby" | "aria-labelledby" | "aria-label" | "disabled">;

export function DateTimePicker({
  value,
  onChange,
  placeholder,
  lang,
  defaultTime = "00:00",
  disabled,
  ...triggerProps
}: DateTimePickerProps) {
  const zh = lang === "zh";
  const selected = parseLocalDateTime(value);
  const time = value.slice(11, 16) || defaultTime;
  const [open, setOpen] = useState(false);
  const timeInputId = useId();

  const apply = (nextDate: Date | undefined, nextTime: string) => {
    if (!nextDate) {
      onChange("");
      return;
    }
    const [hours, minutes] = nextTime.split(":").map((part) => Number(part));
    const next = new Date(nextDate);
    next.setHours(Number.isFinite(hours) ? hours : 0, Number.isFinite(minutes) ? minutes : 0, 0, 0);
    onChange(toLocalDateTimeValue(next));
  };

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger
        {...triggerProps}
        disabled={disabled}
        render={
          <Button
            type="button"
            variant="outline"
            className="w-full min-w-0 justify-between"
          />
        }
      >
        <span className="truncate">
          {selected ? formatDisplay(selected, lang) : placeholder}
        </span>
        <CalendarIcon data-icon="inline-end" />
      </PopoverTrigger>
      <PopoverContent align="start" className="w-auto max-w-[calc(100vw-2rem)] p-0">
        <PopoverTitle className="sr-only">{placeholder}</PopoverTitle>
        <Calendar
          mode="single"
          selected={selected}
          onSelect={(day) => apply(day, selected ? time : defaultTime)}
          locale={zh ? zhCN : undefined}
          captionLayout="label"
        />
        <Separator />
        <FieldGroup className="px-3">
          <Field orientation="horizontal" data-disabled={!selected}>
            <FieldLabel htmlFor={timeInputId}>{zh ? "时间" : "Time"}</FieldLabel>
            <Input
              id={timeInputId}
              type="time"
              value={selected ? time : defaultTime}
              disabled={!selected}
              onChange={(event) => {
                if (!selected) return;
                apply(selected, event.target.value || defaultTime);
              }}
              className="w-32"
            />
          </Field>
        </FieldGroup>
        <div className="flex justify-end gap-2 px-3 pb-3">
          <Button
            type="button"
            variant="ghost"
            size="sm"
            disabled={!value}
            onClick={() => {
              onChange("");
              setOpen(false);
            }}
          >
            {zh ? "清除" : "Clear"}
          </Button>
          <Button type="button" size="sm" onClick={() => setOpen(false)}>
            {zh ? "完成" : "Done"}
          </Button>
        </div>
      </PopoverContent>
    </Popover>
  );
}
