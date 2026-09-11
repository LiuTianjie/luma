import { useState } from "react";
import { CalendarIcon } from "lucide-react";
import { zhCN } from "react-day-picker/locale";
import { Button } from "@/components/ui/button";
import { Calendar } from "@/components/ui/calendar";
import { Input } from "@/components/ui/input";
import { Popover, PopoverContent, PopoverTitle, PopoverTrigger } from "@/components/ui/popover";
import { cn } from "@/lib/utils";
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

export function DateTimePicker({
  value,
  onChange,
  placeholder,
  lang,
  defaultTime = "00:00",
}: {
  value: string;
  onChange: (value: string) => void;
  placeholder: string;
  lang: Lang;
  defaultTime?: string;
}) {
  const zh = lang === "zh";
  const selected = parseLocalDateTime(value);
  const time = value.slice(11, 16) || defaultTime;
  const [open, setOpen] = useState(false);

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
        render={
          <Button
            type="button"
            variant="outline"
            className="w-full min-w-0 justify-between font-normal"
          />
        }
      >
        <span className={cn("truncate", !selected && "text-muted-foreground")}>
          {selected ? formatDisplay(selected, lang) : placeholder}
        </span>
        <CalendarIcon data-icon="inline-end" />
      </PopoverTrigger>
      <PopoverContent align="start" className="w-[268px] p-0">
        <PopoverTitle className="sr-only">{placeholder}</PopoverTitle>
        <Calendar
          mode="single"
          selected={selected}
          onSelect={(day) => apply(day, selected ? time : defaultTime)}
          locale={zh ? zhCN : undefined}
          captionLayout="label"
        />
        <div className="flex items-center gap-2 border-t px-2 py-2">
          <Input
            type="time"
            value={selected ? time : defaultTime}
            disabled={!selected}
            onChange={(event) => {
              if (!selected) return;
              apply(selected, event.target.value || defaultTime);
            }}
            className="h-8 w-[7.5rem]"
          />
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
        </div>
      </PopoverContent>
    </Popover>
  );
}
