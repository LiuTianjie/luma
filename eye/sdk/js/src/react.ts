import { getLumaEye, type LumaEyeClient } from "./index";

export interface ReactErrorInfo {
  componentStack?: string | null;
}

/**
 * Captures a React ErrorBoundary crash onto Track 2 with full componentStack and Ring Buffer breadcrumbs.
 */
export function captureReactError(error: Error, info?: ReactErrorInfo, client?: LumaEyeClient | null): void {
  const target = client ?? getLumaEye();
  if (!target) return;
  target.captureError(error, {
    tags: { source: "react.error_boundary" },
    extra: info?.componentStack ? { componentStack: info.componentStack } : undefined,
  });
}

/**
 * Zero-peer-dependency factory returning a React class ErrorBoundary bound to React.Component.
 */
export function createReactErrorBoundary<P extends { fallback?: unknown; children?: unknown }>(
  ReactModule: {
    Component: new (props: P) => {
      props: P;
      state: { hasError: boolean; error: Error | null };
    };
  },
  client?: LumaEyeClient
) {
  return class LumaEyeErrorBoundary extends ReactModule.Component {
    state = { hasError: false, error: null as Error | null };

    static getDerivedStateFromError(error: Error) {
      return { hasError: true, error };
    }

    componentDidCatch(error: Error, info: ReactErrorInfo) {
      captureReactError(error, info, client);
    }

    render() {
      if (this.state.hasError) {
        return this.props.fallback ?? null;
      }
      return this.props.children;
    }
  };
}
