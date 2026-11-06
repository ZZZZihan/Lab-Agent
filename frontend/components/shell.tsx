"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { Button, buttonVariants } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { cn } from "cn";
import { ApiError, api, changePassword, currentUser, logout, type User } from "@/lib/api";
import { useI18n } from "@/lib/i18n";

export function Shell({ children }: { children: React.ReactNode }) {
  const { t, toggle } = useI18n();
  const pathname = usePathname();
  const router = useRouter();
  const [user, setUser] = useState<User | null>(null);
  const [ready, setReady] = useState(false);
  const [calibrated, setCalibrated] = useState<boolean | null>(null);

  const [pwOpen, setPwOpen] = useState(false);
  const [oldPw, setOldPw] = useState("");
  const [newPw, setNewPw] = useState("");
  const [bindEmail, setBindEmail] = useState("");
  const [pwError, setPwError] = useState("");
  const [pwSaving, setPwSaving] = useState(false);

  useEffect(() => {
    currentUser()
      .then((found) => {
        if (!found) {
          router.replace("/login");
          return;
        }
        setUser(found);
        if (found.must_change_password) {
          setPwOpen(true);
          if (pathname !== "/change-password") {
            router.replace("/change-password");
          }
        }
        setReady(true);
        api<{ calibrated?: boolean }>("health")
          .then((health) => {
            setCalibrated(health.calibrated ?? null);
          })
          .catch(() => {
            setCalibrated(null);
          });
      })
      .catch(() => router.replace("/login"));
  }, [router, pathname]);

  const links = [
    { href: "/chat", label: t.chat },
    { href: "/documents", label: t.documents },
    { href: "/lab", label: t.labAssets },
    { href: "/settings", label: t.settings },
    ...(user?.role === "admin"
      ? [
          { href: "/admin/imports", label: t.importsNav },
          { href: "/admin/users", label: t.users },
          { href: "/admin/groups", label: t.groups },
          { href: "/audit", label: t.audit },
        ]
      : []),
  ];

  async function handleChangePassword(e: React.FormEvent) {
    e.preventDefault();
    setPwError("");
    setPwSaving(true);
    try {
      const updated = await changePassword(oldPw, newPw, bindEmail);
      setUser(updated);
      setPwOpen(false);
      setOldPw("");
      setNewPw("");
      setBindEmail("");
    } catch (err) {
      setPwError(err instanceof ApiError ? err.message : t.error);
    } finally {
      setPwSaving(false);
    }
  }

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
      {user?.must_change_password ? (
        <div
          role="alert"
          className="border-b border-destructive/30 bg-destructive/10 px-4 py-2 text-sm text-destructive font-medium"
        >
          <div className="mx-auto flex w-full max-w-5xl flex-wrap items-center justify-between gap-2">
            <span>
              {t.mustChangePasswordTitle}
              {user.must_change_password_by
                ? `（截止：${user.must_change_password_by.slice(0, 10)}）`
                : ""}
            </span>
            <Button
              size="sm"
              variant="outline"
              onClick={() => setPwOpen(true)}
            >
              {t.saveNewPassword}
            </Button>
          </div>
        </div>
      ) : null}
      {calibrated === false ? (
        <div
          role="alert"
          className="border-b border-amber-500/40 bg-amber-500/10 px-4 py-2.5 text-sm text-amber-800 dark:text-amber-300"
        >
          <div className="mx-auto w-full max-w-5xl">{t.uncalibratedBanner}</div>
        </div>
      ) : null}
      <main className="mx-auto w-full max-w-5xl px-4 py-6">
        {ready ? children : <p className="text-sm text-muted-foreground">{t.loading}</p>}
      </main>
      <Dialog open={pwOpen} onOpenChange={setPwOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t.mustChangePasswordTitle}</DialogTitle>
            <DialogDescription>{t.mustChangePasswordDesc}</DialogDescription>
          </DialogHeader>
          <form onSubmit={handleChangePassword} className="grid gap-3">
            <div>
              <Label htmlFor="old-pw">{t.oldPassword}</Label>
              <Input
                id="old-pw"
                type="password"
                value={oldPw}
                onChange={(e) => setOldPw(e.target.value)}
                required
              />
            </div>
            <div>
              <Label htmlFor="new-pw">{t.newPassword}</Label>
              <Input
                id="new-pw"
                type="password"
                value={newPw}
                onChange={(e) => setNewPw(e.target.value)}
                required
              />
            </div>
            {!user?.email ? (
              <div>
                <Label htmlFor="bind-email">{t.bindEmailOptional}</Label>
                <Input
                  id="bind-email"
                  type="email"
                  placeholder="请输入常用邮箱（选填）"
                  value={bindEmail}
                  onChange={(e) => setBindEmail(e.target.value)}
                />
              </div>
            ) : null}
            {pwError ? <p className="text-sm text-destructive">{pwError}</p> : null}
            <DialogFooter>
              <Button type="submit" disabled={pwSaving}>
                {pwSaving ? t.saving : t.saveNewPassword}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </div>
  );
}
