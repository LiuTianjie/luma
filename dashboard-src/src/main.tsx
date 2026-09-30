import React from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { RouterProvider } from "./router";
import { legacyRedirect } from "./routes";
import "./styles.css";

createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <RouterProvider redirect={legacyRedirect}>
      <App />
    </RouterProvider>
  </React.StrictMode>,
);
