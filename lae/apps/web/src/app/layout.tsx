import "@fontsource-variable/manrope";
import "@fontsource-variable/newsreader";
import "./globals.css";

import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";

export const metadata: Metadata = {
  title: "Luma Application Engine",
  description: "Diagnose, compose and deploy applications on Luma.",
};

export const viewport: Viewport = {
  colorScheme: "dark",
  themeColor: "#081311",
};

export default function RootLayout({ children }: Readonly<{ children: ReactNode }>) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
