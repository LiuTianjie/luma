import { useEffect, useRef, useState, type RefObject } from "react";
import { createPortal } from "react-dom";
import { FitAddon } from "@xterm/addon-fit";
import { Terminal } from "@xterm/xterm";
import { ArrowLeft, TerminalSquare, X } from "lucide-react";
import "@xterm/xterm/css/xterm.css";
import type { DashboardNode, DashboardService, Lang } from "../types";
import { t } from "../i18n";
import { OverlayShell } from "../useOverlay";
import "./terminalSession.css";

type TerminalStatus = "connecting" | "connected" | "ended" | "error";

// The application detail dialog is also portalled to <body>. Keep the terminal
// at the same root so the dashboard shell's isolated stacking context cannot
// trap it underneath that dialog; the overlay z-index then orders them correctly.
const TERMINAL_ROOT = typeof document === "undefined" ? null : document.body;

export type TerminalSessionTarget = {
  kind: "node" | "container";
  node?: DashboardNode;
  service?: DashboardService;
  stack?: string;
};

type TerminalDrawerProps = {
  lang: Lang;
  target: TerminalSessionTarget;
  token: string;
  onClose: () => void;
  inline?: boolean;
};

export function TerminalDrawer(props: TerminalDrawerProps) {
  if (props.inline) return <TerminalContent {...props} />;
  if (!TERMINAL_ROOT) return null;
  return createPortal(
    <OverlayShell<HTMLElement> className="terminal-modal-backdrop" onClose={props.onClose}>
      {(panelRef) => <TerminalContent {...props} panelRef={panelRef} />}
    </OverlayShell>,
    TERMINAL_ROOT,
  );
}

