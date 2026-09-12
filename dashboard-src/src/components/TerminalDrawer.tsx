import { useEffect, useId, useRef, useState } from "react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Separator } from "@/components/ui/separator";
import { Spinner } from "@/components/ui/spinner";
import { FitAddon } from "@xterm/addon-fit";
import { Terminal } from "@xterm/xterm";
import { AlertCircle, ArrowLeft, Check, X } from "lucide-react";
import "@xterm/xterm/css/xterm.css";
import type { DashboardNode, DashboardService, Lang } from "../types";
import { t } from "../i18n";
import "./terminalSession.css";

type TerminalStatus = "connecting" | "connected" | "ended" | "error";

// xterm needs RGB colors. The browser resolves the dashboard's OKLCH tokens.
function terminalTheme() {
  const styles = getComputedStyle(document.documentElement);
  const context = document.createElement("canvas").getContext("2d", { willReadFrequently: true });
  const color = (token: string) => {
    const value = styles.getPropertyValue(token).trim();
    if (!context || !value) return undefined;
    context.clearRect(0, 0, 1, 1);
    context.fillStyle = value;
    context.fillRect(0, 0, 1, 1);
    const [red, green, blue, alpha] = context.getImageData(0, 0, 1, 1).data;
    return `rgba(${red}, ${green}, ${blue}, ${alpha / 255})`;
  };
  return {
    background: color("--background"),
    foreground: color("--foreground"),
    cursor: color("--primary"),
    selectionBackground: color("--accent"),
  };
}

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
  return (
    <Dialog open onOpenChange={(open) => { if (!open) props.onClose(); }}>
      <DialogContent
        className="flex h-[min(84dvh,920px)] min-h-0 flex-col overflow-hidden sm:max-w-[min(1440px,calc(100%-2rem))]"
        showCloseButton={false}
        initialFocus={false}
      >
        <TerminalContent {...props} />
      </DialogContent>
    </Dialog>
  );
}

function TerminalContent({ lang, target, token, onClose, inline = false }: TerminalDrawerProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const socketRef = useRef<WebSocket | null>(null);
  const terminalRef = useRef<Terminal | null>(null);
  const fitRef = useRef<FitAddon | null>(null);
  const sessionRef = useRef("");
  const reportedCloseRef = useRef(false);
  const [status, setStatus] = useState<TerminalStatus>("connecting");
  const [errorMessage, setErrorMessage] = useState("");
  const titleId = useId();
  const isContainer = target.kind === "container";
  const title = isContainer
    ? `${target.stack || target.service?.stack || "-"} / ${target.service?.name || target.service?.fullName || "-"}`
    : (target.node?.name || "-");
  const meta = isContainer
    ? [target.node?.name, target.node?.region].filter(Boolean).join(" · ")
    : `${target.node?.region || "-"} · ${target.node?.agentOs || "agent"}`;

  useEffect(() => {
    let active = true;
    const container = containerRef.current;
    if (!container) return;
    setStatus("connecting");
    setErrorMessage("");
    reportedCloseRef.current = false;
    const term = new Terminal({
      cursorBlink: true,
      convertEol: true,
      fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace',
      fontSize: 13,
      theme: terminalTheme(),
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(container);
    fit.fit();
    // Dialog owns focus containment; the terminal caret is the initial target.
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
    const themeObserver = new MutationObserver(() => { term.options.theme = terminalTheme(); });
    themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ["class", "data-theme", "style"] });

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
        setErrorMessage(String(message.message || "Terminal error"));
        term.writeln("");
        term.writeln(String(message.message || "Terminal error"));
      }
    });
    socket.addEventListener("close", (event) => {
      if (!active) return;
      if (!sessionRef.current && !reportedCloseRef.current) {
        setStatus("error");
        setErrorMessage(lang === "zh" ? `终端连接已关闭（${event.code || "-"}）。` : `Terminal connection closed (${event.code || "-"}).`);
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
      setErrorMessage(lang === "zh" ? "终端连接失败，请稍后重试。" : "The terminal could not connect. Try again shortly.");
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
      themeObserver.disconnect();
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

  const Header = inline ? CardHeader : DialogHeader;
  const Title = inline ? CardTitle : DialogTitle;
  const Description = inline ? CardDescription : DialogDescription;
  const header = (
    <Header className="flex shrink-0 flex-row flex-wrap items-center justify-between gap-3">
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <Title id={titleId} className="truncate" title={title}>{title}</Title>
        <Description className="truncate" title={meta}>
          {isContainer ? (lang === "zh" ? "容器终端" : "Container terminal") : (lang === "zh" ? "节点终端" : "Node terminal")} · {meta}
        </Description>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant={status === "error" ? "destructive" : status === "connected" ? "success" : "secondary"} role="status" aria-live="polite">
          {status === "connecting" && <Spinner aria-hidden="true" data-icon="inline-start" />}
          {status === "connected" && <Check data-icon="inline-start" />}
          {statusLabel}
        </Badge>
        <Button type="button" variant="outline" size="sm" onClick={onClose}>
          {inline ? <ArrowLeft data-icon="inline-start" /> : <X data-icon="inline-start" />}
          {inline ? (lang === "zh" ? "结束并返回" : "End session and return") : t(lang, "close")}
        </Button>
      </div>
    </Header>
  );
  const surface = (
    <>
      {errorMessage && <Alert variant="destructive">
        <AlertCircle />
        <AlertTitle>{lang === "zh" ? "终端连接异常" : "Terminal connection error"}</AlertTitle>
        <AlertDescription>{errorMessage}</AlertDescription>
      </Alert>}
      <div className="terminal-session__surface" ref={containerRef} role="region" aria-label={lang === "zh" ? "交互式终端" : "Interactive terminal"} />
    </>
  );
  return inline ? (
    <Card className="h-full min-h-0 min-w-0" role="region" aria-labelledby={titleId}>
      {header}
      <Separator />
      <CardContent className="flex min-h-0 flex-1 flex-col gap-3">{surface}</CardContent>
    </Card>
  ) : (
    <>
      {header}
      <Separator />
      {surface}
    </>
  );
}
