import type { Metadata } from "next";

import "./globals.css";

export const metadata: Metadata = {
  title: "Nexus",
  description: "Enterprise knowledge intelligence platform",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
