import { Button as ButtonPrimitive } from "@base-ui/react/button"
import { cva, type VariantProps } from "class-variance-authority"
import { cn } from "cn"

// Primary actions use Kumo's emphasis treatment: a short top-lit gradient, a ring a
// shade darker than the fill and a 1px inner highlight. The gradient only darkens
// toward the bottom so white text keeps 4.5:1 at its lightest point. Drawn in one background so
// render-prop buttons need no extra markup. Secondary actions are white controls
// with a hairline ring; ghost actions only tint on hover.
// Tailwind scans source text, so both emphasis strings are spelled out in full.
const EMPHASIS_PRIMARY =
  "[--btn:var(--primary)] bg-(--btn) bg-[linear-gradient(to_bottom,var(--btn),color-mix(in_oklch,var(--btn),black_9%))] text-white shadow-[inset_0_1px_0_0_rgb(255_255_255/0.16),0_0_0_1px_color-mix(in_oklch,var(--btn),black_14%),0_1px_2px_0_var(--shadow-color)] hover:bg-[linear-gradient(to_bottom,color-mix(in_oklch,var(--btn),black_6%),color-mix(in_oklch,var(--btn),black_15%))] active:bg-[linear-gradient(to_bottom,color-mix(in_oklch,var(--btn),black_12%),color-mix(in_oklch,var(--btn),black_12%))]"
const EMPHASIS_DANGER =
  "[--btn:var(--destructive-fill)] bg-(--btn) bg-[linear-gradient(to_bottom,var(--btn),color-mix(in_oklch,var(--btn),black_9%))] text-white shadow-[inset_0_1px_0_0_rgb(255_255_255/0.16),0_0_0_1px_color-mix(in_oklch,var(--btn),black_14%),0_1px_2px_0_var(--shadow-color)] hover:bg-[linear-gradient(to_bottom,color-mix(in_oklch,var(--btn),black_6%),color-mix(in_oklch,var(--btn),black_15%))] active:bg-[linear-gradient(to_bottom,color-mix(in_oklch,var(--btn),black_12%),color-mix(in_oklch,var(--btn),black_12%))]"

const buttonVariants = cva(
  "group/button relative inline-flex shrink-0 items-center justify-center rounded-lg border border-transparent bg-clip-padding text-[0.8125rem] font-medium whitespace-nowrap transition-[color,background-color,border-color,box-shadow] outline-none select-none [@media(pointer:coarse)]:min-h-11 [@media(pointer:coarse)]:min-w-11 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring disabled:pointer-events-none disabled:opacity-50 aria-invalid:border-destructive aria-invalid:ring-3 aria-invalid:ring-destructive/20 dark:aria-invalid:border-destructive/50 dark:aria-invalid:ring-destructive/40 [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-4",
  {
    variants: {
      variant: {
        default: EMPHASIS_PRIMARY,
        outline:
          "bg-background text-foreground shadow-xs ring-1 ring-border hover:bg-accent aria-expanded:bg-accent data-[popup-open]:bg-accent",
        secondary:
          "bg-secondary text-secondary-foreground hover:bg-[color-mix(in_oklch,var(--secondary),var(--foreground)_6%)] aria-expanded:bg-secondary aria-expanded:text-secondary-foreground",
        ghost:
          "text-foreground hover:bg-accent aria-expanded:bg-accent aria-expanded:text-foreground data-[popup-open]:bg-accent",
        destructive:
          "bg-background text-destructive-foreground shadow-xs ring-1 ring-border hover:bg-destructive-soft hover:ring-destructive/30 focus-visible:outline-destructive",
        "destructive-solid": EMPHASIS_DANGER,
        link: "text-link underline-offset-4 hover:underline",
      },
      size: {
        default:
          "h-8 gap-1.5 px-3 has-data-[icon=inline-end]:pr-2.5 has-data-[icon=inline-start]:pl-2.5",
        xs: "h-6 gap-1 rounded-md px-2 text-xs in-data-[slot=button-group]:rounded-lg has-data-[icon=inline-end]:pr-1.5 has-data-[icon=inline-start]:pl-1.5 [&_svg:not([class*='size-'])]:size-3.5",
        sm: "h-7 gap-1 rounded-md px-2.5 text-[0.8125rem] in-data-[slot=button-group]:rounded-lg has-data-[icon=inline-end]:pr-2 has-data-[icon=inline-start]:pl-2 [&_svg:not([class*='size-'])]:size-3.5",
        lg: "h-9 gap-1.5 px-3.5 text-sm has-data-[icon=inline-end]:pr-3 has-data-[icon=inline-start]:pl-3",
        icon: "size-8",
        "icon-xs":
          "size-6 rounded-md in-data-[slot=button-group]:rounded-lg [&_svg:not([class*='size-'])]:size-3.5",
        "icon-sm":
          "size-7 rounded-md in-data-[slot=button-group]:rounded-lg",
        "icon-lg": "size-9",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  }
)

function Button({
  className,
  variant = "default",
  size = "default",
  ...props
}: ButtonPrimitive.Props & VariantProps<typeof buttonVariants>) {
  return (
    <ButtonPrimitive
      data-slot="button"
      className={cn(buttonVariants({ variant, size, className }))}
      {...props}
    />
  )
}

export { Button, buttonVariants }
