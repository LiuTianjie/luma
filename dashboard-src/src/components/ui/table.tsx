"use client"

import * as React from "react"
import { cn } from "cn"

// Shaded header band, hairline row dividers. When a table is the only content of a
// card it runs edge to edge; its first and last cells then take the card gutter so
// text still lines up with the card title.
function Table({ className, containerProps, ...props }: React.ComponentProps<"table"> & {
  containerProps?: React.ComponentProps<"div">
}) {
  return (
    <div
      data-slot="table-container"
      {...containerProps}
      className={cn(
        "relative w-full overflow-x-auto",
        "in-data-[slot=card-content]:only:border-t in-data-[slot=card-content]:only:border-hairline",
        "in-data-[slot=card-content]:only:[&_tr>*:first-child]:pl-(--card-spacing) in-data-[slot=card-content]:only:[&_tr>*:last-child]:pr-(--card-spacing)",
        containerProps?.className
      )}
    >
      <table
        data-slot="table"
        className={cn("w-full caption-bottom text-[0.8125rem] tabular-nums", className)}
        {...props}
      />
    </div>
  )
}

function TableHeader({ className, ...props }: React.ComponentProps<"thead">) {
  return (
    <thead
      data-slot="table-header"
      className={cn(
        "bg-muted/70 [&_tr]:border-b [&_tr]:border-hairline [&_tr]:hover:bg-transparent [&_th]:h-9 [&_th]:py-0 [&_th]:text-xs",
        className
      )}
      {...props}
    />
  )
}

function TableBody({ className, ...props }: React.ComponentProps<"tbody">) {
  return (
    <tbody
      data-slot="table-body"
      className={cn("[&_tr:last-child]:border-0", className)}
      {...props}
    />
  )
}

function TableFooter({ className, ...props }: React.ComponentProps<"tfoot">) {
  return (
    <tfoot
      data-slot="table-footer"
      className={cn(
        "border-t border-hairline bg-muted/50 font-medium [&>tr]:last:border-b-0",
        className
      )}
      {...props}
    />
  )
}

function TableRow({ className, ...props }: React.ComponentProps<"tr">) {
  return (
    <tr
      data-slot="table-row"
      className={cn(
        "border-b border-hairline transition-colors hover:bg-muted/45 has-aria-expanded:bg-muted/50 data-[state=selected]:bg-muted",
        className
      )}
      {...props}
    />
  )
}

// Column headers are small muted labels. Row headers (scope="row") in key/value
// tables read as labels beside their value: a fixed label column, body size,
// top-aligned so multi-line values line up. Caller classes still win via cn().
function TableHead({ className, scope, ...props }: React.ComponentProps<"th">) {
  return (
    <th
      data-slot="table-head"
      scope={scope}
      className={cn(
        "px-3 text-left align-middle whitespace-nowrap text-muted-foreground [&:has([role=checkbox])]:pr-0",
        scope === "row" ? "w-40 py-2.5 align-top text-[0.8125rem] font-normal" : "py-2 font-medium",
        className
      )}
      {...props}
    />
  )
}

function TableCell({ className, ...props }: React.ComponentProps<"td">) {
  return (
    <td
      data-slot="table-cell"
      className={cn(
        "px-3 py-2.5 align-middle whitespace-nowrap [&:has([role=checkbox])]:pr-0",
        className
      )}
      {...props}
    />
  )
}

function TableCaption({
  className,
  ...props
}: React.ComponentProps<"caption">) {
  return (
    <caption
      data-slot="table-caption"
      className={cn("mt-4 text-sm text-muted-foreground", className)}
      {...props}
    />
  )
}

export {
  Table,
  TableHeader,
  TableBody,
  TableFooter,
  TableHead,
  TableRow,
  TableCell,
  TableCaption,
}
