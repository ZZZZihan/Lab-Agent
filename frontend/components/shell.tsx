"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { Button, buttonVariants } from "@/components/ui/button";
import { cn } from "cn";
import { currentUser, logout, type User } from "@/lib/api";
import { useI18n } from "@/lib/i18n";

export function Shell({ children }: { children: React.ReactNode }) {
  const { t, toggle } = useI18n();
  const pathname = usePathname();
  const router = useRouter();
  const [user, setUser] = useState<User | null>(null);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    currentUser()
      .then((found) => {
        if (!found) {
          router.replace("/login");
          return;
        }
        setUser(found);
        setReady(true);
      })
      .catch(() => router.replace("/login"));
  }, [router]);

  const links = [
    { href: "/chat", label: t.chat },
    { href: "/documents", label: t.documents },
    ...(user?.role === "admin" ? [{ href: "/audit", label: t.audit }] : []),
  ];

  return (
    <div className="min-h-screen">
      <header className="border-b border-border bg-card/80">
        <div className="mx-auto flex w-full max-w-5xl flex-wrap items-center gap-3 px-4 py-3">
          <Link href="/chat" className="mr-auto flex items-baseline gap-2">
            <span className="font-display text-xl tracking-wide">{t.brand}</span>
            <span className="text-xs text-muted-foreground">{t.lab}</span>
          </Link>
          <nav className="flex flex-wrap gap-1">
            {links.map((link) => (
              <Link
                key={link.href}
                href={link.href}
                className={cn(
                  buttonVariants({
                    variant: pathname.startsWith(link.href) ? "default" : "ghost",
                    size: "sm",
                  }),
                )}
              >
                {link.label}
              </Link>
            ))}
          </nav>
          <Button variant="outline" size="sm" onClick={toggle}>
            {t.language}
          </Button>
          {user ? (
            <div className="flex items-center gap-2 text-sm">
              <span>
                {user.display_name}
                <span className="ml-1 text-muted-foreground">{user.role}</span>
              </span>
              <Button
                variant="ghost"
                size="sm"
                onClick={async () => {
                  await logout();
                  router.replace("/login");
                }}
              >
                {t.logout}
              </Button>
            </div>
          ) : null}
        </div>
      </header>
      <main className="mx-auto w-full max-w-5xl px-4 py-6">
        {ready ? children : <p className="text-sm text-muted-foreground">{t.loading}</p>}
      </main>
    </div>
  );
}
