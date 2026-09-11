"use client"

import * as React from "react"
import { cn } from "cn"
import {
  DayPicker,
  getDefaultClassNames,
  type DayButton,
  type Locale,
} from "react-day-picker"

import { Button, buttonVariants } from "@/components/ui/button"
import { ChevronLeftIcon, ChevronRightIcon, ChevronDownIcon } from "lucide-react"

function Calendar({
  className,
  classNames,
  showOutsideDays = true,
  captionLayout = "label",
  buttonVariant = "ghost",
  locale,
  formatters,
  components,
  ...props
}: React.ComponentProps<typeof DayPicker> & {
  buttonVariant?: React.ComponentProps<typeof Button>["variant"]
}) {
  const defaultClassNames = getDefaultClassNames()

  return (
    <DayPicker
      showOutsideDays={showOutsideDays}
      className={cn("group/calendar w-[252px] bg-background p-2", className)}
      captionLayout={captionLayout}
      locale={locale}
      formatters={{
        formatMonthDropdown: (date) =>
          date.toLocaleString(locale?.code, { month: "short" }),
        ...formatters,
      }}
      classNames={{
        root: cn("w-[252px]", defaultClassNames.root),
        months: cn("relative flex w-[252px] flex-col", defaultClassNames.months),
        month: cn("flex w-[252px] flex-col gap-2", defaultClassNames.month),
        nav: cn(
          "absolute inset-x-0 top-0 z-10 flex h-8 items-center justify-between",
          defaultClassNames.nav
        ),
        button_previous: cn(
          buttonVariants({ variant: buttonVariant, size: "icon" }),
          "size-8 p-0 select-none aria-disabled:opacity-50",
          defaultClassNames.button_previous
        ),
        button_next: cn(
          buttonVariants({ variant: buttonVariant, size: "icon" }),
          "size-8 p-0 select-none aria-disabled:opacity-50",
          defaultClassNames.button_next
        ),
        month_caption: cn(
          "flex h-8 w-[252px] items-center justify-center px-8 text-sm font-medium",
          defaultClassNames.month_caption
        ),
        caption_label: cn("truncate text-sm font-medium", defaultClassNames.caption_label),
        month_grid: cn("flex w-[252px] flex-col gap-1", defaultClassNames.month_grid),
        weekdays: cn("grid w-[252px] grid-cols-7", defaultClassNames.weekdays),
        weekday: cn(
          "flex size-8 items-center justify-center text-xs font-normal text-muted-foreground",
          defaultClassNames.weekday
        ),
        week: cn("grid w-[252px] grid-cols-7", defaultClassNames.week),
        day: cn("flex size-8 items-center justify-center p-0", defaultClassNames.day),
        today: cn("[&_button]:bg-muted", defaultClassNames.today),
        outside: cn("text-muted-foreground opacity-60", defaultClassNames.outside),
        disabled: cn("text-muted-foreground opacity-50", defaultClassNames.disabled),
        hidden: cn("invisible", defaultClassNames.hidden),
        ...classNames,
      }}
      components={{
        Root: ({ className, rootRef, ...props }) => (
          <div data-slot="calendar" ref={rootRef} className={cn("w-[252px]", className)} {...props} />
        ),
        MonthGrid: ({ className, ...props }) => (
          <div role="grid" className={cn("flex w-[252px] flex-col gap-1", className)} {...props} />
        ),
        Weekdays: ({ className, ...props }) => (
          <div className={cn("grid w-[252px] grid-cols-7", className)} {...props} />
        ),
        Weekday: ({ className, ...props }) => (
          <div className={cn("flex size-8 items-center justify-center text-xs text-muted-foreground", className)} {...props} />
        ),
        Week: ({ className, week: _week, ...props }) => (
          <div className={cn("grid w-[252px] grid-cols-7", className)} {...props} />
        ),
        Day: ({ className, day: _day, modifiers: _modifiers, ...props }) => (
          <div className={cn("flex size-8 items-center justify-center p-0", className)} {...props} />
        ),
        Chevron: ({ className, orientation, ...props }) => {
          if (orientation === "left") return <ChevronLeftIcon className={cn("size-4", className)} {...props} />
          if (orientation === "right") return <ChevronRightIcon className={cn("size-4", className)} {...props} />
          return <ChevronDownIcon className={cn("size-4", className)} {...props} />
        },
        DayButton: (props) => <CalendarDayButton locale={locale} {...props} />,
        ...components,
      }}
      {...props}
    />
  )
}

function CalendarDayButton({
  className,
  day,
  modifiers,
  locale,
  ...props
}: React.ComponentProps<typeof DayButton> & { locale?: Partial<Locale> }) {
  const ref = React.useRef<HTMLButtonElement>(null)
  React.useEffect(() => {
    if (modifiers.focused) ref.current?.focus()
  }, [modifiers.focused])

  return (
    <Button
      ref={ref}
      type="button"
      variant="ghost"
      size="icon"
      data-day={day.date.toLocaleDateString(locale?.code)}
      className={cn(
        "size-8 rounded-md p-0 font-normal",
        modifiers.selected && "bg-primary text-primary-foreground hover:bg-primary hover:text-primary-foreground",
        modifiers.today && !modifiers.selected && "bg-muted",
        className
      )}
      {...props}
    />
  )
}

export { Calendar, CalendarDayButton }
