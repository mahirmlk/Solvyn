import "./globals.css";
import { Sidebar } from "@/components/Sidebar";
import { AsciiAnimation } from "@/components/AsciiAnimation";

export const metadata = {
  title: "Solvyn | Engineering Operations",
  description: "Editorial operations console for autonomous engineering agents",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body suppressHydrationWarning>
        <div className="app-shell">
          <AsciiAnimation />
          <div className="glass-grid" aria-hidden="true">
            {Array.from({ length: 24 }).map((_, i) => (
              <div className="glass-block" key={i} />
            ))}
          </div>
          <div className="scanlines" aria-hidden="true" />
          <div className="vignette" aria-hidden="true" />
          <Sidebar />
          <main className="app-content">{children}</main>
        </div>
      </body>
    </html>
  );
}
