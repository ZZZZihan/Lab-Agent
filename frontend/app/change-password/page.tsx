"use client";

import { useRouter } from "next/navigation";
import { useEffect, useMemo, useState } from "react";

import { Badge } from "@/components/ui/badge";
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
import { ApiError, changePassword, currentUser, logout, type User } from "@/lib/api";
import { useI18n } from "@/lib/i18n";

const DEFAULT_INITIAL_PASSWORD = "LabAgent@2026";

function computeDeadlineInfo(
  deadlineIso: string | null | undefined,
  locale: "zh" | "en",
): {
  formattedDeadline: string;
  remainingText: string;
  remainingHours: number;
  isUrgent: boolean;
} | null {
  if (!deadlineIso) return null;
  const deadline = new Date(deadlineIso);
  if (Number.isNaN(deadline.getTime())) return null;
  const diffMs = deadline.getTime() - Date.now();
  const remainingHours = Math.max(0, Math.floor(diffMs / (1000 * 60 * 60)));
  const isUrgent = diffMs < 24 * 60 * 60 * 1000;
  const days = Math.floor(remainingHours / 24);
  const hours = remainingHours % 24;

  const formattedDeadline = deadline
    .toISOString()
    .replace("T", " ")
    .slice(0, 16);

  let remainingText: string;
  if (locale === "zh") {
    remainingText =
      days > 0
        ? `剩余 ${days} 天 ${hours} 小时（共 ${remainingHours} 小时）`
        : `剩余 ${remainingHours} 小时`;
  } else {
    remainingText =
      days > 0
        ? `${days}d ${hours}h remaining (${remainingHours}h total)`
        : `${remainingHours}h remaining`;
  }

  return {
    formattedDeadline,
    remainingText,
    remainingHours,
    isUrgent,
  };
}

