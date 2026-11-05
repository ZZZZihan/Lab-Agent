"use client";

import { useCallback, useEffect, useState } from "react";

import { Shell } from "@/components/shell";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
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
import { ApiError, api, currentUser, type User } from "@/lib/api";
import { useI18n } from "@/lib/i18n";

type AdminUser = {
  id: string;
  email: string;
  display_name: string;
  role: "admin" | "member" | "viewer" | string;
  is_active: boolean;
  created_at?: string | null;
};

export default function AdminUsersPage() {
  const { t } = useI18n();
  const [me, setMe] = useState<User | null>(null);
  const [users, setUsers] = useState<AdminUser[] | null>(null);
  const [error, setError] = useState("");

  const [createOpen, setCreateOpen] = useState(false);
  const [createEmail, setCreateEmail] = useState("");
  const [createName, setCreateName] = useState("");
  const [createRole, setCreateRole] = useState<"admin" | "member" | "viewer">("member");
  const [createSecret, setCreateSecret] = useState("");
  const [createError, setCreateError] = useState("");
  const [creating, setCreating] = useState(false);

  const [resetTarget, setResetTarget] = useState<AdminUser | null>(null);
  const [resetSecret, setResetSecret] = useState("");
  const [resetError, setResetError] = useState("");
  const [resetting, setResetting] = useState(false);

  const load = useCallback(async () => {
    setError("");
    try {
      const [actor, listed] = await Promise.all([
        currentUser(),
        api<{ users: AdminUser[] }>("admin/users"),
      ]);
      setMe(actor);
      setUsers(listed.users);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t.error);
      setUsers([]);
    }
  }, [t.error]);

  useEffect(() => {
    let cancelled = false;
    Promise.all([currentUser(), api<{ users: AdminUser[] }>("admin/users")])
      .then(([actor, listed]) => {
        if (cancelled) return;
        setMe(actor);
        setUsers(listed.users);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(err instanceof ApiError ? err.message : "这次请求没有完成。");
        setUsers([]);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    setCreateError("");
    setCreating(true);
    try {
      await api("admin/users", {
        method: "POST",
        body: JSON.stringify({
          email: createEmail,
          display_name: createName,
          role: createRole,
          password: createSecret,
        }),
      });
      setCreateOpen(false);
      setCreateEmail("");
      setCreateName("");
      setCreateRole("member");
      setCreateSecret("");
      await load();
    } catch (err) {
      setCreateError(err instanceof ApiError ? err.message : t.error);
    } finally {
      setCreating(false);
    }
  }

  async function handleRoleChange(target: AdminUser, newRole: string) {
    setError("");
    try {
      await api(`admin/users/${target.id}`, {
        method: "PATCH",
        body: JSON.stringify({ role: newRole }),
      });
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t.error);
    }
  }

  async function handleToggleActive(target: AdminUser) {
    setError("");
    try {
      await api(`admin/users/${target.id}`, {
        method: "PATCH",
        body: JSON.stringify({ is_active: !target.is_active }),
      });
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t.error);
    }
  }

  async function handleResetSecret(e: React.FormEvent) {
    e.preventDefault();
    if (!resetTarget) return;
    setResetError("");
    setResetting(true);
    try {
      await api(`admin/users/${resetTarget.id}/password`, {
        method: "POST",
        body: JSON.stringify({ password: resetSecret }),
      });
      setResetTarget(null);
      setResetSecret("");
    } catch (err) {
      setResetError(err instanceof ApiError ? err.message : t.error);
    } finally {
      setResetting(false);
    }
  }

  return (
    <Shell>
      <div className="space-y-6">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div>
            <h1 className="font-display text-2xl">{t.usersTitle}</h1>
            <p className="mt-1 text-sm text-muted-foreground">{t.usersHint}</p>
          </div>
          {me?.role === "admin" ? (
            <Button
              onClick={() => {
                setCreateError("");
                setCreateOpen(true);
              }}
            >
              {t.createUser}
            </Button>
          ) : null}
        </div>

        {error ? (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        ) : null}

        {users === null ? (
          <p className="text-sm text-muted-foreground">{t.loading}</p>
        ) : (
          <div className="overflow-x-auto rounded-md border border-border bg-card">
            <table className="w-full text-left text-sm">
              <thead className="border-b border-border bg-muted/40 text-xs text-muted-foreground">
                <tr>
                  <th className="px-4 py-3 font-medium">{t.displayName}</th>
                  <th className="px-4 py-3 font-medium">{t.email}</th>
                  <th className="px-4 py-3 font-medium">{t.role}</th>
                  <th className="px-4 py-3 font-medium">{t.userStatus}</th>
                  <th className="px-4 py-3 font-medium">{t.action}</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {users.map((u) => {
                  const isSelf = me?.id === u.id;
                  return (
                    <tr key={u.id}>
                      <td className="px-4 py-3 font-medium">{u.display_name}</td>
                      <td className="px-4 py-3 font-mono text-xs">{u.email}</td>
                      <td className="px-4 py-3">
                        <select
                          aria-label={`${u.display_name} ${t.role}`}
                          value={u.role}
                          disabled={isSelf}
                          onChange={(e) => handleRoleChange(u, e.target.value)}
                          className="rounded-md border border-input bg-background px-2.5 py-1 text-xs disabled:opacity-50"
                        >
                          <option value="admin">{t.roleAdmin}</option>
                          <option value="member">{t.roleMember}</option>
                          <option value="viewer">{t.roleViewer}</option>
                        </select>
                      </td>
                      <td className="px-4 py-3">
                        <Badge variant={u.is_active ? "secondary" : "outline"}>
                          {u.is_active ? t.userActive : t.userInactive}
                        </Badge>
                      </td>
                      <td className="px-4 py-3">
                        <div className="flex flex-wrap gap-2">
                          <Button
                            variant="outline"
                            size="sm"
                            disabled={isSelf}
                            onClick={() => handleToggleActive(u)}
                          >
                            {u.is_active ? t.deactivateUser : t.activateUser}
                          </Button>
                          <Button
                            variant="outline"
                            size="sm"
                            onClick={() => {
                              setResetTarget(u);
                              setResetSecret("");
                              setResetError("");
                            }}
                          >
                            {t.resetPassword}
                          </Button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}

        <Dialog open={createOpen} onOpenChange={setCreateOpen}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>{t.createUser}</DialogTitle>
              <DialogDescription>{t.usersHint}</DialogDescription>
            </DialogHeader>
            <form onSubmit={handleCreate} className="space-y-4">
              <div className="space-y-1.5">
                <Label htmlFor="create-email">{t.email}</Label>
                <Input
                  id="create-email"
                  type="email"
                  required
                  value={createEmail}
                  onChange={(e) => setCreateEmail(e.target.value)}
                />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="create-name">{t.displayName}</Label>
                <Input
                  id="create-name"
                  required
                  value={createName}
                  onChange={(e) => setCreateName(e.target.value)}
                />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="create-role">{t.role}</Label>
                <select
                  id="create-role"
                  value={createRole}
                  onChange={(e) =>
                    setCreateRole(e.target.value as "admin" | "member" | "viewer")
                  }
                  className="flex h-9 w-full rounded-md border border-input bg-background px-3 py-1 text-sm"
                >
                  <option value="admin">{t.roleAdmin}</option>
                  <option value="member">{t.roleMember}</option>
                  <option value="viewer">{t.roleViewer}</option>
                </select>
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="create-secret">{t.password}</Label>
                <Input
                  id="create-secret"
                  type="password"
                  required
                  value={createSecret}
                  onChange={(e) => setCreateSecret(e.target.value)}
                />
              </div>
              {createError ? (
                <p role="alert" className="text-sm text-destructive">
                  {createError}
                </p>
              ) : null}
              <DialogFooter>
                <Button
                  type="button"
                  variant="ghost"
                  onClick={() => setCreateOpen(false)}
                >
                  {t.cancel}
                </Button>
                <Button type="submit" disabled={creating}>
                  {creating ? t.saving : t.createUser}
                </Button>
              </DialogFooter>
            </form>
          </DialogContent>
        </Dialog>

        <Dialog
          open={resetTarget !== null}
          onOpenChange={(openState) => {
            if (!openState) setResetTarget(null);
          }}
        >
          <DialogContent>
            <DialogHeader>
              <DialogTitle>
                {t.resetPassword}
                {resetTarget ? ` — ${resetTarget.display_name}` : ""}
              </DialogTitle>
              <DialogDescription>{t.usersHint}</DialogDescription>
            </DialogHeader>
            <form onSubmit={handleResetSecret} className="space-y-4">
              <div className="space-y-1.5">
                <Label htmlFor="reset-secret">{t.newPassword}</Label>
                <Input
                  id="reset-secret"
                  type="password"
                  required
                  value={resetSecret}
                  onChange={(e) => setResetSecret(e.target.value)}
                />
              </div>
              {resetError ? (
                <p role="alert" className="text-sm text-destructive">
                  {resetError}
                </p>
              ) : null}
              <DialogFooter>
                <Button
                  type="button"
                  variant="ghost"
                  onClick={() => setResetTarget(null)}
                >
                  {t.cancel}
                </Button>
                <Button type="submit" disabled={resetting}>
                  {resetting ? t.saving : t.resetPassword}
                </Button>
              </DialogFooter>
            </form>
          </DialogContent>
        </Dialog>
      </div>
    </Shell>
  );
}