function TerminalContent({ lang, target, token, onClose, inline = false, panelRef }: TerminalDrawerProps & { panelRef?: RefObject<HTMLElement | null> }) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const socketRef = useRef<WebSocket | null>(null);
  const terminalRef = useRef<Terminal | null>(null);
  const fitRef = useRef<FitAddon | null>(null);
  const sessionRef = useRef("");
  const reportedCloseRef = useRef(false);
  const [status, setStatus] = useState<TerminalStatus>("connecting");
  const isContainer = target.kind === "container";
  const title = isContainer
    ? `${target.stack || target.service?.stack || "-"} / ${target.service?.name || target.service?.fullName || "-"}`
    : (target.node?.name || "-");
  const meta = isContainer
    ? [target.node?.name, target.node?.region, "container"].filter(Boolean).join(" · ")
    : `${target.node?.region || "-"} · ${target.node?.agentOs || "agent"}`;

  useEffect(() => {
    let active = true;
    const container = containerRef.current;
    if (!container) return;
    setStatus("connecting");
    reportedCloseRef.current = false;
    const term = new Terminal({
      cursorBlink: true,
      convertEol: true,
      fontFamily: '"Berkeley Mono", "IBM Plex Mono", ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace',
      fontSize: 13,
      theme: {
        background: "#1a1818",
        foreground: "#fdfcfc",
        cursor: "#007aff",
        selectionBackground: "#004085",
      },
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(container);
    fit.fit();
    // Take focus off whatever useOverlay picked (the first control in the
    // dialog): in a terminal the caret belongs in the terminal.
    term.focus();
    terminalRef.current = term;
    fitRef.current = fit;

    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const params = new URLSearchParams();
    if (isContainer) {
      params.set("service", target.service?.fullName || target.service?.name || "");
    } else {
      params.set("node", target.node?.name || "");
    }
    const socket = new WebSocket(`${protocol}//${window.location.host}/v1/terminal/browser?${params.toString()}`);
    socketRef.current = socket;

    const sendResize = () => {
      fit.fit();
      if (!sessionRef.current || socket.readyState !== WebSocket.OPEN) return;
      socket.send(JSON.stringify({ type: "resize", sessionId: sessionRef.current, cols: term.cols, rows: term.rows }));
    };

    const resizeObserver = new ResizeObserver(sendResize);
    resizeObserver.observe(container);

    socket.addEventListener("open", () => {
      if (!active) return;
      term.writeln(lang === "zh"
        ? (isContainer ? "正在连接容器终端..." : "正在连接节点终端...")
        : (isContainer ? "Connecting to container shell..." : "Connecting to node terminal..."));
      socket.send(JSON.stringify({ type: "auth", token }));
    });
    socket.addEventListener("message", (event) => {
      if (!active) return;
      let message: Record<string, unknown>;
      try {
        message = JSON.parse(String(event.data));
      } catch {
        return;
      }
      const kind = String(message.type || "");
      if (kind === "open") {
        setStatus("connected");
        sessionRef.current = String(message.sessionId || "");
        sendResize();
        socket.send(JSON.stringify({ type: "input", sessionId: sessionRef.current, data: "\r" }));
      } else if (kind === "output") {
        term.write(String(message.data || ""));
      } else if (kind === "exit") {
        setStatus("ended");
        term.writeln("");
        term.writeln(`Session ended (${message.exitCode ?? "-"})`);
      } else if (kind === "error") {
        reportedCloseRef.current = true;
        setStatus("error");
        term.writeln("");
        term.writeln(String(message.message || "Terminal error"));
      }
    });
    socket.addEventListener("close", (event) => {
      if (!active) return;
      if (!sessionRef.current && !reportedCloseRef.current) {
        setStatus("error");
        term.writeln("");
        term.writeln(
          lang === "zh"
            ? `Terminal 连接已关闭（${event.code || "-"}）。请确认该节点的 terminal supervisor 已连接到 Luma Control。`
            : `Terminal connection closed (${event.code || "-"}). Confirm the node terminal supervisor is connected to Luma Control.`,
        );
        return;
      }
      setStatus((current) => (current === "ended" || current === "error") ? current : "ended");
    });
    socket.addEventListener("error", () => {
      if (!active) return;
      reportedCloseRef.current = true;
      setStatus("error");
      term.writeln("");
      term.writeln(lang === "zh" ? "Terminal WebSocket 连接失败。" : "Terminal WebSocket connection failed.");
    });

    const disposable = term.onData((data) => {
      if (!sessionRef.current || socket.readyState !== WebSocket.OPEN) return;
      socket.send(JSON.stringify({ type: "input", sessionId: sessionRef.current, data }));
    });

    window.addEventListener("resize", sendResize);
    return () => {
      active = false;
      window.removeEventListener("resize", sendResize);
      resizeObserver.disconnect();
      disposable.dispose();
      if (socket.readyState === WebSocket.OPEN) {
        if (sessionRef.current) {
          socket.send(JSON.stringify({ type: "close", sessionId: sessionRef.current }));
        }
      }
      // Also cancel sockets still connecting when navigating away.
      socket.close();
      term.dispose();
      socketRef.current = null;
      terminalRef.current = null;
      fitRef.current = null;
      sessionRef.current = "";
    };
  }, [lang, isContainer, target.node?.name, target.service?.fullName, target.service?.name, token]);

  const statusLabel = {
    connecting: lang === "zh" ? "连接中" : "Connecting",
    connected: lang === "zh" ? "已连接" : "Connected",
    ended: lang === "zh" ? "已结束" : "Ended",
    error: lang === "zh" ? "错误" : "Error",
  }[status];

  return (
      <section
        className={`terminal-session ${inline ? "terminal-page" : "terminal-modal"} terminal-modal-${status}`}
        style={inline ? { minHeight: "65vh", display: "flex", flexDirection: "column" } : undefined}
        ref={panelRef}
        role={inline ? "region" : "dialog"}
        aria-modal={inline ? undefined : true}
        aria-labelledby="terminal-modal-title"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="terminal-session__header">
          <div className="terminal-session__identity">
            <TerminalSquare size={18} aria-hidden="true" />
            <h2 id="terminal-modal-title" title={title}>{title}</h2>
            <span className="terminal-session__meta" title={meta}>{isContainer ? (lang === "zh" ? "容器" : "Container") : (lang === "zh" ? "节点" : "Node")} · {meta}</span>
          </div>
          <div className="terminal-session__actions">
            <span className={`terminal-session__status is-${status}`} role="status" aria-live="polite">{statusLabel}</span>
            <button type="button" className="terminal-session__close" onClick={onClose}>
              {inline ? <ArrowLeft size={15} aria-hidden="true" /> : <X size={15} aria-hidden="true" />}
              {inline ? (lang === "zh" ? "结束并返回" : "End session and return") : t(lang, "close")}
            </button>
          </div>
        </header>
        <div className="terminal-surface" style={inline ? { flex: 1, minHeight: "55vh" } : undefined} ref={containerRef} />
      </section>
  );
}
