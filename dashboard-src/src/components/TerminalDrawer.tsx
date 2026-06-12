import { useEffect, useRef, useState } from "react";
import { FitAddon } from "@xterm/addon-fit";
import { Terminal } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";
import type { DashboardNode, Lang } from "../types";
import { t } from "../i18n";

type TerminalStatus = "connecting" | "connected" | "ended" | "error";

export function TerminalDrawer({
  lang,
  node,
  token,
  onClose,
}: {
  lang: Lang;
  node: DashboardNode;
  token: string;
  onClose: () => void;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const socketRef = useRef<WebSocket | null>(null);
  const terminalRef = useRef<Terminal | null>(null);
  const fitRef = useRef<FitAddon | null>(null);
  const sessionRef = useRef("");
  const [status, setStatus] = useState<TerminalStatus>("connecting");

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const term = new Terminal({
      cursorBlink: true,
      convertEol: true,
      fontFamily: "SFMono-Regular, Menlo, Monaco, Consolas, monospace",
      fontSize: 13,
      theme: {
        background: "#05070b",
        foreground: "#e6edf3",
        cursor: "#00c2a8",
        selectionBackground: "#245b57",
      },
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(container);
    fit.fit();
    terminalRef.current = term;
    fitRef.current = fit;

    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const params = new URLSearchParams({ node: node.name || "" });
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
      term.writeln(lang === "zh" ? "正在连接节点终端..." : "Connecting to node terminal...");
      socket.send(JSON.stringify({ type: "auth", token }));
    });
    socket.addEventListener("message", (event) => {
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
      } else if (kind === "output") {
        term.write(String(message.data || ""));
      } else if (kind === "exit") {
        setStatus("ended");
        term.writeln("");
        term.writeln(`Session ended (${message.exitCode ?? "-"})`);
      } else if (kind === "error") {
        setStatus("error");
        term.writeln("");
        term.writeln(String(message.message || "Terminal error"));
      }
    });
    socket.addEventListener("close", () => {
      setStatus((current) => current === "ended" ? current : "ended");
    });
    socket.addEventListener("error", () => {
      setStatus("error");
    });

    const disposable = term.onData((data) => {
      if (!sessionRef.current || socket.readyState !== WebSocket.OPEN) return;
      socket.send(JSON.stringify({ type: "input", sessionId: sessionRef.current, data }));
    });

    return () => {
      disposable.dispose();
      resizeObserver.disconnect();
      if (socket.readyState === WebSocket.OPEN && sessionRef.current) {
        socket.send(JSON.stringify({ type: "close", sessionId: sessionRef.current }));
      }
      socket.close();
      term.dispose();
      socketRef.current = null;
      terminalRef.current = null;
      fitRef.current = null;
      sessionRef.current = "";
    };
  }, [lang, node.name, token]);

  const statusLabel = {
    connecting: lang === "zh" ? "连接中" : "Connecting",
    connected: lang === "zh" ? "已连接" : "Connected",
    ended: lang === "zh" ? "已结束" : "Ended",
    error: lang === "zh" ? "错误" : "Error",
  }[status];

  return (
    <div className="terminal-modal-backdrop" onClick={onClose}>
      <section
        className="terminal-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="terminal-modal-title"
        onClick={(event) => event.stopPropagation()}
      >
        <header>
          <div>
            <p className="eyebrow">Terminal</p>
            <h2 id="terminal-modal-title">{node.name || "-"}</h2>
            <span className="terminal-node-meta">{node.region || "-"} · {node.agentOs || "agent"} · {statusLabel}</span>
          </div>
          <button type="button" className="icon-button" onClick={onClose}>
            {t(lang, "close")}
          </button>
        </header>
        <div className="terminal-surface" ref={containerRef} />
      </section>
    </div>
  );
}
