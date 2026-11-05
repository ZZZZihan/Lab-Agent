"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";

import { Shell } from "@/components/shell";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ApiError, api } from "@/lib/api";
import { useI18n } from "@/lib/i18n";

type DocumentACL = {
  visibility: string;
  user_emails: string[];
  group_slugs: string[];
  roles: string[];
};

type Detail = {
  id: string;
  title: string;
  collection: string;
  visibility: string;
  content: string;
  acl?: DocumentACL;
};

type GroupItem = {
  slug: string;
  name: string;
};

const ROLE_OPTIONS = ["admin", "member", "viewer"] as const;

export default function DocumentPage() {
  const { t } = useI18n();
  const params = useParams<{ id: string }>();
  const [doc, setDoc] = useState<Detail | null>(null);
  const [error, setError] = useState("");
  const [groups, setGroups] = useState<GroupItem[]>([]);

  const [visibility, setVisibility] = useState("lab");
  const [userEmails, setUserEmails] = useState("");
  const [groupSlugs, setGroupSlugs] = useState<string[]>([]);
  const [roles, setRoles] = useState<string[]>([]);
  const [savingAcl, setSavingAcl] = useState(false);
  const [aclError, setAclError] = useState("");
  const [aclSaved, setAclSaved] = useState(false);

  useEffect(() => {
    api<Detail>(`documents/${params.id}`)
      .then((detail) => {
        setDoc(detail);
        if (detail.acl) {
          setVisibility(detail.acl.visibility);
          setUserEmails(detail.acl.user_emails.join(", "));
          setGroupSlugs(detail.acl.group_slugs);
          setRoles(detail.acl.roles);
          api<{ groups: GroupItem[] }>("groups")
            .then((res) => setGroups(res.groups))
            .catch(() => {});
        }
      })
      .catch((err: unknown) => setError(err instanceof ApiError ? err.message : t.error));
  }, [params.id, t.error]);

  function toggleGroupSlug(slug: string) {
    setGroupSlugs((prev) =>
      prev.includes(slug) ? prev.filter((item) => item !== slug) : [...prev, slug],
    );
  }

  function toggleRole(role: string) {
    setRoles((prev) =>
      prev.includes(role) ? prev.filter((item) => item !== role) : [...prev, role],
    );
  }

  async function saveAcl(e: React.FormEvent) {
    e.preventDefault();
    if (!doc) return;
    setSavingAcl(true);
    setAclError("");
    setAclSaved(false);
    try {
      const parsedEmails = userEmails
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean);
      const updated = await api<{ id: string; visibility: string; acl: DocumentACL }>(
        `documents/${doc.id}/acl`,
        {
          method: "PUT",
          body: JSON.stringify({
            visibility,
            user_emails: parsedEmails,
            group_slugs: groupSlugs,
            roles,
          }),
        },
      );
      setDoc({
        ...doc,
        visibility: updated.visibility,
        acl: updated.acl,
      });
      setVisibility(updated.acl.visibility);
      setUserEmails(updated.acl.user_emails.join(", "));
      setGroupSlugs(updated.acl.group_slugs);
      setRoles(updated.acl.roles);
      setAclSaved(true);
    } catch (err) {
      setAclError(err instanceof ApiError ? err.message : t.error);
    } finally {
      setSavingAcl(false);
    }
  }

  return (
    <Shell>
      <Link href="/documents" className="text-sm text-primary underline">
        {t.back}
      </Link>
      {error ? <p className="mt-4 text-sm text-destructive">{error}</p> : null}
      {!doc && !error ? <p className="mt-4 text-sm text-muted-foreground">{t.loading}</p> : null}
      {doc ? (
        <article className="mt-4">
          <h1 className="font-display text-3xl">{doc.title}</h1>
          <div className="mt-3 flex gap-2">
            <Badge variant="outline">{doc.collection}</Badge>
            <Badge variant="secondary">{doc.visibility}</Badge>
          </div>

          {doc.acl ? (
            <form
              onSubmit={saveAcl}
              className="mt-6 space-y-4 rounded-xl bg-card p-5 ring-1 ring-foreground/10"
            >
              <div>
                <h2 className="font-medium">{t.permissions}</h2>
                <p className="mt-1 text-xs text-muted-foreground">{t.permissionsHint}</p>
              </div>
              <div className="grid gap-4 sm:grid-cols-2">
                <div>
                  <label className="text-xs text-muted-foreground">{t.visibility}</label>
                  <select
                    value={visibility}
                    onChange={(e) => setVisibility(e.target.value)}
                    className="mt-1 block h-8 w-full rounded-lg border border-input bg-transparent px-2.5 text-sm"
                  >
                    <option value="lab">{t.labVis}</option>
                    <option value="restricted">{t.restricted}</option>
                    <option value="private">{t.private}</option>
                  </select>
                </div>
                {visibility === "restricted" ? (
                  <div>
                    <label className="text-xs text-muted-foreground">{t.grants}</label>
                    <Input
                      value={userEmails}
                      onChange={(e) => setUserEmails(e.target.value)}
                      placeholder="lin.ke@lab.local, zhou.ning@lab.local"
                      className="mt-1"
                    />
                    <p className="mt-1 text-xs text-muted-foreground">{t.grantsHint}</p>
                  </div>
                ) : null}
              </div>

              {visibility === "restricted" ? (
                <div className="grid gap-4 sm:grid-cols-2">
                  <div>
                    <label className="text-xs text-muted-foreground">{t.grantGroups}</label>
                    <div className="mt-1 flex flex-wrap gap-2 rounded-lg border border-input p-2.5">
                      {groups.length === 0 ? (
                        <span className="text-xs text-muted-foreground">{t.noGroups}</span>
                      ) : (
                        groups.map((g) => (
                          <label key={g.slug} className="flex items-center gap-1.5 text-xs">
                            <input
                              type="checkbox"
                              checked={groupSlugs.includes(g.slug)}
                              onChange={() => toggleGroupSlug(g.slug)}
                            />
                            <span>
                              {g.name} ({g.slug})
                            </span>
                          </label>
                        ))
                      )}
                    </div>
                  </div>
                  <div>
                    <label className="text-xs text-muted-foreground">{t.grantRoles}</label>
                    <div className="mt-1 flex flex-wrap gap-3 rounded-lg border border-input p-2.5">
                      {ROLE_OPTIONS.map((roleOpt) => (
                        <label key={roleOpt} className="flex items-center gap-1.5 text-xs">
                          <input
                            type="checkbox"
                            checked={roles.includes(roleOpt)}
                            onChange={() => toggleRole(roleOpt)}
                          />
                          <span>{roleOpt}</span>
                        </label>
                      ))}
                    </div>
                  </div>
                </div>
              ) : null}

              <div className="flex items-center gap-3">
                <Button type="submit" size="sm" disabled={savingAcl}>
                  {savingAcl ? t.saving : t.savePermissions}
                </Button>
                {aclSaved ? <span className="text-xs text-primary">{t.permissionsSaved}</span> : null}
                {aclError ? <span className="text-xs text-destructive">{aclError}</span> : null}
              </div>
            </form>
          ) : null}

          <pre className="mt-6 whitespace-pre-wrap rounded-xl bg-card p-4 font-sans text-sm leading-7 ring-1 ring-foreground/10">
            {doc.content}
          </pre>
        </article>
      ) : null}
    </Shell>
  );
}
