import { useMemo, useState, type AriaAttributes, type ReactNode } from "react";
import { Link } from "react-router";
import { ArrowDown, ArrowRight, ChevronsUpDown } from "@/components/icons";
import { ArrowUp } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from "@/components/ui/empty";
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { cn } from "@/lib/utils";

export interface SelectOption {
  value: string;
  label: string;
  disabled?: boolean;
}

export function SelectControl({
  value,
  options,
  onChange,
  placeholder = "-",
  disabled = false,
  ariaLabel,
  className = "",
  id,
  ...accessibility
}: {
  value: string;
  options: SelectOption[];
  onChange: (value: string) => void;
  placeholder?: ReactNode;
  disabled?: boolean;
  ariaLabel?: string;
  className?: string;
  id?: string;
  "aria-describedby"?: string;
  "aria-labelledby"?: string;
  "aria-invalid"?: AriaAttributes["aria-invalid"];
}) {
  const emptyValue = "__empty__";
  const encode = (item: string) => (item === "" ? emptyValue : item);
  const items = options.map((option) => ({
    value: encode(option.value),
    label: option.label || option.value || String(placeholder),
    disabled: option.disabled,
  }));

  return (
    <Select
      value={encode(value)}
      onValueChange={(next) => {
        if (typeof next === "string") onChange(next === emptyValue ? "" : next);
      }}
      disabled={disabled}
      items={items}
    >
      <SelectTrigger id={id} aria-label={ariaLabel} {...accessibility} className={cn("min-w-0", className)}>
        <SelectValue placeholder={placeholder} />
      </SelectTrigger>
      <SelectContent alignItemWithTrigger={false} side="bottom">
        <SelectGroup>
          {options.map((option) => (
            <SelectItem key={encode(option.value)} value={encode(option.value)} disabled={option.disabled}>
              {option.label}
            </SelectItem>
          ))}
        </SelectGroup>
      </SelectContent>
    </Select>
  );
}

export function PrimaryCell({ title, meta }: { title: ReactNode; meta?: ReactNode }) {
  return (
    <span className="flex min-w-0 flex-col gap-0.5">
      <strong className="truncate text-[0.8125rem] font-medium text-foreground">{title || "—"}</strong>
      {meta ? <small className="truncate text-xs text-muted-foreground">{meta}</small> : null}
    </span>
  );
}

export function StatTile({
  label,
  icon: Icon,
  value,
  detail,
  footer,
  tone = "normal",
  to,
  className,
}: {
  label: ReactNode;
  icon?: (props: { className?: string; "aria-hidden"?: boolean }) => ReactNode;
  value: ReactNode;
  detail?: ReactNode;
  footer?: ReactNode;
  tone?: "normal" | "warning" | "critical";
  to?: string;
  className?: string;
}) {
  const inner = (
    <>
      <span className="flex min-w-0 items-center gap-2 text-xs font-medium text-muted-foreground">
        {Icon ? (
          <Icon
            className={cn(
              "size-3.5 shrink-0",
              tone === "critical" ? "text-destructive" : tone === "warning" ? "text-warning" : "text-muted-foreground",
            )}
            aria-hidden={true}
          />
        ) : null}
        <span className="truncate">{label}</span>
        {to ? (
          <ArrowRight
            className="ml-auto size-3.5 shrink-0 -translate-x-0.5 opacity-0 transition-[opacity,transform] group-hover:translate-x-0 group-hover:opacity-100"
            aria-hidden="true"
          />
        ) : null}
      </span>
      <span className="flex min-w-0 items-baseline gap-1.5 tabular-nums">
        <strong
          className={cn(
            "text-2xl/none font-semibold tracking-[-0.02em]",
            tone === "critical" && "text-destructive-foreground",
            tone === "warning" && "text-warning-foreground",
          )}
        >
          {value}
        </strong>
        {detail ? <span className="truncate text-xs text-muted-foreground">{detail}</span> : null}
      </span>
      {footer ? <div className="min-w-0 truncate text-xs text-muted-foreground">{footer}</div> : null}
    </>
  );

  const baseCls = cn(
    "group flex min-w-0 flex-col gap-2 rounded-lg bg-card px-4 py-3.5 shadow-xs ring-1 ring-border outline-none",
    to && "transition-[box-shadow,background-color] hover:ring-foreground/20 focus-visible:ring-2 focus-visible:ring-ring",
    className,
  );

  if (to) {
    return (
      <Link to={to} className={baseCls}>
        {inner}
      </Link>
    );
  }
  return <div className={baseCls}>{inner}</div>;
}

export function UsageMeter({
  value,
  label,
  className,
}: {
  value?: number;
  label?: string;
  className?: string;
}) {
  const known = typeof value === "number" && Number.isFinite(value);
  const clamped = known ? Math.max(0, Math.min(100, value)) : 0;
  const tone = !known ? "normal" : clamped >= 90 ? "critical" : clamped >= 75 ? "warning" : "normal";
  return (
    <span className={cn("inline-flex min-w-0 items-center gap-2", className)}>
      <span
        role="meter"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={known ? Math.round(clamped) : undefined}
        aria-valuetext={known ? `${clamped.toFixed(1)}%` : "—"}
        className="relative h-1.5 w-full min-w-12 flex-1 overflow-hidden rounded-full bg-secondary"
      >
        <span
          className={cn(
            "absolute inset-y-0 left-0 rounded-full transition-[width] duration-300",
            tone === "critical" ? "bg-destructive" : tone === "warning" ? "bg-warning" : "bg-primary/80",
          )}
          style={{ width: `${Math.max(known && clamped > 0 ? 2 : 0, clamped)}%` }}
        />
      </span>
      <span
        className={cn(
          "shrink-0 text-right text-xs tabular-nums",
          known ? "text-foreground" : "text-subtle-foreground",
          tone === "critical" && "font-medium text-destructive-foreground",
          tone === "warning" && "font-medium text-warning-foreground",
        )}
      >
        {known ? `${clamped.toFixed(1)}%` : "—"}
      </span>
    </span>
  );
}

