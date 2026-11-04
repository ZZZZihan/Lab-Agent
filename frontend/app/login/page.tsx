"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ApiError, login } from "@/lib/api";
import { useI18n } from "@/lib/i18n";

const DEMOS = [
  ["admin@lab.local", "沈昭", "admin", "LabAgent-Demo-2026-Admin"],
  ["member@lab.local", "林可", "member", "LabAgent-Demo-2026-Member"],
  ["zhou@lab.local", "周宁", "member", "LabAgent-Demo-2026-Zhou"],
  ["viewer@lab.local", "王敏", "viewer", "LabAgent-Demo-2026-Viewer"],
];

export default function LoginPage() {
  const { t, toggle } = useI18n();
  const router = useRouter();
  const showHints = process.env.NEXT_PUBLIC_DEMO_HINTS === "true";
  const [email, setEmail] = useState(showHints ? "member@lab.local" : "");
  const [password, setPassword] = useState(showHints ? "LabAgent-Demo-2026-Member" : "");
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);

  return (
    <div className="mx-auto flex min-h-screen w-full max-w-lg flex-col justify-center px-4 py-10">
      <div className="mb-6 flex items-end justify-between">
        <div>
          <p className="text-xs tracking-[0.2em] text-muted-foreground">{t.lab}</p>
          <h1 className="font-display text-4xl">{t.brand}</h1>
          <p className="mt-2 text-sm text-muted-foreground">{t.tagline}</p>
        </div>
        <Button variant="outline" size="sm" onClick={toggle}>
          {t.language}
        </Button>
      </div>
      <Card>
        <CardHeader>
          <CardTitle>{t.login}</CardTitle>
          <CardDescription>{t.tagline}</CardDescription>
        </CardHeader>
        <CardContent>
          <form
            className="grid gap-3"
            onSubmit={async (event) => {
              event.preventDefault();
              setPending(true);
              setError("");
              try {
                await login(email, password);
                router.replace("/chat");
              } catch (err) {
                setError(err instanceof ApiError ? err.message : t.loginError);
              } finally {
                setPending(false);
              }
            }}
          >
            <Label htmlFor="email">{t.email}</Label>
            <Input id="email" type="email" value={email} onChange={(event) => setEmail(event.target.value)} required />
            <Label htmlFor="password">{t.password}</Label>
            <Input
              id="password"
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              required
            />
            {error ? <p className="text-sm text-destructive">{error}</p> : null}
            <Button type="submit" disabled={pending}>
              {pending ? t.signingIn : t.login}
            </Button>
          </form>
        </CardContent>
      </Card>
      {showHints ? (
        <div className="mt-4 rounded-xl border border-border bg-card p-4 text-sm">
          <p className="font-medium">{t.demoTitle}</p>
          <p className="mt-1 text-muted-foreground">{t.demoNote}</p>
          <ul className="mt-3 grid gap-2">
            {DEMOS.map(([account, name, role, secret]) => (
              <li key={account}>
                <button
                  type="button"
                  className="w-full rounded-lg border border-border px-3 py-2 text-left hover:bg-muted"
                  onClick={() => {
                    setEmail(account);
                    setPassword(secret);
                  }}
                >
                  <span className="font-medium">{name}</span>
                  <span className="ml-2 text-muted-foreground">{role}</span>
                  <span className="mt-1 block text-xs text-muted-foreground">{account}</span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}
