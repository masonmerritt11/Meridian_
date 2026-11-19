import "./globals.css";
import type { ReactNode } from "react";
import type { Metadata, Viewport } from "next";
import RegisterSW from "@/components/RegisterSW";

export const metadata: Metadata = {
  title: "MERIDIAN Command Center",
  description: "Advisory-only research platform",
  manifest: "/manifest.webmanifest",
  applicationName: "MERIDIAN",
  appleWebApp: { capable: true, statusBarStyle: "black-translucent", title: "MERIDIAN" },
  formatDetection: { telephone: false },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
  themeColor: "#0b0e13",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>{children}<RegisterSW /></body>
    </html>
  );
}