export function StatePill({
  label,
  tone = "outline",
}: {
  label: ReactNode;
  tone?: "success" | "warning" | "destructive" | "info" | "outline" | "secondary";
}) {
  if (!label) return null;
  if (tone === "info") {
    return <Badge variant="secondary">{label}</Badge>;
  }
  return (
    <Badge
      variant={tone}
      appearance={tone === "success" || tone === "outline" ? "dot" : "filled"}
    >
      {label}
    </Badge>
  );
}

export interface DataColumn<T> {
  key: string;
  header: string;
  align?: "left" | "right";
  sortable?: boolean;
  sortValue?: (row: T) => string | number;
  className?: string;
  render: (row: T, index: number) => ReactNode;
}

export function DataTable<T>({
  ariaLabel,
  rows,
  rowKey,
  columns,
  defaultSortKey,
  defaultSortDir = "desc",
  loading = false,
  emptyTitle = "暂无记录",
  emptyDescription,
  emptyAction,
  onRowClick,
}: {
  ariaLabel: string;
  rows: T[];
  rowKey: (row: T, index: number) => string;
  columns: DataColumn<T>[];
  defaultSortKey?: string;
  defaultSortDir?: "asc" | "desc";
  loading?: boolean;
  emptyTitle?: string;
  emptyDescription?: ReactNode;
  emptyAction?: ReactNode;
  onRowClick?: (row: T) => void;
}) {
  const [sortKey, setSortKey] = useState<string | undefined>(defaultSortKey);
  const [sortDir, setSortDir] = useState<"asc" | "desc">(defaultSortDir);

  const sortedRows = useMemo(() => {
    if (!sortKey) return rows;
    const col = columns.find((c) => c.key === sortKey);
    if (!col?.sortable || !col.sortValue) return rows;
    const copy = rows.slice();
    copy.sort((a, b) => {
      const va = col.sortValue!(a);
      const vb = col.sortValue!(b);
      let cmp = 0;
      if (typeof va === "number" && typeof vb === "number") {
        cmp = va - vb;
      } else {
        cmp = String(va).localeCompare(String(vb), "zh-CN", { numeric: true });
      }
      return sortDir === "asc" ? cmp : -cmp;
    });
    return copy;
  }, [rows, columns, sortKey, sortDir]);

  if (loading && rows.length === 0) {
    return (
      <div className="flex flex-col gap-2.5 py-2" role="status" aria-label="正在加载数据">
        <Skeleton className="h-9 w-full" />
        <Skeleton className="h-9 w-full" />
        <Skeleton className="h-9 w-4/5" />
      </div>
    );
  }

  if (rows.length === 0) {
    return (
      <Empty className="py-10">
        <EmptyHeader>
          <EmptyTitle>{emptyTitle}</EmptyTitle>
          {emptyDescription ? <EmptyDescription>{emptyDescription}</EmptyDescription> : null}
        </EmptyHeader>
        {emptyAction}
      </Empty>
    );
  }

  const toggleSort = (key: string) => {
    if (sortKey === key) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir("desc");
    }
  };

  return (
    <Table
      aria-label={ariaLabel}
      containerProps={{
        tabIndex: 0,
        role: "region",
        "aria-label": `${ariaLabel}（可横向滚动）`,
      }}
    >
      <TableHeader className="sticky top-0 z-10 bg-muted/80 backdrop-blur-xs">
        <TableRow>
          {columns.map((col) => {
            const isSorted = sortKey === col.key;
            const ariaSort = !col.sortable
              ? undefined
              : isSorted
                ? sortDir === "asc"
                  ? "ascending"
                  : "descending"
                : "none";
            return (
              <TableHead
                key={col.key}
                aria-sort={ariaSort}
                className={cn(col.align === "right" && "text-right", col.className)}
              >
                {col.sortable ? (
                  <button
                    type="button"
                    onClick={() => toggleSort(col.key)}
                    className={cn(
                      "inline-flex items-center gap-1 rounded-xs py-0.5 font-medium text-muted-foreground transition-colors hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none",
                      isSorted && "text-foreground",
                      col.align === "right" && "ml-auto",
                    )}
                  >
                    <span>{col.header}</span>
                    {isSorted ? (
                      sortDir === "asc" ? (
                        <ArrowUp className="size-3 shrink-0" aria-hidden="true" />
                      ) : (
                        <ArrowDown className="size-3 shrink-0" aria-hidden="true" />
                      )
                    ) : (
                      <ChevronsUpDown className="size-3 shrink-0 opacity-50" aria-hidden="true" />
                    )}
                  </button>
                ) : (
                  col.header
                )}
              </TableHead>
            );
          })}
        </TableRow>
      </TableHeader>
      <TableBody>
        {sortedRows.map((row, idx) => (
          <TableRow
            key={rowKey(row, idx)}
            onClick={
              onRowClick
                ? (ev) => {
                    const target = ev.target as HTMLElement | null;
                    if (target?.closest("a, button, input, select")) return;
                    onRowClick(row);
                  }
                : undefined
            }
            className={cn(onRowClick && "cursor-pointer")}
          >
            {columns.map((col) => (
              <TableCell
                key={col.key}
                className={cn("align-top", col.align === "right" && "text-right", col.className)}
              >
                {col.render(row, idx)}
              </TableCell>
            ))}
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}
