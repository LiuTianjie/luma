import { cn } from "cn"

function Skeleton({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="skeleton"
      className={cn("animate-pulse rounded-md bg-foreground/[0.08] dark:bg-foreground/[0.12]", className)}
      {...props}
    />
  )
}

export { Skeleton }