export default function ChangePasswordPage() {
  const { locale, t, toggle } = useI18n();
  const router = useRouter();
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  const [oldPw, setOldPw] = useState("");
  const [newPw, setNewPw] = useState("");
  const [confirmPw, setConfirmPw] = useState("");
  const [bindEmail, setBindEmail] = useState("");

  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [successMsg, setSuccessMsg] = useState("");

  useEffect(() => {
    let cancelled = false;
    currentUser()
      .then((found) => {
        if (cancelled) return;
        if (!found) {
          router.replace("/login");
          return;
        }
        setUser(found);
        setLoading(false);
      })
      .catch(() => {
        if (!cancelled) router.replace("/login");
      });
    return () => {
      cancelled = true;
    };
  }, [router]);

  const deadlineInfo = useMemo(
    () => computeDeadlineInfo(user?.must_change_password_by, locale),
    [user?.must_change_password_by, locale],
  );

  const checklist = useMemo(() => {
    const ruleLength = newPw.length >= 12;
    const charClasses =
      (/[a-z]/.test(newPw) ? 1 : 0) +
      (/[A-Z]/.test(newPw) ? 1 : 0) +
      (/[0-9]/.test(newPw) ? 1 : 0) +
      (/[^a-zA-Z0-9]/.test(newPw) ? 1 : 0);
    const ruleClasses = charClasses >= 3;

    const ruleNotDefaultOrOld =
      newPw.length > 0 &&
      newPw !== DEFAULT_INITIAL_PASSWORD &&
      (oldPw.length === 0 || newPw !== oldPw) &&
      !newPw.toLowerCase().startsWith("labagent-demo-2026-");

    const loweredNew = newPw.toLowerCase();
    const loginName = (user?.login_name || "").trim().toLowerCase();
    const emailSource = (user?.email || bindEmail || "").trim().toLowerCase();
    const emailPrefix = emailSource.includes("@")
      ? emailSource.split("@")[0].trim()
      : "";
    const ruleNoIdentity =
      newPw.length > 0 &&
      (!loginName || !loweredNew.includes(loginName)) &&
      (!emailPrefix || !loweredNew.includes(emailPrefix));

    const ruleConfirmMatch = newPw.length > 0 && newPw === confirmPw;

    return [
      {
        key: "length",
        passed: ruleLength,
        label:
          locale === "zh"
            ? "长度至少 12 个字符"
            : "At least 12 characters",
      },
      {
        key: "classes",
        passed: ruleClasses,
        label:
          locale === "zh"
            ? "至少包含大写字母、小写字母、数字、特殊符号中的 3 类"
            : "Contains at least 3 of: uppercase, lowercase, digits, special symbols",
      },
      {
        key: "not_default",
        passed: ruleNotDefaultOrOld,
        label:
          locale === "zh"
            ? `不能与当前口令或开源默认初始口令（${DEFAULT_INITIAL_PASSWORD}）相同`
            : `Must differ from current password and default (${DEFAULT_INITIAL_PASSWORD})`,
      },
      {
        key: "no_identity",
        passed: ruleNoIdentity,
        label:
          locale === "zh"
            ? "不能包含本人登录名（login_name）或邮箱前缀"
            : "Must not contain your login_name or email prefix",
      },
      {
        key: "confirm_match",
        passed: ruleConfirmMatch,
        label:
          locale === "zh"
            ? "两次输入的新口令一致"
            : "New password and confirmation match",
      },
    ];
  }, [newPw, oldPw, confirmPw, user?.login_name, user?.email, bindEmail, locale]);

  const allRulesPassed = useMemo(
    () => checklist.every((item) => item.passed),
    [checklist],
  );

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setSuccessMsg("");
    if (newPw !== confirmPw) {
      setError(locale === "zh" ? "两次输入的新口令不一致" : "Passwords do not match");
      return;
    }
    setSaving(true);
    try {
      await changePassword(oldPw, newPw, bindEmail);
      const verified = await currentUser();
      if (verified && !verified.must_change_password) {
        setUser(verified);
        setSuccessMsg(
          locale === "zh"
            ? "口令已更新！正在为您进入课题组资料库…"
            : "Password updated! Redirecting to Lab Assets…",
        );
        router.replace("/lab");
      } else {
        router.replace("/chat");
      }
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t.error);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="mx-auto flex min-h-screen w-full max-w-xl flex-col justify-center px-4 py-10">
      <div className="mb-6 flex flex-wrap items-center justify-between gap-3">
        <div>
          <p className="text-xs tracking-[0.2em] text-muted-foreground">{t.lab}</p>
          <h1 className="font-display text-2xl font-medium tracking-tight">
            {t.brand}
          </h1>
        </div>
        <div className="flex items-center gap-2">
          {user ? (
            <Badge variant="outline" className="font-mono text-xs">
              {user.display_name}
              {user.login_name ? ` (${user.login_name})` : ""}
            </Badge>
          ) : null}
          <Button variant="outline" size="sm" onClick={toggle}>
            {t.language}
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={async () => {
              await logout();
              router.replace("/login");
            }}
          >
            {t.logout}
          </Button>
        </div>
      </div>

      {loading ? (
        <p className="text-sm text-muted-foreground">{t.loading}</p>
      ) : (
        <Card>
          <CardHeader>
            <CardTitle>{t.changePasswordPageTitle}</CardTitle>
            <CardDescription>{t.changePasswordPageSubtitle}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            {deadlineInfo ? (
              <div
                role="status"
                className={
                  deadlineInfo.isUrgent
                    ? "rounded-md border border-destructive bg-destructive/10 px-3.5 py-2.5 text-xs font-semibold text-destructive"
                    : "rounded-md border border-amber-500/40 bg-amber-500/10 px-3.5 py-2.5 text-xs text-amber-800 dark:text-amber-300"
                }
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span>
                    {locale === "zh" ? "改密截止时间：" : "Deadline: "}
                    <span className="font-mono">{deadlineInfo.formattedDeadline}</span>
                  </span>
                  <span className="font-mono">{deadlineInfo.remainingText}</span>
                </div>
                {deadlineInfo.isUrgent ? (
                  <p className="mt-1">
                    {locale === "zh"
                      ? "⚠ 距离初始口令过期已不足 24 小时，请立即完成修改，逾期账号将被锁定。"
                      : "⚠ Less than 24 hours left before your initial password expires. Please change it immediately."}
                  </p>
                ) : null}
              </div>
            ) : null}

            <form onSubmit={handleSubmit} className="grid gap-3.5">
              <div className="space-y-1.5">
                <Label htmlFor="old_password">{t.oldPassword}</Label>
                <Input
                  id="old_password"
                  name="old_password"
                  type="password"
                  autoComplete="current-password"
                  value={oldPw}
                  onChange={(e) => setOldPw(e.target.value)}
                  required
                />
              </div>

              <div className="space-y-1.5">
                <Label htmlFor="new_password">{t.newPassword}</Label>
                <Input
                  id="new_password"
                  name="new_password"
                  type="password"
                  autoComplete="new-password"
                  value={newPw}
                  onChange={(e) => setNewPw(e.target.value)}
                  required
                />
              </div>

              <div className="space-y-1.5">
                <Label htmlFor="confirm_password">{t.confirmNewPassword}</Label>
                <Input
                  id="confirm_password"
                  name="confirm_password"
                  type="password"
                  autoComplete="new-password"
                  value={confirmPw}
                  onChange={(e) => setConfirmPw(e.target.value)}
                  required
                />
              </div>

              {!user?.email ? (
                <div className="space-y-1.5">
                  <Label htmlFor="bind_email">{t.bindEmailOptional}</Label>
                  <Input
                    id="bind_email"
                    name="bind_email"
                    type="email"
                    autoComplete="email"
                    placeholder={
                      locale === "zh"
                        ? "例如：name@university.edu.cn（选填，绑定后可用邮箱登录）"
                        : "Optional: bind your email for sign-in"
                    }
                    value={bindEmail}
                    onChange={(e) => setBindEmail(e.target.value)}
                  />
                </div>
              ) : null}

              <div className="rounded-md border border-border bg-muted/30 p-3">
                <p className="mb-2 text-xs font-medium text-muted-foreground">
                  {locale === "zh" ? "口令安全规则检查：" : "Password security checklist:"}
                </p>
                <ul className="space-y-1.5 text-xs">
                  {checklist.map((rule) => (
                    <li
                      key={rule.key}
                      className={
                        rule.passed
                          ? "flex items-center gap-2 font-medium text-emerald-600 dark:text-emerald-400"
                          : "flex items-center gap-2 text-muted-foreground"
                      }
                    >
                      <span aria-hidden="true" className="font-mono text-sm">
                        {rule.passed ? "✓" : "○"}
                      </span>
                      <span>{rule.label}</span>
                    </li>
                  ))}
                </ul>
              </div>

              {error ? (
                <p role="alert" className="text-sm text-destructive">
                  {error}
                </p>
              ) : null}
              {successMsg ? (
                <p role="status" className="text-sm font-medium text-emerald-600">
                  {successMsg}
                </p>
              ) : null}

              <Button
                type="submit"
                disabled={saving || !oldPw || !allRulesPassed}
                className="w-full"
              >
                {saving ? t.saving : t.saveNewPassword}
              </Button>
            </form>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
