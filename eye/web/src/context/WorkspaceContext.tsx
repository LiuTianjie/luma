import { createContext, useContext } from "react";
import type { AuthStatus, PortfolioOverview, Project, RetentionStatus } from "@/api/types";
import type { ThemeMode } from "@/hooks/useTheme";

export interface WorkspaceContextValue {
  authStatus: AuthStatus | null;
  refreshAuthStatus: () => Promise<void>;
  projects: Project[];
  publicUrl: string;
  retention?: RetentionStatus;
  portfolio: PortfolioOverview | null;
  since: string;
  setSince: (next: string) => void;
  refreshTick: number;
  triggerRefresh: () => void;
  reloadProjects: () => Promise<Project[]>;
  openNewProjectModal: () => void;
  notify: (msg: string, tone?: "success" | "error") => void;
  themeMode: ThemeMode;
  setThemeMode: (mode: ThemeMode) => void;
  logout: () => Promise<void>;
}

export const WorkspaceContext = createContext<WorkspaceContextValue | null>(null);

export function useWorkspace(): WorkspaceContextValue {
  const ctx = useContext(WorkspaceContext);
  if (!ctx) {
    throw new Error("useWorkspace must be used within WorkspaceShell");
  }
  return ctx;
}
