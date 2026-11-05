"use client";

import { useCallback, useEffect, useState } from "react";

import { Shell } from "@/components/shell";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ApiError, api } from "@/lib/api";
import { useI18n } from "@/lib/i18n";

type PatToken = {
  id: string;
  name: string;
  prefix: string;
  created_at: string | null;
  last_used_at: string | null;
  expires_at: string | null;
  revoked_at: string | null;
  is_expired?: boolean;
  token?: string;
};

function formatDate(iso: string | null): string {
  if (!iso) return "—";
  return iso.slice(0, 19).replace("T", " ");
}

export default function SettingsPage() {
  const { t } = useI18n();
  const [tokens, setTokens] = useState<PatToken[] | null>(null);
  const [error, setError] = useState("");
  const [name, setName] = useState("");
  const [expiresDays, setExpiresDays] = useState("90");
  const [creating, setCreating] = useState(false);
  const [newRawToken, setNewRawToken] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const load = useCallback(async () => {
    setError("");
    try {
      const res = await api<{ tokens: PatToken[] }>("tokens");
      setTokens(res.tokens);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t.error);
      setTokens([]);
    }
  }, [t.error]);

  useEffect(() => {
    let cancelled = false;
    api<{ tokens: PatToken[] }>("tokens")
      .then((res) => {
        if (!cancelled) setTokens(res.tokens);
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setError(err instanceof ApiError ? err.message : t.error);
          setTokens([]);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [t.error]);

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    if (!name.trim()) return;
    setError("");
    setCreating(true);
    setCopied(false);
    try {
      const parsedDays = parseInt(expiresDays, 10);
      const created = await api<PatToken>("tokens", {
        method: "POST",
        body: JSON.stringify({
          name: name.trim(),
          expires_in_days: Number.isFinite(parsedDays) && parsedDays > 0 ? parsedDays : 90,
        }),
      });
      setName("");
      setNewRawToken(created.token || null);
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t.error);
    } finally {
      setCreating(false);
    }
  }

  async function handleRevoke(tokenId: string) {
    setError("");
    try {
      await api(`tokens/${tokenId}`, { method: "DELETE" });
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t.error);
    }
  }

  async function handleCopy() {
    if (!newRawToken) return;
    try {
      await navigator.clipboard.writeText(newRawToken);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  }

  return (
    <Shell>
      <div className="space-y-6">
        <div>
          <h1 className="font-display text-2xl">{t.tokensTitle}</h1>
          <p className="mt-1 text-sm text-muted-foreground">{t.tokensHint}</p>
        </div>

        <form
          onSubmit={handleCreate}
          className="flex flex-wrap items-end gap-3 rounded-lg border border-border bg-card p-4"
        >
          <div className="min-w-[220px] flex-1 space-y-1.5">
            <Label htmlFor="token-name">{t.tokenName}</Label>
            <Input
              id="token-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Claude Code MCP"
              required
              maxLength={120}
            />
          </div>
          <div className="w-36 space-y-1.5">
            <Label htmlFor="token-days">{t.tokenExpiresDays}</Label>
            <Input
              id="token-days"
              type="number"
              min={1}
              max={3650}
              value={expiresDays}
              onChange={(e) => setExpiresDays(e.target.value)}
            />
          </div>
          <Button type="submit" disabled={creating || !name.trim()}>
            {t.createToken}
          </Button>
        </form>

        {newRawToken ? (
          <div
            role="alert"
            className="space-y-2 rounded-lg border border-amber-500/40 bg-amber-500/10 p-4 text-sm"
          >
            <p className="font-medium text-amber-900 dark:text-amber-200">
              {t.tokenCreatedBanner}
            </p>
            <div className="flex flex-wrap items-center gap-2">
              <code className="rounded bg-background px-2.5 py-1.5 font-mono text-xs break-all">
                {newRawToken}
              </code>
              <Button type="button" variant="outline" size="sm" onClick={handleCopy}>
                {copied ? t.copiedToken : t.copyToken}
              </Button>
            </div>
          </div>
        ) : null}

        {error ? <p className="text-sm text-destructive">{error}</p> : null}

        {tokens === null ? (
          <p className="text-sm text-muted-foreground">{t.loading}</p>
        ) : tokens.length === 0 ? (
          <p className="text-sm text-muted-foreground">{t.noTokens}</p>
        ) : (
          <div className="overflow-x-auto rounded-lg border border-border bg-card">
            <table className="w-full text-left text-sm">
              <thead className="border-b border-border text-xs text-muted-foreground">
                <tr>
                  <th className="px-4 py-3">{t.tokenName}</th>
                  <th className="px-4 py-3">{t.tokenPrefix}</th>
                  <th className="px-4 py-3">{t.userStatus}</th>
                  <th className="px-4 py-3">{t.tokenCreated}</th>
                  <th className="px-4 py-3">{t.tokenLastUsed}</th>
                  <th className="px-4 py-3">{t.tokenExpiresAt}</th>
                  <th className="px-4 py-3 text-right">{t.action}</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {tokens.map((tok) => {
                  const isRevoked = Boolean(tok.revoked_at);
                  const isExpired = !isRevoked && Boolean(tok.is_expired);
                  return (
                    <tr key={tok.id}>
                      <td className="px-4 py-3 font-medium">{tok.name}</td>
                      <td className="px-4 py-3 font-mono text-xs">{tok.prefix}…</td>
                      <td className="px-4 py-3">
                        {isRevoked ? (
                          <Badge variant="secondary">{t.tokenRevoked}</Badge>
                        ) : isExpired ? (
                          <Badge variant="secondary">{t.tokenExpired}</Badge>
                        ) : (
                          <Badge variant="default">{t.tokenActive}</Badge>
                        )}
                      </td>
                      <td className="px-4 py-3 text-xs text-muted-foreground">
                        {formatDate(tok.created_at)}
                      </td>
                      <td className="px-4 py-3 text-xs text-muted-foreground">
                        {tok.last_used_at ? formatDate(tok.last_used_at) : t.tokenNeverUsed}
                      </td>
                      <td className="px-4 py-3 text-xs text-muted-foreground">
                        {formatDate(tok.expires_at)}
                      </td>
                      <td className="px-4 py-3 text-right">
                        {!isRevoked ? (
                          <Button
                            variant="outline"
                            size="sm"
                            onClick={() => handleRevoke(tok.id)}
                          >
                            {t.revokeToken}
                          </Button>
                        ) : null}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </Shell>
  );
}
