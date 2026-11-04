"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";

import { Shell } from "@/components/shell";
import { Badge } from "@/components/ui/badge";
import { ApiError, api } from "@/lib/api";
import { useI18n } from "@/lib/i18n";

type Detail = {
  id: string;
  title: string;
  collection: string;
  visibility: string;
  content: string;
};

export default function DocumentPage() {
  const { t } = useI18n();
  const params = useParams<{ id: string }>();
  const [doc, setDoc] = useState<Detail | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    api<Detail>(`documents/${params.id}`)
      .then(setDoc)
      .catch((err: unknown) => setError(err instanceof ApiError ? err.message : t.error));
  }, [params.id, t.error]);

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
          <pre className="mt-6 whitespace-pre-wrap rounded-xl bg-card p-4 font-sans text-sm leading-7 ring-1 ring-foreground/10">
            {doc.content}
          </pre>
        </article>
      ) : null}
    </Shell>
  );
}
