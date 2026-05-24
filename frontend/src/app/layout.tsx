import "./globals.css";
import { Sidebar } from "@/components/Sidebar";

export const metadata = {
  title: "Solvyn | Engineering Operations",
  description: "Editorial operations console for autonomous engineering agents",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body suppressHydrationWarning>
        <div className="app-stage">
          <div className="app-frame">
            <Sidebar />
            <main className="app-content">{children}</main>
          </div>
        </div>
      </body>
    </html>
  );
}
