"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";

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
import {
  ApiError,
  changePassword,
  currentUser,
  getSystemHealth,
  logout,
  resetClientAuthCache,
  type HealthResponse,
  type User,
} from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { validatePasswordClient } from "@/lib/security";

export type ShellContextValue = {
  user: User | null;
  calibrated: boolean | null;
  health: HealthResponse | null;
  refreshUser: () => Promise<User | null>;
};

const ShellContext = createContext<ShellContextValue>({
  user: null,
  calibrated: null,
  health: null,
  refreshUser: async () => null,
});

export function useShell(): ShellContextValue {
  return useContext(ShellContext);
}

export function Shell({ children }: { children: React.ReactNode }) {
  const { t, toggle } = useI18n();
  const pathname = usePathname();
  const router = useRouter();
  const [user, setUser] = useState<User | null>(null);
  const [ready, setReady] = useState(false);
  const [calibrated, setCalibrated] = useState<boolean | null>(null);
  const [health, setHealth] = useState<HealthResponse | null>(null);

  const [pwOpen, setPwOpen] = useState(false);
  const [oldPw, setOldPw] = useState("");
  const [newPw, setNewPw] = useState("");
  const [bindEmail, setBindEmail] = useState("");
  const [pwError, setPwError] = useState("");
  const [pwSaving, setPwSaving] = useState(false);

  const refreshUser = useCallback(async () => {
    const found = await currentUser({ force: true });
    setUser(found);
    return found;
  }, []);

  useEffect(() => {
    const handleLogoutEvent = () => {
      resetClientAuthCache();
    };
    window.addEventListener("lab-auth-logout", handleLogoutEvent);
    return () => {
      window.removeEventListener("lab-auth-logout", handleLogoutEvent);
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    currentUser()
      .then((found) => {
        if (cancelled) return;
        if (!found) {
          resetClientAuthCache();
          window.dispatchEvent(new Event("lab-auth-logout"));
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
        getSystemHealth()
          .then((h) => {
            if (cancelled) return;
            setHealth(h);
            setCalibrated(h.calibrated ?? null);
          })
          .catch(() => {
            if (cancelled) return;
            setCalibrated(null);
          });
      })
      .catch(() => {
        if (cancelled) return;
        resetClientAuthCache();
        window.dispatchEvent(new Event("lab-auth-logout"));
        router.replace("/login");
      });
    return () => {
      cancelled = true;
    };
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
    if (pwSaving) return;
    setPwError("");
    const pwErr = validatePasswordClient(newPw, {
      email: bindEmail.trim() || user?.email,
      oldPassword: oldPw,
    });
    if (pwErr) {
      setPwError(pwErr);
      return;
    }
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

  const shellCtx = useMemo<ShellContextValue>(
    () => ({ user, calibrated, health, refreshUser }),
    [user, calibrated, health, refreshUser],
  );

  const isChatCanvas = pathname.startsWith("/chat");
  const containerMaxW = isChatCanvas ? "max-w-7xl" : "max-w-5xl";

  return (
    <ShellContext.Provider value={shellCtx}>
      <div className="min-h-screen bg-[#FAF9F6] text-stone-900 dark:bg-[#181816] dark:text-stone-100">
      <header className="border-b border-stone-200/80 bg-[#FAF9F6]/90 backdrop-blur-xs dark:border-stone-800 dark:bg-[#181816]/90">
        <div
          className={`mx-auto flex w-full ${containerMaxW} flex-wrap items-center gap-3 px-4 py-3`}
        >
          <Link href="/chat" className="mr-auto flex items-baseline gap-2">
            <span className="text-sm text-primary dark:text-emerald-400">✦</span>
            <span className="font-display text-lg font-semibold tracking-wide">
              {t.lab} · 科研智能工作台
            </span>
            <span className="hidden text-xs text-muted-foreground sm:inline">
              {t.brand}
            </span>
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
              <span className="inline-flex items-center gap-1.5 rounded-full border border-stone-200/80 bg-white/80 px-2.5 py-0.5 text-xs font-medium dark:border-stone-800 dark:bg-stone-900">
                <span>👤 {user.display_name}</span>
                <span className="text-muted-foreground">· {user.role}</span>
              </span>
              <Button
                variant="ghost"
                size="sm"
                onClick={async () => {
                  window.dispatchEvent(new Event("lab-auth-logout"));
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
          <div
            className={`mx-auto flex w-full ${containerMaxW} flex-wrap items-center justify-between gap-2`}
          >
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
          <div className={`mx-auto w-full ${containerMaxW}`}>
            {t.uncalibratedBanner}
          </div>
        </div>
      ) : null}
      <main className={`mx-auto w-full ${containerMaxW} px-4 py-6`}>
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
    </ShellContext.Provider>
  );
}
