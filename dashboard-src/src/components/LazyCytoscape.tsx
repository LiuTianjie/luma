import { lazy, Suspense, type ComponentProps } from "react";
import type CytoscapeComponentType from "react-cytoscapejs";
import { Skeleton } from "@/components/ui/skeleton";

const InnerCytoscape = lazy(() => import("react-cytoscapejs"));

export function LazyCytoscape(props: ComponentProps<typeof CytoscapeComponentType>) {
  return (
    <Suspense fallback={<Skeleton className="h-[360px] w-full rounded-lg" />}>
      <InnerCytoscape {...props} />
    </Suspense>
  );
}
