"use client";

import Link from "next/link";
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
import { Textarea } from "@/components/ui/textarea";
import { ApiError, api, currentUser, type User } from "@/lib/api";
import { type Copy, useI18n } from "@/lib/i18n";

const MAX_UPLOAD_BYTES = 5 * 1024 * 1024;

type Doc = {
  id: string;
  title: string;
  collection: string;
  visibility: string;
  source: string;
  status: "pending" | "processing" | "ready" | "failed" | string;
  error_code?: string | null;
  source_format?: string;
  original_filename?: string | null;
};

function stripExtension(filename: string): string {
  const lastDot = filename.lastIndexOf(".");
  if (lastDot <= 0) return filename;
  return filename.slice(0, lastDot);
}

function translateErrorCode(code: string | null | undefined, t: Copy): string {
  if (!code) return "";
  const map: Record<string, string> = {
    timeout: t.errTimeout,
    conn_refused: t.errConnRefused,
    bad_response: t.errBadResponse,
    dimension_mismatch: t.errDimensionMismatch,
    empty_document: t.errEmptyDocument,
  };
  return map[code] || code;
}

export default function DocumentsPage() {
  const { t } = useI18n();
  const [user, setUser] = useState<User | null>(null);
  const [docs, setDocs] = useState<Doc[] | null>(null);
  const [error, setError] = useState("");
  const [dialogError, setDialogError] = useState("");
  const [open, setOpen] = useState(false);
  const [pending, setPending] = useState(false);
  const [mode, setMode] = useState<"paste" | "file">("paste");
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("说明\n\n## 记录\n\n在这里写可以核对的事实。");
  const [collection, setCollection] = useState("knowledge");
  const [visibility, setVisibility] = useState("lab");
  const [grants, setGrants] = useState("");
  const [availableGroups, setAvailableGroups] = useState<{ slug: string; name: string }[]>([]);
  const [selectedGroups, setSelectedGroups] = useState<string[]>([]);

  const load = useCallback(async () => {
    setError("");
    try {
      const [me, listed, groupList] = await Promise.all([
        currentUser(),
        api<{ documents: Doc[] }>("documents"),
        api<{ groups: { slug: string; name: string }[] }>("groups").catch(() => ({
          groups: [],
        })),
      ]);
      setUser(me);
      setDocs(listed.documents);
      setAvailableGroups(groupList.groups || []);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t.error);
      setDocs([]);
    }
  }, [t.error]);

  useEffect(() => {
    let cancelled = false;
    Promise.all([
      currentUser(),
      api<{ documents: Doc[] }>("documents"),
      api<{ groups: { slug: string; name: string }[] }>("groups").catch(() => ({
        groups: [],
      })),
    ])
      .then(([me, listed, groupList]) => {
        if (cancelled) return;
        setUser(me);
        setDocs(listed.documents);
        setAvailableGroups(groupList.groups || []);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(err instanceof ApiError ? err.message : "这次请求没有完成。");
        setDocs([]);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const hasInProgress = Boolean(
    docs?.some((doc) => doc.status === "pending" || doc.status === "processing"),
  );

  useEffect(() => {
    if (!hasInProgress) return;
    let cancelled = false;
    const timer = setInterval(() => {
      api<{ documents: Doc[] }>("documents")
        .then((listed) => {
          if (!cancelled) {
            setDocs(listed.documents);
          }
        })
        .catch(() => {
          // Keep polling quietly on transient network hiccups.
        });
    }, 2000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [hasInProgress]);

  const collections = [
    ["knowledge", t.knowledge],
    ["project", t.project],
    ["meeting", t.meeting],
    ["paper_note", t.paper],
  ];
  const visibilities = [
    ["lab", t.labVis],
    ["restricted", t.restricted],
    ["private", t.private],
  ];

  function renderStatusBadge(doc: Doc) {
    if (doc.status === "pending" || doc.status === "processing") {
      return <Badge variant="secondary">{t.statusProcessing}</Badge>;
    }
    if (doc.status === "failed") {
      const reason = translateErrorCode(doc.error_code, t);
      return (
        <Badge variant="destructive">
          {reason ? `${t.statusFailed}：${reason}` : t.statusFailed}
        </Badge>
      );
    }
    return <Badge variant="outline">{t.statusReady}</Badge>;
  }

  return (
    <Shell>
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <h1 className="font-display text-3xl">{t.documents}</h1>
        {user && user.role !== "viewer" ? (
          <Button
            onClick={() => {
              setDialogError("");
              setOpen(true);
            }}
          >
            {t.upload}
          </Button>
        ) : (
          <p className="text-sm text-muted-foreground">{t.viewerNoUpload}</p>
        )}
      </div>
      {error ? <p className="mb-3 text-sm text-destructive">{error}</p> : null}
      {docs === null ? <p className="text-sm text-muted-foreground">{t.loading}</p> : null}
      {docs && docs.length === 0 ? (
        <div className="rounded-xl border border-dashed border-border px-4 py-8">
          <p className="font-medium">{t.docsEmpty}</p>
          <p className="mt-2 text-sm text-muted-foreground">{t.docsEmptyBody}</p>
        </div>
      ) : null}
      <ul className="grid gap-2">
        {(docs || []).map((doc) => (
          <li
            key={doc.id}
            className="flex flex-wrap items-center gap-2 rounded-xl bg-card px-4 py-3 ring-1 ring-foreground/10"
          >
            {doc.status === "ready" ? (
              <Link href={`/documents/${doc.id}`} className="mr-auto font-medium hover:underline">
                {doc.title}
              </Link>
            ) : (
              <span className="mr-auto font-medium text-muted-foreground">{doc.title}</span>
            )}
            {renderStatusBadge(doc)}
            <Badge variant="outline">{doc.collection}</Badge>
            <Badge variant="secondary">{doc.visibility}</Badge>
            {doc.source === "upload" && user?.role !== "viewer" ? (
              <Button
                variant="ghost"
                size="sm"
                onClick={async () => {
                  try {
                    await api(`documents/${doc.id}`, { method: "DELETE" });
                    await load();
                  } catch (err) {
                    setError(err instanceof ApiError ? err.message : t.error);
                  }
                }}
              >
                {t.delete}
              </Button>
            ) : null}
          </li>
        ))}
      </ul>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>{t.uploadTitle}</DialogTitle>
            <DialogDescription>{t.bodyHint}</DialogDescription>
          </DialogHeader>
          <div className="flex gap-2">
            <Button
              type="button"
              size="sm"
              variant={mode === "paste" ? "default" : "outline"}
              onClick={() => {
                setMode("paste");
                setDialogError("");
              }}
            >
              {t.modePaste}
            </Button>
            <Button
              type="button"
              size="sm"
              variant={mode === "file" ? "default" : "outline"}
              onClick={() => {
                setMode("file");
                setDialogError("");
              }}
            >
              {t.modeFile}
            </Button>
          </div>
          {dialogError ? <p className="text-sm text-destructive">{dialogError}</p> : null}
          <form
            className="grid gap-3"
            onSubmit={async (event) => {
              event.preventDefault();
              setDialogError("");
              setError("");
              const form = new FormData();
              form.set("title", title);
              form.set("collection", collection);
              form.set("visibility", visibility);
              if (grants.trim()) form.set("grant_emails", grants);
              if (visibility === "restricted" && selectedGroups.length > 0) {
                form.set("grant_groups", selectedGroups.join(","));
              }

              if (mode === "file") {
                if (!selectedFile) {
                  setDialogError(t.fileRequired);
                  return;
                }
                if (selectedFile.size > MAX_UPLOAD_BYTES) {
                  setDialogError(t.fileTooLarge);
                  return;
                }
                form.set("file", selectedFile);
              } else {
                form.set("text", body);
              }

              setPending(true);
              try {
                await api("documents", { method: "POST", body: form });
                setOpen(false);
                setTitle("");
                setSelectedFile(null);
                setSelectedGroups([]);
                await load();
              } catch (err) {
                const msg = err instanceof ApiError ? err.message : t.error;
                setDialogError(msg);
                setError(msg);
              } finally {
                setPending(false);
              }
            }}
          >
            {mode === "file" ? (
              <>
                <Label htmlFor="doc-file">{t.chooseFile}</Label>
                <Input
                  id="doc-file"
                  type="file"
                  accept=".md,.markdown,.txt,.pdf"
                  onChange={(event) => {
                    setDialogError("");
                    const file = event.target.files?.[0] || null;
                    if (file && file.size > MAX_UPLOAD_BYTES) {
                      setDialogError(t.fileTooLarge);
                      setSelectedFile(null);
                      return;
                    }
                    setSelectedFile(file);
                    if (file) {
                      setTitle(stripExtension(file.name));
                    }
                  }}
                />
              </>
            ) : null}
            <Label htmlFor="doc-title">{t.title}</Label>
            <Input
              id="doc-title"
              value={title}
              onChange={(event) => setTitle(event.target.value)}
              required
            />
            {mode === "paste" ? (
              <>
                <Label htmlFor="doc-body">{t.body}</Label>
                <Textarea
                  id="doc-body"
                  rows={8}
                  value={body}
                  onChange={(event) => setBody(event.target.value)}
                  required
                />
              </>
            ) : null}
            <Label htmlFor="collection">{t.collection}</Label>
            <select
              id="collection"
              className="h-8 rounded-lg border border-input bg-transparent px-2 text-sm"
              value={collection}
              onChange={(event) => setCollection(event.target.value)}
            >
              {collections.map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
            <Label htmlFor="visibility">{t.visibility}</Label>
            <select
              id="visibility"
              className="h-8 rounded-lg border border-input bg-transparent px-2 text-sm"
              value={visibility}
              onChange={(event) => setVisibility(event.target.value)}
            >
              {visibilities.map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
            {visibility === "restricted" ? (
              <div className="space-y-1.5">
                <Label>{t.grantGroups}</Label>
                {availableGroups.length === 0 ? (
                  <p className="text-xs text-muted-foreground">{t.noGroups}</p>
                ) : (
                  <div className="flex flex-wrap gap-2 rounded-lg border border-input p-2">
                    {availableGroups.map((group) => {
                      const checked = selectedGroups.includes(group.slug);
                      return (
                        <label
                          key={group.slug}
                          className="flex cursor-pointer items-center gap-1.5 rounded-md border border-border px-2 py-1 text-xs"
                        >
                          <input
                            type="checkbox"
                            checked={checked}
                            onChange={(event) => {
                              if (event.target.checked) {
                                setSelectedGroups((prev) =>
                                  prev.includes(group.slug) ? prev : [...prev, group.slug],
                                );
                              } else {
                                setSelectedGroups((prev) =>
                                  prev.filter((item) => item !== group.slug),
                                );
                              }
                            }}
                          />
                          <span>{group.name}</span>
                          <span className="font-mono text-muted-foreground">({group.slug})</span>
                        </label>
                      );
                    })}
                  </div>
                )}
                <p className="text-xs text-muted-foreground">{t.grantGroupsHint}</p>
              </div>
            ) : null}
            <Label htmlFor="grants">{t.grants}</Label>
            <Input
              id="grants"
              value={grants}
              onChange={(event) => setGrants(event.target.value)}
              placeholder="member@lab.local"
            />
            <p className="text-xs text-muted-foreground">{t.grantsHint}</p>
            <DialogFooter>
              <Button type="button" variant="outline" onClick={() => setOpen(false)}>
                {t.cancel}
              </Button>
              <Button type="submit" disabled={pending}>
                {pending ? t.saving : t.save}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </Shell>
  );
}
