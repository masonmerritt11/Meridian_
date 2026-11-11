import "./globals.css";
import type { ReactNode } from "react";

export const metadata = { title: "MERIDIAN Command Center", description: "Advisory-only research platform" };

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
