"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

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
  email: string | null;
  login_name?: string | null;
  display_name: string;
  role: string;
  is_active: boolean;
};

type GroupMemberInfo = {
  id: string;
  email: string | null;
  login_name?: string | null;
  display_name: string;
  role: string;
};

type AdminGroup = {
  id: string;
  slug: string;
  name: string;
  created_at?: string | null;
  member_ids: string[];
  members: GroupMemberInfo[];
};

export default function AdminGroupsPage() {
  const { t } = useI18n();
  const [me, setMe] = useState<User | null>(null);
  const [groups, setGroups] = useState<AdminGroup[] | null>(null);
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [error, setError] = useState("");
  const [removingKey, setRemovingKey] = useState<string | null>(null);

  const [createOpen, setCreateOpen] = useState(false);
  const [createSlug, setCreateSlug] = useState("");
  const [createName, setCreateName] = useState("");
  const [createError, setCreateError] = useState("");
  const [creating, setCreating] = useState(false);

  const [editTarget, setEditTarget] = useState<AdminGroup | null>(null);
  const [selectedUserIds, setSelectedUserIds] = useState<string[]>([]);
  const [memberSearch, setMemberSearch] = useState("");
  const [editError, setEditError] = useState("");
  const [savingMembers, setSavingMembers] = useState(false);

  const load = useCallback(async () => {
    setError("");
    try {
      const [actor, listedGroups, listedUsers] = await Promise.all([
        currentUser(),
        api<{ groups: AdminGroup[] }>("admin/groups"),
        api<{ users: AdminUser[] }>("admin/users"),
      ]);
      setMe(actor);
      setGroups(listedGroups.groups);
      setUsers(listedUsers.users);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t.error);
      setGroups([]);
    }
  }, [t.error]);

  useEffect(() => {
    let cancelled = false;
    Promise.all([
      currentUser(),
      api<{ groups: AdminGroup[] }>("admin/groups"),
      api<{ users: AdminUser[] }>("admin/users"),
    ])
      .then(([actor, listedGroups, listedUsers]) => {
        if (cancelled) return;
        setMe(actor);
        setGroups(listedGroups.groups);
        setUsers(listedUsers.users);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(err instanceof ApiError ? err.message : "这次请求没有完成。");
        setGroups([]);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const filteredModalUsers = useMemo(() => {
    const q = memberSearch.trim().toLowerCase();
    if (!q) return users;
    return users.filter(
      (u) =>
        u.display_name.toLowerCase().includes(q) ||
        (u.email || "").toLowerCase().includes(q) ||
        (u.login_name || "").toLowerCase().includes(q),
    );
  }, [users, memberSearch]);

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    if (creating) return;
    setCreateError("");
    setCreating(true);
    try {
      await api("admin/groups", {
        method: "POST",
        body: JSON.stringify({
          slug: createSlug.trim().toLowerCase(),
          name: createName.trim(),
        }),
      });
      setCreateOpen(false);
      setCreateSlug("");
      setCreateName("");
      await load();
    } catch (err) {
      setCreateError(err instanceof ApiError ? err.message : t.error);
    } finally {
      setCreating(false);
    }
  }

  async function handleSaveMembers(e: React.FormEvent) {
    e.preventDefault();
    if (!editTarget || savingMembers) return;
    setEditError("");
    setSavingMembers(true);
    try {
      await api(`admin/groups/${editTarget.id}/members`, {
        method: "PUT",
        body: JSON.stringify({ user_ids: selectedUserIds }),
      });
      setEditTarget(null);
      setMemberSearch("");
      await load();
    } catch (err) {
      setEditError(err instanceof ApiError ? err.message : t.error);
    } finally {
      setSavingMembers(false);
    }
  }

  async function handleQuickRemoveMember(group: AdminGroup, memberId: string) {
    if (removingKey !== null) return;
    const opKey = `${group.id}:${memberId}`;
    setError("");
    setRemovingKey(opKey);
    try {
      const nextIds = group.member_ids.filter((id) => id !== memberId);
      await api(`admin/groups/${group.id}/members`, {
        method: "PUT",
        body: JSON.stringify({ user_ids: nextIds }),
      });
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t.error);
    } finally {
      setRemovingKey(null);
    }
  }

  return (
    <Shell>
      <div className="space-y-6">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div>
            <h1 className="font-display text-2xl">{t.groupsTitle}</h1>
            <p className="mt-1 text-sm text-muted-foreground">{t.groupsHint}</p>
          </div>
          {me?.role === "admin" ? (
            <Button
              onClick={() => {
                setCreateError("");
                setCreateOpen(true);
              }}
            >
              {t.createGroup}
            </Button>
          ) : null}
        </div>

        {error ? (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        ) : null}

        {groups === null ? (
          <p className="text-sm text-muted-foreground">{t.loading}</p>
        ) : (
          <div className="overflow-x-auto rounded-md border border-border bg-card">
            <table className="w-full text-left text-sm">
              <thead className="border-b border-border bg-muted/40 text-xs text-muted-foreground">
                <tr>
                  <th className="px-4 py-3 font-medium">{t.groupName}</th>
                  <th className="px-4 py-3 font-medium">{t.groupSlug}</th>
                  <th className="px-4 py-3 font-medium">{t.groupMembers}</th>
                  <th className="px-4 py-3 font-medium">{t.action}</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {groups.map((group) => (
                  <tr key={group.id}>
                    <td className="px-4 py-3 font-medium">{group.name}</td>
                    <td className="px-4 py-3 font-mono text-xs">{group.slug}</td>
                    <td className="px-4 py-3">
                      <div className="flex flex-wrap gap-1.5">
                        {group.members.length === 0 ? (
                          <span className="text-xs text-muted-foreground">—</span>
                        ) : (
                          group.members.map((m) => {
                            const isRemoving =
                              removingKey === `${group.id}:${m.id}`;
                            return (
                              <Badge
                                key={m.id}
                                variant="secondary"
                                className="inline-flex items-center gap-1"
                              >
                                <span>{m.display_name}</span>
                                {me?.role === "admin" ? (
                                  <button
                                    type="button"
                                    disabled={removingKey !== null}
                                    aria-label={`${t.removeMember} ${m.display_name}`}
                                    title={`${t.removeMember} ${m.display_name}`}
                                    onClick={() =>
                                      void handleQuickRemoveMember(group, m.id)
                                    }
                                    className="ml-0.5 cursor-pointer rounded px-0.5 text-xs text-muted-foreground hover:text-destructive disabled:opacity-40"
                                  >
                                    {isRemoving ? "…" : "×"}
                                  </button>
                                ) : null}
                              </Badge>
                            );
                          })
                        )}
                      </div>
                    </td>
                    <td className="px-4 py-3">
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => {
                          setEditTarget(group);
                          setSelectedUserIds(group.member_ids);
                          setMemberSearch("");
                          setEditError("");
                        }}
                      >
                        {t.editMembers}
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        <Dialog open={createOpen} onOpenChange={setCreateOpen}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>{t.createGroup}</DialogTitle>
              <DialogDescription>{t.groupsHint}</DialogDescription>
            </DialogHeader>
            <form onSubmit={handleCreate} className="space-y-4">
              <div className="space-y-1.5">
                <Label htmlFor="group-slug">{t.groupSlug}</Label>
                <Input
                  id="group-slug"
                  required
                  placeholder="helios"
                  value={createSlug}
                  onChange={(e) => setCreateSlug(e.target.value)}
                />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="group-name">{t.groupName}</Label>
                <Input
                  id="group-name"
                  required
                  value={createName}
                  onChange={(e) => setCreateName(e.target.value)}
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
                  {creating ? t.saving : t.createGroup}
                </Button>
              </DialogFooter>
            </form>
          </DialogContent>
        </Dialog>

        <Dialog
          open={editTarget !== null}
          onOpenChange={(openState) => {
            if (!openState) {
              setEditTarget(null);
              setMemberSearch("");
            }
          }}
        >
          <DialogContent>
            <DialogHeader>
              <DialogTitle>
                {t.editMembers}
                {editTarget ? ` — ${editTarget.name} (${editTarget.slug})` : ""}
              </DialogTitle>
              <DialogDescription>{t.groupsHint}</DialogDescription>
            </DialogHeader>
            <form onSubmit={handleSaveMembers} className="space-y-4">
              <Input
                type="search"
                placeholder={t.searchMembersPlaceholder}
                value={memberSearch}
                onChange={(e) => setMemberSearch(e.target.value)}
                className="h-8 text-xs"
              />
              <div className="max-h-64 space-y-2 overflow-y-auto rounded-md border border-border p-3">
                {filteredModalUsers.map((u) => {
                  const checked = selectedUserIds.includes(u.id);
                  const identifier = u.email || u.login_name || "—";
                  return (
                    <label
                      key={u.id}
                      className="flex cursor-pointer items-center justify-between gap-2 rounded px-2 py-1.5 text-sm hover:bg-muted/50"
                    >
                      <span className="flex flex-wrap items-center gap-2">
                        <input
                          type="checkbox"
                          checked={checked}
                          onChange={(e) => {
                            if (e.target.checked) {
                              setSelectedUserIds((prev) =>
                                prev.includes(u.id) ? prev : [...prev, u.id],
                              );
                            } else {
                              setSelectedUserIds((prev) =>
                                prev.filter((item) => item !== u.id),
                              );
                            }
                          }}
                        />
                        <span className="font-medium">{u.display_name}</span>
                        <span className="font-mono text-xs text-muted-foreground">
                          {identifier}
                        </span>
                        {!u.is_active ? (
                          <Badge variant="outline" className="text-[10px]">
                            {t.userInactive}
                          </Badge>
                        ) : null}
                      </span>
                      <Badge variant="outline">{u.role}</Badge>
                    </label>
                  );
                })}
              </div>
              {editError ? (
                <p role="alert" className="text-sm text-destructive">
                  {editError}
                </p>
              ) : null}
              <DialogFooter>
                <Button
                  type="button"
                  variant="ghost"
                  onClick={() => {
                    setEditTarget(null);
                    setMemberSearch("");
                  }}
                >
                  {t.cancel}
                </Button>
                <Button type="submit" disabled={savingMembers}>
                  {savingMembers ? t.saving : t.save}
                </Button>
              </DialogFooter>
            </form>
          </DialogContent>
        </Dialog>
      </div>
    </Shell>
  );
}
