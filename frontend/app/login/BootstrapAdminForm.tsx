"use client";

import { useRouter } from "next/navigation";
import { useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ApiError, bootstrapAdmin, mapLabErrorCode } from "@/lib/api";
import { useI18n } from "@/lib/i18n";

export function BootstrapAdminForm({
  setupTokenRequired,
  onBootstrapClosed,
}: {
  setupTokenRequired: boolean;
  onBootstrapClosed?: () => void;
}) {
  const { t } = useI18n();
  const router = useRouter();
  const [displayName, setDisplayName] = useState("");
  const [email, setEmail] = useState("");
  const [staffNo, setStaffNo] = useState("");
  const [password, setPassword] = useState("");
  const [setupToken, setSetupToken] = useState("");
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);
  const submittingRef = useRef(false);

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t.bootstrapTitle}</CardTitle>
        <CardDescription>{t.bootstrapSubtitle}</CardDescription>
      </CardHeader>
      <CardContent>
        <form
          className="grid gap-3.5"
          onSubmit={async (event) => {
            event.preventDefault();
            if (submittingRef.current || pending) return;
            const trimmedName = displayName.trim();
            const trimmedEmail = email.trim();
            const trimmedStaffNo = staffNo.trim();
            const trimmedToken = setupToken.trim();
            if (!trimmedName || !trimmedEmail || !password) {
              return;
            }
            if (
              trimmedStaffNo &&
              !/^[A-Za-z0-9]{6,12}$/.test(trimmedStaffNo)
            ) {
              setError(mapLabErrorCode("staff_no_invalid"));
              return;
            }
            submittingRef.current = true;
            setPending(true);
            setError("");
            try {
              await bootstrapAdmin({
                display_name: trimmedName,
                email: trimmedEmail,
                ...(trimmedStaffNo ? { staff_no: trimmedStaffNo } : {}),
                password,
                ...(setupTokenRequired && trimmedToken
                  ? { setup_token: trimmedToken }
                  : {}),
              });
              router.replace("/lab?coldstart=1");
            } catch (err) {
              if (err instanceof ApiError) {
                setError(mapLabErrorCode(err.code, err.message));
                if (err.code === "bootstrap_closed") {
                  onBootstrapClosed?.();
                }
              } else {
                setError(t.error);
              }
            } finally {
              submittingRef.current = false;
              setPending(false);
            }
          }}
        >
          <div className="grid gap-1.5">
            <Label htmlFor="bootstrap-display-name">
              {t.bootstrapDisplayNameLabel}
            </Label>
            <Input
              id="bootstrap-display-name"
              type="text"
              autoComplete="name"
              placeholder="例如：沈昭"
              value={displayName}
              onChange={(event) => setDisplayName(event.target.value)}
              required
            />
            <p className="text-xs text-muted-foreground">
              {t.bootstrapDisplayNameHint}
            </p>
          </div>

          <div className="grid gap-1.5">
            <Label htmlFor="bootstrap-email">{t.bootstrapEmailLabel}</Label>
            <Input
              id="bootstrap-email"
              type="email"
              autoComplete="email"
              placeholder="admin@lab.edu.cn"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              required
            />
          </div>

          <div className="grid gap-1.5">
            <Label htmlFor="bootstrap-staff-no">
              {t.bootstrapStaffNoLabel}
            </Label>
            <Input
              id="bootstrap-staff-no"
              type="text"
              autoComplete="username"
              placeholder="例如：20180042"
              value={staffNo}
              onChange={(event) => setStaffNo(event.target.value)}
            />
            <p className="text-xs text-muted-foreground">
              {t.bootstrapStaffNoHint}
            </p>
          </div>

          <div className="grid gap-1.5">
            <Label htmlFor="bootstrap-password">
              {t.bootstrapPasswordLabel}
            </Label>
            <Input
              id="bootstrap-password"
              type="password"
              autoComplete="new-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              minLength={12}
              required
            />
            <p className="text-xs text-muted-foreground">
              {t.bootstrapPasswordHint}
            </p>
          </div>

          {setupTokenRequired ? (
            <div className="grid gap-1.5">
              <Label htmlFor="bootstrap-setup-token">
                {t.bootstrapSetupTokenLabel}
              </Label>
              <Input
                id="bootstrap-setup-token"
                type="password"
                autoComplete="off"
                value={setupToken}
                onChange={(event) => setSetupToken(event.target.value)}
                required
              />
              <p className="text-xs text-muted-foreground">
                {t.bootstrapSetupTokenHint}
              </p>
            </div>
          ) : null}

          {error ? (
            <p role="alert" className="text-sm text-destructive">
              {error}
            </p>
          ) : null}

          <Button type="submit" disabled={pending}>
            {pending ? t.bootstrapSubmitting : t.bootstrapSubmitBtn}
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}
