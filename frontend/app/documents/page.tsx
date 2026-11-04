"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

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
import { useI18n } from "@/lib/i18n";

type Doc = {
  id: string;
  title: string;
  collection: string;
  visibility: string;
  source: string;
  status: string;
};

export default function DocumentsPage() {
  const { t } = useI18n();
  const [user, setUser] = useState<User | null>(null);
  const [docs, setDocs] = useState<Doc[] | null>(null);
  const [error, setError] = useState("");
  const [open, setOpen] = useState(false);
  const [pending, setPending] = useState(false);
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("说明\n\n## 记录\n\n在这里写可以核对的事实。");
  const [collection, setCollection] = useState("knowledge");
  const [visibility, setVisibility] = useState("lab");
  const [grants, setGrants] = useState("");

  async function load() {
    setError("");
    try {
      const [me, listed] = await Promise.all([
        currentUser(),
        api<{ documents: Doc[] }>("documents"),
      ]);
      setUser(me);
      setDocs(listed.documents);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t.error);
      setDocs([]);
    }
  }

  useEffect(() => {
    let cancelled = false;
    Promise.all([currentUser(), api<{ documents: Doc[] }>("documents")])
      .then(([me, listed]) => {
        if (cancelled) return;
        setUser(me);
        setDocs(listed.documents);
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

  return (
    <Shell>
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <h1 className="font-display text-3xl">{t.documents}</h1>
        {user && user.role !== "viewer" ? (
          <Button onClick={() => setOpen(true)}>{t.upload}</Button>
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
          <li key={doc.id} className="flex flex-wrap items-center gap-2 rounded-xl bg-card px-4 py-3 ring-1 ring-foreground/10">
            <Link href={`/documents/${doc.id}`} className="mr-auto font-medium hover:underline">
              {doc.title}
            </Link>
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
          <form
            className="grid gap-3"
            onSubmit={async (event) => {
              event.preventDefault();
              setPending(true);
              setError("");
              const form = new FormData();
              form.set("title", title);
              form.set("text", body);
              form.set("collection", collection);
              form.set("visibility", visibility);
              if (grants.trim()) form.set("grant_emails", grants);
              try {
                await api("documents", { method: "POST", body: form });
                setOpen(false);
                setTitle("");
                await load();
              } catch (err) {
                setError(err instanceof ApiError ? err.message : t.error);
              } finally {
                setPending(false);
              }
            }}
          >
            <Label htmlFor="doc-title">{t.title}</Label>
            <Input id="doc-title" value={title} onChange={(event) => setTitle(event.target.value)} required />
            <Label htmlFor="doc-body">{t.body}</Label>
            <Textarea id="doc-body" rows={8} value={body} onChange={(event) => setBody(event.target.value)} required />
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
            <Label htmlFor="grants">{t.grants}</Label>
            <Input id="grants" value={grants} onChange={(event) => setGrants(event.target.value)} placeholder="member@lab.local" />
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
