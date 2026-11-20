import { mergeProps } from "@base-ui/react/merge-props"
import { useRender } from "@base-ui/react/use-render"
import { cva, type VariantProps } from "class-variance-authority"
import { cn } from "cn"

// Kumo badges. Neutral facts are soft grey; tones carry meaning. `appearance="dot"`
// is the quiet status form (ring, coloured dot, body text) used for steady states so
// a page of healthy rows does not shout; the filled tints are kept for states that
// need a look.
const badgeVariants = cva(
  "group/badge inline-flex h-5 w-fit shrink-0 items-center justify-center gap-1 overflow-hidden rounded-full border border-transparent px-2 text-xs font-medium whitespace-nowrap transition-colors focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50 has-data-[icon=inline-end]:pr-1.5 has-data-[icon=inline-start]:pl-1.5 aria-invalid:border-destructive [&>svg]:pointer-events-none [&>svg]:size-3!",
  {
    variants: {
      variant: {
        default: "bg-primary text-primary-foreground [a]:hover:bg-primary-hover",
        secondary: "bg-secondary text-secondary-foreground [a]:hover:bg-accent",
        outline: "border-border bg-background text-foreground [a]:hover:bg-accent",
        ghost: "hover:bg-muted hover:text-muted-foreground",
        link: "text-link underline-offset-4 hover:underline",
        info: "bg-info-soft text-info-foreground",
        success: "bg-success-soft text-success-foreground",
        warning: "bg-warning-soft text-warning-foreground",
        destructive: "bg-destructive-soft text-destructive-foreground",
      },
      appearance: {
        filled: "",
        dot: "gap-1.5 border-border bg-background pl-1.5 text-foreground before:size-1.5 before:shrink-0 before:rounded-full before:bg-(--dot) before:content-['']",
      },
    },
    compoundVariants: [
      { appearance: "dot", variant: "success", className: "[--dot:var(--success)]" },
      { appearance: "dot", variant: "warning", className: "[--dot:var(--warning)]" },
      { appearance: "dot", variant: "destructive", className: "[--dot:var(--destructive)]" },
      { appearance: "dot", variant: "info", className: "[--dot:var(--info)]" },
      { appearance: "dot", variant: ["outline", "secondary", "default", "ghost", "link"], className: "[--dot:var(--subtle-foreground)]" },
    ],
    defaultVariants: {
      variant: "default",
      appearance: "filled",
    },
  }
)

function Badge({
  className,
  variant = "default",
  appearance = "filled",
  render,
  ...props
}: useRender.ComponentProps<"span"> & VariantProps<typeof badgeVariants>) {
  return useRender({
    defaultTagName: "span",
    props: mergeProps<"span">(
      {
        className: cn(badgeVariants({ variant, appearance }), className),
      },
      props
    ),
    render,
    state: {
      slot: "badge",
      variant,
    },
  })
}

export { Badge, badgeVariants }
