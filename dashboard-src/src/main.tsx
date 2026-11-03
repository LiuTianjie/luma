import React from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { RouterProvider, useRouter } from "./router";
import { legacyRedirect } from "./routes";
import { SharedDashboardPage } from "./components/SharedDashboardPage";
import "./styles.css";

function DashboardEntry() {
  const { path } = useRouter();
  const shared = /^\/share\/([a-fA-F0-9]{32})\/?$/.exec(path);
  return shared ? <SharedDashboardPage accessToken={shared[1]} /> : <App />;
}

createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <RouterProvider redirect={legacyRedirect}>
      <DashboardEntry />
    </RouterProvider>
  </React.StrictMode>,
);
