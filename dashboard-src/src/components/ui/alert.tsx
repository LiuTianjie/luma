import * as React from "react"
import { cva, type VariantProps } from "class-variance-authority"
import { cn } from "cn"

// Kumo banners: a tinted surface that names its tone with the icon colour and a
// matching hairline. Text stays at body contrast; only the title takes the tone.
const alertVariants = cva(
  "group/alert relative grid w-full gap-0.5 rounded-lg border px-3.5 py-3 text-left text-[0.8125rem] has-data-[slot=alert-action]:relative has-data-[slot=alert-action]:pr-28 has-[>svg]:grid-cols-[auto_1fr] has-[>svg]:gap-x-2.5 *:[svg]:row-span-2 *:[svg]:translate-y-px *:[svg:not([class*='size-'])]:size-4",
  {
    variants: {
      variant: {
        default: "border-info/20 bg-info-soft text-foreground *:[svg]:text-info",
        destructive:
          "border-destructive/25 bg-destructive-soft text-foreground *:data-[slot=alert-title]:text-destructive-foreground *:[svg]:text-destructive",
        warning:
          "border-warning/30 bg-warning-soft text-foreground *:data-[slot=alert-title]:text-warning-foreground *:[svg]:text-warning",
        success:
          "border-success/25 bg-success-soft text-foreground *:[svg]:text-success",
        neutral: "border-border bg-card text-card-foreground *:[svg]:text-muted-foreground",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  }
)

function Alert({
  className,
  variant,
  ...props
}: React.ComponentProps<"div"> & VariantProps<typeof alertVariants>) {
  return (
    <div
      data-slot="alert"
      role="alert"
      className={cn(alertVariants({ variant }), className)}
      {...props}
    />
  )
}

function AlertTitle({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="alert-title"
      className={cn(
        "font-medium group-has-[>svg]/alert:col-start-2 [&_a]:underline [&_a]:underline-offset-3 [&_a]:hover:text-foreground",
        className
      )}
      {...props}
    />
  )
}

function AlertDescription({
  className,
  ...props
}: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="alert-description"
      className={cn(
        "text-[0.8125rem]/5 text-pretty text-muted-foreground [&_a]:underline [&_a]:underline-offset-3 [&_a]:hover:text-foreground [&_p:not(:last-child)]:mb-4",
        className
      )}
      {...props}
    />
  )
}

function AlertAction({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="alert-action"
      className={cn("absolute top-2.5 right-3", className)}
      {...props}
    />
  )
}

export { Alert, AlertTitle, AlertDescription, AlertAction }
