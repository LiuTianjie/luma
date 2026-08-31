import { useEffect, useRef, type ReactNode, type RefObject } from "react";

// Shared modal behaviour for every dashboard overlay (detail drawer, application
// detail page, logs modal, terminal, registry confirm dialog).
//
// Three things every overlay owes the user, which were previously implemented
// ad-hoc — or not at all — per overlay:
//   1. Escape closes it. Without this the only exit is finding the close button.
//   2. Focus moves into the overlay on open and returns to the trigger on close,
//      so keyboard and screen-reader users are not left behind the backdrop.
//   3. Tab is confined to the overlay while it is open, so the ~50 focusable
//      controls behind the backdrop stay unreachable.
//
// Pair it with role="dialog" + aria-modal="true" on the returned ref's element.

const FOCUSABLE = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  '[tabindex]:not([tabindex="-1"])',
].join(", ");

function focusableWithin(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE)).filter((element) => {
    if (element.hidden || element.getAttribute("aria-hidden") === "true") return false;
    // offsetParent is null for display:none subtrees; the rect check also skips
    // controls collapsed to zero size (e.g. a not-yet-expanded section).
    return element.offsetParent !== null || element.getBoundingClientRect().height > 0;
  });
}

export function useOverlay<T extends HTMLElement = HTMLElement>(onClose: () => void) {
  const ref = useRef<T | null>(null);
  // Read onClose through a ref so a caller passing an inline arrow does not
  // re-run the effect (and re-steal focus) on every parent render.
  const closeRef = useRef(onClose);
  closeRef.current = onClose;

  useEffect(() => {
    const container = ref.current;
    const previouslyFocused = document.activeElement as HTMLElement | null;
    // Whether we ever took focus. Checked on cleanup instead of re-testing
    // container.contains(activeElement): by the time cleanup runs React has
    // already detached the overlay, so the focused node is no longer a
    // descendant and the containment test always fails.
    let tookFocus = false;

    // Move focus in: prefer the first control, falling back to the container so
    // screen readers announce the dialog even when it is action-free.
    if (container) {
      const targets = focusableWithin(container);
      if (targets.length) targets[0].focus();
      else {
        container.tabIndex = -1;
        container.focus();
      }
      tookFocus = container.contains(document.activeElement);
    }

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        closeRef.current();
        return;
      }
      if (event.key !== "Tab" || !container) return;
      const targets = focusableWithin(container);
      if (!targets.length) {
        event.preventDefault();
        return;
      }
      const first = targets[0];
      const last = targets[targets.length - 1];
      const active = document.activeElement;
      // Wrap at both ends, and pull focus back in if it escaped the overlay
      // (browser chrome, or a control that unmounted while focused).
      if (!container.contains(active)) {
        event.preventDefault();
        first.focus();
      } else if (event.shiftKey && active === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && active === last) {
        event.preventDefault();
        first.focus();
      }
    };

    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      // Hand focus back to whatever opened the overlay, but only when the
      // overlay still owns it: if the user has since clicked or tabbed
      // somewhere else, yanking focus back would be the surprising behaviour.
      //
      // Cleanup runs *before* React detaches the overlay, so at this point
      // focus is usually still on a control inside it. The rAF then confirms
      // the handoff after the DOM settles, covering the other ordering where
      // the node is already gone and the browser has reset focus to <body>.
      if (!tookFocus || !previouslyFocused?.focus) return;
      const owns = (node: Element | null) =>
        !node || node === document.body || !node.isConnected || Boolean(container?.contains(node));
      if (!owns(document.activeElement)) return;
      previouslyFocused.focus();
      requestAnimationFrame(() => {
        const active = document.activeElement;
        if (!active || active === document.body) previouslyFocused.focus();
      });
    };
  }, []);

  return ref;
}

// Backdrop wrapper for overlays whose panel is rendered inline inside a larger
// component. It gives `useOverlay` its own mount boundary (so focus is captured
// when the overlay appears, not on every render of its parent) and hands the
// panel ref down via a render prop. Spread that ref onto the panel element
// together with role="dialog" and aria-modal="true".
//
// Lives here alongside the hook rather than in its own file so the two stay in
// sync; the cost is that Vite cannot Fast Refresh this module (a file exporting
// both a hook and a component), so editing it triggers a full reload in dev.
export function OverlayShell<T extends HTMLElement = HTMLElement>({
  className,
  onClose,
  children,
}: {
  className: string;
  onClose: () => void;
  children: (ref: RefObject<T | null>) => ReactNode;
}) {
  const ref = useOverlay<T>(onClose);
  return (
    <div className={className} onClick={onClose}>
      {children(ref)}
    </div>
  );
}
