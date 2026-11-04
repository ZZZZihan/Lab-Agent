import type { Metadata } from "next";
import { cookies } from "next/headers";

import { LocaleProvider, type Locale } from "@/lib/i18n";
import "./globals.css";

export const metadata: Metadata = {
  title: "Lab Agent · 清衡实验室",
  description: "只根据有权阅读的实验室文档作答，并给出小节级引用。",
};

export default async function RootLayout({ children }: { children: React.ReactNode }) {
  const jar = await cookies();
  const initial: Locale = jar.get("la_locale")?.value === "en" ? "en" : "zh";
  return (
    <html lang={initial === "en" ? "en" : "zh-CN"} className="h-full antialiased">
      <body className="min-h-full">
        <LocaleProvider initial={initial}>{children}</LocaleProvider>
      </body>
    </html>
  );
}
