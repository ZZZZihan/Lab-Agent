"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { useShell } from "@/components/shell";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
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
import { cn } from "cn";
import {
  ApiError,
  deleteInvoice,
  downloadInvoicePdf,
  exportInvoicesCsv,
  exportInvoicesZip,
  listInvoices,
  parseInvoicePdf,
  updateInvoice,
  uploadInvoicePdf,
  type InvoiceItem,
  type InvoiceOwnerOption,
  type InvoiceSummary,
} from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { triggerBlobDownload } from "../../lab/components/labTypesAndHelpers";

const MAX_INVOICE_BYTES = 10 * 1024 * 1024;

function buildClientRenamedPreview(itemName: string, amount: string): string {
  let rawItem = itemName.trim();
  const starMatch = rawItem.match(/\*\s*([^*\n\r]{1,40}?)\s*\*\s*([^\n\r*]+)/);
  if (starMatch) {
    rawItem = starMatch[2].trim() || starMatch[1].trim();
  }
  const cleanItem =
    rawItem
      .replace(/[/\\:*?"<>|\s]+/g, "_")
      .replace(/^[_.\s]+|[_.\s]+$/g, "")
      .slice(0, 60) || "未命名项目";
  const num = Number(amount.trim().replace(/,/g, ""));
  const cleanAmount =
    amount.trim() && Number.isFinite(num) && num >= 0 ? num.toFixed(2) : "0.00";
  return `${cleanItem}+${cleanAmount}.pdf`;
}

export function InvoiceWorkspace({ mode }: { mode: "member" | "admin" }) {
  const { t } = useI18n();
  const { user } = useShell();

  const [invoices, setInvoices] = useState<InvoiceItem[]>([]);
  const [summary, setSummary] = useState<InvoiceSummary>({
    total_count: 0,
    total_amount: "0.00",
    owner_count: 0,
  });
  const [owners, setOwners] = useState<InvoiceOwnerOption[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  // Filters
  const [ownerFilter, setOwnerFilter] = useState("");
  const [queryFilter, setQueryFilter] = useState("");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());

  // Upload & auto-recognition form state
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [selectedFiles, setSelectedFiles] = useState<File[]>([]);
  const [parsingPreview, setParsingPreview] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState("");
  const [parseWarnings, setParseWarnings] = useState<string[]>([]);
  const [rawItemHint, setRawItemHint] = useState<string | null>(null);
  const [itemName, setItemName] = useState("");
  const [totalAmount, setTotalAmount] = useState("");
  const [invoiceDate, setInvoiceDate] = useState("");
  const [invoiceNumber, setInvoiceNumber] = useState("");
  const [note, setNote] = useState("");

  // Edit dialog state
  const [editTarget, setEditTarget] = useState<InvoiceItem | null>(null);
  const [editItemName, setEditItemName] = useState("");
  const [editAmount, setEditAmount] = useState("");
  const [editDate, setEditDate] = useState("");
  const [editNumber, setEditNumber] = useState("");
  const [editNote, setEditNote] = useState("");
  const [editError, setEditError] = useState("");
  const [editSaving, setEditSaving] = useState(false);

  // Busy states and re-entrancy locks
  const [busyId, setBusyId] = useState<string | null>(null);
  const [exportingKind, setExportingKind] = useState<"zip" | "csv" | null>(null);
  const uploadLockRef = useRef(false);
  const parseLockRef = useRef(false);
  const editLockRef = useRef(false);
  const actionLockRef = useRef(false);
  const exportLockRef = useRef(false);

  const scope = mode === "admin" ? "all" : "mine";
  const isViewer = user?.role === "viewer";
  const isAdmin = user?.role === "admin";

  const loadInvoices = useCallback(
    async (signal?: AbortSignal) => {
      if (mode === "admin" && user && user.role !== "admin") {
        setLoading(false);
        return;
      }
      setLoading(true);
      setError("");
      try {
        const res = await listInvoices(
          {
            scope,
            owner_id: mode === "admin" && ownerFilter ? ownerFilter : undefined,
            q: queryFilter || undefined,
            date_from: dateFrom || undefined,
            date_to: dateTo || undefined,
          },
          signal,
        );
        setInvoices(res.invoices);
        setSummary(res.summary);
        setOwners(res.owners || []);
        setSelectedIds((prev) => {
          const valid = new Set(res.invoices.map((r) => r.id));
          const next = new Set<string>();
          for (const id of prev) {
            if (valid.has(id)) next.add(id);
          }
          return next;
        });
      } catch (err) {
        if (err instanceof DOMException && err.name === "AbortError") return;
        setError(err instanceof ApiError ? err.message : t.error);
        setInvoices([]);
      } finally {
        setLoading(false);
      }
    },
    [mode, user, scope, ownerFilter, queryFilter, dateFrom, dateTo, t.error],
  );

  useEffect(() => {
    if (mode === "admin" && user && user.role !== "admin") {
      return;
    }
    let cancelled = false;
    const ctrl = new AbortController();
    listInvoices(
      {
        scope,
        owner_id: mode === "admin" && ownerFilter ? ownerFilter : undefined,
        q: queryFilter || undefined,
        date_from: dateFrom || undefined,
        date_to: dateTo || undefined,
      },
      ctrl.signal,
    )
      .then((res) => {
        if (cancelled) return;
        setError("");
        setInvoices(res.invoices);
        setSummary(res.summary);
        setOwners(res.owners || []);
        setSelectedIds((prev) => {
          const valid = new Set(res.invoices.map((r) => r.id));
          const next = new Set<string>();
          for (const id of prev) {
            if (valid.has(id)) next.add(id);
          }
          return next;
        });
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof DOMException && err.name === "AbortError") return;
        setError(err instanceof ApiError ? err.message : t.error);
        setInvoices([]);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
      ctrl.abort();
    };
  }, [mode, user, scope, ownerFilter, queryFilter, dateFrom, dateTo, t.error]);

  const renamedPreview = useMemo(
    () => buildClientRenamedPreview(itemName, totalAmount),
    [itemName, totalAmount],
  );

  const editRenamedPreview = useMemo(
    () => buildClientRenamedPreview(editItemName, editAmount),
    [editItemName, editAmount],
  );

  async function handleFileChange(e: React.ChangeEvent<HTMLInputElement>) {
    const files = Array.from(e.target.files || []);
    setUploadError("");
    setParseWarnings([]);
    setNotice("");
    setItemName("");
    setRawItemHint(null);
    setTotalAmount("");
    setInvoiceDate("");
    setInvoiceNumber("");
    if (files.length === 0) {
      setSelectedFiles([]);
      return;
    }
    for (const f of files) {
      if (!f.name.toLowerCase().endsWith(".pdf")) {
        setUploadError(`文件「${f.name}」不是 .pdf 格式`);
        setSelectedFiles([]);
        return;
      }
      if (f.size > MAX_INVOICE_BYTES) {
        setUploadError(`文件「${f.name}」超过 10 MB 上限`);
        setSelectedFiles([]);
        return;
      }
    }
    setSelectedFiles(files);

    // If a single PDF is selected, auto-parse and populate fields immediately for preview/editing
    if (files.length === 1 && !parseLockRef.current) {
      parseLockRef.current = true;
      setParsingPreview(true);
      try {
        const preview = await parseInvoicePdf(files[0]);
        setItemName(preview.item_name || "");
        setRawItemHint(preview.raw_item_name || null);
        setTotalAmount(preview.total_amount || "");
        setInvoiceDate(preview.invoice_date || "");
        setInvoiceNumber(preview.invoice_number || "");
        setParseWarnings(preview.warnings || []);
      } catch (err) {
        setItemName("");
        setRawItemHint(null);
        setTotalAmount("");
        setInvoiceDate("");
        setInvoiceNumber("");
        setUploadError(err instanceof ApiError ? err.message : t.error);
        setParseWarnings([]);
      } finally {
        parseLockRef.current = false;
        setParsingPreview(false);
      }
    }
  }

  async function handleUploadSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (uploadLockRef.current || uploading || parsingPreview) return;
    if (selectedFiles.length === 0) {
      setUploadError("请先选择要上传的 PDF 发票文件");
      return;
    }
    uploadLockRef.current = true;
    setUploading(true);
    setUploadError("");
    setNotice("");
    try {
      if (selectedFiles.length === 1) {
        const res = await uploadInvoicePdf({
          file: selectedFiles[0],
          item_name: itemName,
          total_amount: totalAmount,
          invoice_date: invoiceDate,
          invoice_number: invoiceNumber,
          note,
        });
        setNotice(`已上传并自动重命名为：${res.invoice.renamed_filename}`);
      } else {
        let successCount = 0;
        const failedNames: string[] = [];
        for (const file of selectedFiles) {
          try {
            await uploadInvoicePdf({ file, note });
            successCount += 1;
          } catch (err) {
            const msg = err instanceof ApiError ? err.message : "解析失败";
            failedNames.push(`${file.name}（${msg}）`);
          }
        }
        if (failedNames.length > 0) {
          setUploadError(
            `成功上传 ${successCount} 张，失败 ${failedNames.length} 张：${failedNames.join("；")}`,
          );
        } else {
          setNotice(`已批量上传并自动识别 ${successCount} 张 PDF 发票`);
        }
      }
      setSelectedFiles([]);
      setItemName("");
      setRawItemHint(null);
      setTotalAmount("");
      setInvoiceDate("");
      setInvoiceNumber("");
      setNote("");
      setParseWarnings([]);
      if (fileInputRef.current) {
        fileInputRef.current.value = "";
      }
      await loadInvoices();
    } catch (err) {
      setUploadError(err instanceof ApiError ? err.message : t.error);
    } finally {
      uploadLockRef.current = false;
      setUploading(false);
    }
  }

  function openEditDialog(inv: InvoiceItem) {
    setEditTarget(inv);
    setEditItemName(inv.item_name);
    setEditAmount(inv.total_amount);
    setEditDate(inv.invoice_date || "");
    setEditNumber(inv.invoice_number || "");
    setEditNote(inv.note || "");
    setEditError("");
  }

  async function handleEditSave(e: React.FormEvent) {
    e.preventDefault();
    if (!editTarget || editLockRef.current || editSaving) return;
    if (!editItemName.trim()) {
      setEditError("货物名称不能为空");
      return;
    }
    if (!editAmount.trim()) {
      setEditError("合计金额不能为空");
      return;
    }
    editLockRef.current = true;
    setEditSaving(true);
    setEditError("");
    try {
      const res = await updateInvoice(editTarget.id, {
        item_name: editItemName.trim(),
        total_amount: editAmount.trim(),
        invoice_date: editDate.trim() || null,
        invoice_number: editNumber.trim() || null,
        note: editNote.trim() || null,
      });
      setEditTarget(null);
      setNotice(`已更新发票，重命名文件名为：${res.invoice.renamed_filename}`);
      await loadInvoices();
    } catch (err) {
      setEditError(err instanceof ApiError ? err.message : t.error);
    } finally {
      editLockRef.current = false;
      setEditSaving(false);
    }
  }

  async function handleDownloadOne(inv: InvoiceItem) {
    if (actionLockRef.current || busyId) return;
    actionLockRef.current = true;
    setBusyId(inv.id);
    setError("");
    try {
      const { blob, filename } = await downloadInvoicePdf(
        inv.id,
        inv.renamed_filename,
      );
      triggerBlobDownload(blob, filename);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t.error);
    } finally {
      actionLockRef.current = false;
      setBusyId(null);
    }
  }

  async function handleDeleteOne(inv: InvoiceItem) {
    if (actionLockRef.current || busyId) return;
    actionLockRef.current = true;
    setBusyId(inv.id);
    setError("");
    try {
      await deleteInvoice(inv.id);
      setSelectedIds((prev) => {
        const next = new Set(prev);
        next.delete(inv.id);
        return next;
      });
      setNotice(`已删除发票：${inv.renamed_filename}`);
      await loadInvoices();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t.error);
    } finally {
      actionLockRef.current = false;
      setBusyId(null);
    }
  }

  async function handleExportZip() {
    if (exportLockRef.current || exportingKind) return;
    exportLockRef.current = true;
    setExportingKind("zip");
    setError("");
    try {
      const ids = selectedIds.size > 0 ? Array.from(selectedIds) : undefined;
      const { blob, filename } = await exportInvoicesZip({
        invoice_ids: ids,
        scope,
        owner_id: mode === "admin" && ownerFilter ? ownerFilter : undefined,
        q: queryFilter || undefined,
        date_from: dateFrom || undefined,
        date_to: dateTo || undefined,
      });
      triggerBlobDownload(blob, filename);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t.error);
    } finally {
      exportLockRef.current = false;
      setExportingKind(null);
    }
  }

  async function handleExportCsv() {
    if (exportLockRef.current || exportingKind) return;
    exportLockRef.current = true;
    setExportingKind("csv");
    setError("");
    try {
      const { blob, filename } = await exportInvoicesCsv({
        scope,
        owner_id: mode === "admin" && ownerFilter ? ownerFilter : undefined,
        q: queryFilter || undefined,
        date_from: dateFrom || undefined,
        date_to: dateTo || undefined,
      });
      triggerBlobDownload(blob, filename);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t.error);
    } finally {
      exportLockRef.current = false;
      setExportingKind(null);
    }
  }

  function toggleSelectAll() {
    if (selectedIds.size === invoices.length && invoices.length > 0) {
      setSelectedIds(new Set());
    } else {
      setSelectedIds(new Set(invoices.map((r) => r.id)));
    }
  }

  function toggleSelectRow(id: string) {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  }

  if (mode === "admin" && user && user.role !== "admin") {
    return (
      <div className="rounded-lg border bg-card p-6 space-y-3">
        <p className="text-sm text-muted-foreground">{t.adminInvoicesOnly}</p>
        <div>
          <Link
            href="/invoices"
            className={cn(buttonVariants({ variant: "outline", size: "sm" }))}
          >
            前往我的发票上传页
          </Link>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="font-display text-2xl font-semibold">
            {mode === "admin" ? t.adminInvoicesTitle : t.invoicesTitle}
          </h1>
          <p className="mt-1 text-sm text-muted-foreground">
            {mode === "admin" ? t.adminInvoicesSubtitle : t.invoicesSubtitle}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {mode === "member" && isAdmin ? (
            <Link
              href="/admin/invoices"
              className={cn(buttonVariants({ variant: "outline", size: "sm" }))}
            >
              进入后台全组发票汇总与导出 →
            </Link>
          ) : null}
          {mode === "admin" ? (
            <Link
              href="/invoices"
              className={cn(buttonVariants({ variant: "outline", size: "sm" }))}
            >
              返回个人发票页
            </Link>
          ) : null}
        </div>
      </div>

      {/* Summary KPI Cards */}
      <div className="grid gap-4 sm:grid-cols-3">
        <div className="rounded-lg border bg-card p-4">
          <div className="text-xs text-muted-foreground">
            {mode === "admin" ? "当前筛选发票总数" : "我上传的发票数"}
          </div>
          <div className="mt-1 font-display text-2xl font-semibold">
            {summary.total_count} <span className="text-sm font-normal">张</span>
          </div>
        </div>
        <div className="rounded-lg border bg-card p-4">
          <div className="text-xs text-muted-foreground">
            {mode === "admin" ? "当前筛选合计总金额" : "我的发票累计合计金额"}
          </div>
          <div className="mt-1 font-display text-2xl font-semibold text-emerald-700 dark:text-emerald-400">
            ¥{summary.total_amount}
          </div>
        </div>
        <div className="rounded-lg border bg-card p-4">
          <div className="text-xs text-muted-foreground">
            {mode === "admin" ? "涉及上传成员数" : "下载导出重命名规则"}
          </div>
          <div className="mt-1 font-display text-lg font-semibold">
            {mode === "admin" ? (
              <>
                {summary.owner_count}{" "}
                <span className="text-sm font-normal">人</span>
              </>
            ) : (
              <span className="font-mono text-sm">货物名称+合计金额.pdf</span>
            )}
          </div>
        </div>
      </div>

      {/* Upload & Auto-Recognition Form */}
      {isViewer ? (
        <div className="rounded-lg border bg-card p-4 text-sm text-muted-foreground">
          {t.viewerNoUpload}
        </div>
      ) : (
        <form
          onSubmit={handleUploadSubmit}
          className="rounded-lg border bg-card p-5 space-y-4"
        >
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <h2 className="font-medium text-base">
                上传 PDF 发票（自动识别货物名称、合计金额与开票日期）
              </h2>
              <p className="text-xs text-muted-foreground mt-0.5">
                选择单张 PDF 时会立即识别并显示预览，可核对或微调后入库；选择多张 PDF 时可一键批量识别入库。
              </p>
            </div>
            {selectedFiles.length === 1 && (itemName || totalAmount) ? (
              <Badge variant="secondary" className="font-mono text-xs">
                重命名预览：{renamedPreview}
              </Badge>
            ) : null}
          </div>

          <div className="grid gap-4 sm:grid-cols-2">
            <div className="sm:col-span-2">
              <Label htmlFor="invoice-pdf-file">
                选择 PDF 发票文件（支持单选或多选 .pdf，单文件不超过 10 MB）
              </Label>
              <Input
                ref={fileInputRef}
                id="invoice-pdf-file"
                type="file"
                accept=".pdf,application/pdf"
                multiple
                onChange={handleFileChange}
                disabled={uploading || parsingPreview}
                className="mt-1"
              />
            </div>

            {selectedFiles.length <= 1 ? (
              <>
                <div>
                  <Label htmlFor="invoice-item-name">
                    货物名称（自动提取，可修改）
                  </Label>
                  <Input
                    id="invoice-item-name"
                    value={itemName}
                    onChange={(e) => setItemName(e.target.value)}
                    placeholder={
                      parsingPreview
                        ? "正在从 PDF 识别货物名称…"
                        : "例如：高纯试剂、算力测试费、餐费"
                    }
                    disabled={uploading || parsingPreview}
                    className="mt-1"
                  />
                  {rawItemHint && rawItemHint !== itemName ? (
                    <p className="mt-1 text-xs text-muted-foreground">
                      发票原始行：{rawItemHint}
                    </p>
                  ) : null}
                </div>

                <div>
                  <Label htmlFor="invoice-total-amount">
                    合计金额（元，自动提取，可修改）
                  </Label>
                  <Input
                    id="invoice-total-amount"
                    value={totalAmount}
                    onChange={(e) => setTotalAmount(e.target.value)}
                    placeholder={
                      parsingPreview ? "正在从 PDF 识别合计金额…" : "例如：1280.00"
                    }
                    disabled={uploading || parsingPreview}
                    className="mt-1"
                  />
                </div>

                <div>
                  <Label htmlFor="invoice-date">
                    开票日期（自动提取，可修改）
                  </Label>
                  <Input
                    id="invoice-date"
                    type="date"
                    value={invoiceDate}
                    onChange={(e) => setInvoiceDate(e.target.value)}
                    disabled={uploading || parsingPreview}
                    className="mt-1"
                  />
                </div>

                <div>
                  <Label htmlFor="invoice-number">
                    发票号码（选填，自动提取）
                  </Label>
                  <Input
                    id="invoice-number"
                    value={invoiceNumber}
                    onChange={(e) => setInvoiceNumber(e.target.value)}
                    placeholder="选填：发票号码"
                    disabled={uploading || parsingPreview}
                    className="mt-1"
                  />
                </div>
              </>
            ) : (
              <div className="sm:col-span-2 rounded-md border border-stone-200/80 bg-stone-50/70 p-3 text-xs text-muted-foreground dark:border-stone-800 dark:bg-stone-900/50">
                已选择 {selectedFiles.length} 张 PDF 发票，点击下方按钮将自动逐张提取货物名称、合计金额与开票日期并入库。
              </div>
            )}

            <div className="sm:col-span-2">
              <Label htmlFor="invoice-note">备注（选填）</Label>
              <Input
                id="invoice-note"
                value={note}
                onChange={(e) => setNote(e.target.value)}
                placeholder="选填：报销批次或用途说明"
                disabled={uploading || parsingPreview}
                className="mt-1"
              />
            </div>
          </div>

          {parseWarnings.length > 0 ? (
            <div className="rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-800 dark:text-amber-300">
              {parseWarnings.join("；")}
            </div>
          ) : null}

          {uploadError ? (
            <p className="text-sm text-destructive" role="alert">
              {uploadError}
            </p>
          ) : null}

          {notice ? (
            <p className="text-sm text-emerald-700 dark:text-emerald-400">
              {notice}
            </p>
          ) : null}

          <div className="flex items-center justify-end gap-2">
            <Button
              type="submit"
              disabled={
                uploading || parsingPreview || selectedFiles.length === 0
              }
            >
              {parsingPreview
                ? "正在识别发票字段…"
                : uploading
                  ? "正在上传入库…"
                  : selectedFiles.length > 1
                    ? `批量识别并上传 (${selectedFiles.length} 张)`
                    : "确认并上传发票"}
            </Button>
          </div>
        </form>
      )}

      {/* Filter & Export Bar */}
      <div className="rounded-lg border bg-card p-4 space-y-3">
        <div className="grid gap-3 sm:grid-cols-4">
          {mode === "admin" ? (
            <div>
              <Label htmlFor="filter-owner" className="text-xs">
                按上传成员筛选
              </Label>
              <select
                id="filter-owner"
                value={ownerFilter}
                onChange={(e) => setOwnerFilter(e.target.value)}
                className="mt-1 flex h-9 w-full rounded-md border border-input bg-background px-3 py-1 text-sm"
              >
                <option value="">全部成员</option>
                {owners.map((o) => (
                  <option key={o.id} value={o.id}>
                    {o.display_name} ({o.email})
                  </option>
                ))}
              </select>
            </div>
          ) : null}

          <div className={mode === "admin" ? "" : "sm:col-span-2"}>
            <Label htmlFor="filter-q" className="text-xs">
              搜索货物名称 / 发票号 / 原文件名
            </Label>
            <Input
              id="filter-q"
              value={queryFilter}
              onChange={(e) => setQueryFilter(e.target.value)}
              placeholder="输入关键词筛选…"
              className="mt-1"
            />
          </div>

          <div>
            <Label htmlFor="filter-date-from" className="text-xs">
              开票起始日期
            </Label>
            <Input
              id="filter-date-from"
              type="date"
              value={dateFrom}
              onChange={(e) => setDateFrom(e.target.value)}
              className="mt-1"
            />
          </div>

          <div>
            <Label htmlFor="filter-date-to" className="text-xs">
              开票截止日期
            </Label>
            <Input
              id="filter-date-to"
              type="date"
              value={dateTo}
              onChange={(e) => setDateTo(e.target.value)}
              className="mt-1"
            />
          </div>
        </div>

        <div className="flex flex-wrap items-center justify-between gap-2 pt-1">
          <div className="flex flex-wrap items-center gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={toggleSelectAll}
              disabled={invoices.length === 0}
            >
              {selectedIds.size === invoices.length && invoices.length > 0
                ? "取消全选"
                : "全选当前列表"}
            </Button>
            {(ownerFilter || queryFilter || dateFrom || dateTo) ? (
              <Button
                type="button"
                variant="ghost"
                size="sm"
                onClick={() => {
                  setOwnerFilter("");
                  setQueryFilter("");
                  setDateFrom("");
                  setDateTo("");
                }}
              >
                重置筛选
              </Button>
            ) : null}
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={handleExportCsv}
              disabled={invoices.length === 0 || exportingKind !== null}
            >
              {exportingKind === "csv" ? "正在导出 CSV…" : "导出汇总表 (.csv)"}
            </Button>
            <Button
              type="button"
              size="sm"
              onClick={handleExportZip}
              disabled={invoices.length === 0 || exportingKind !== null}
            >
              {exportingKind === "zip"
                ? "正在打包重命名 PDF…"
                : selectedIds.size > 0
                  ? `打包导出所选重命名 PDF (${selectedIds.size} 张 .zip)`
                  : "打包导出全部重命名 PDF (.zip)"}
            </Button>
          </div>
        </div>
      </div>

      {error ? (
        <p className="text-sm text-destructive" role="alert">
          {error}
        </p>
      ) : null}

      {/* Invoices List Table */}
      <div className="rounded-lg border bg-card overflow-x-auto">
        {loading ? (
          <p className="p-6 text-sm text-muted-foreground">{t.loading}</p>
        ) : invoices.length === 0 ? (
          <p className="p-6 text-sm text-muted-foreground">
            暂无发票记录。上传 PDF 发票后将自动识别货物名称、合计金额与开票日期。
          </p>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b bg-muted/40 text-left text-xs text-muted-foreground">
                <th className="p-3 w-10">
                  <input
                    type="checkbox"
                    aria-label="全选发票"
                    checked={
                      invoices.length > 0 && selectedIds.size === invoices.length
                    }
                    onChange={toggleSelectAll}
                  />
                </th>
                <th className="p-3">下载重命名文件名</th>
                <th className="p-3">货物名称</th>
                <th className="p-3">合计金额</th>
                <th className="p-3">开票日期</th>
                {mode === "admin" ? <th className="p-3">上传成员</th> : null}
                <th className="p-3">原文件名 / 发票号</th>
                <th className="p-3 text-right">操作</th>
              </tr>
            </thead>
            <tbody>
              {invoices.map((inv) => (
                <tr key={inv.id} className="border-b last:border-b-0">
                  <td className="p-3">
                    <input
                      type="checkbox"
                      aria-label={`选择 ${inv.renamed_filename}`}
                      checked={selectedIds.has(inv.id)}
                      onChange={() => toggleSelectRow(inv.id)}
                    />
                  </td>
                  <td className="p-3 font-mono text-xs font-medium text-emerald-800 dark:text-emerald-300">
                    {inv.renamed_filename}
                  </td>
                  <td className="p-3 font-medium">
                    <div>{inv.item_name}</div>
                    {inv.note ? (
                      <div className="text-xs text-muted-foreground">
                        备注：{inv.note}
                      </div>
                    ) : null}
                  </td>
                  <td className="p-3 font-mono font-semibold">
                    ¥{inv.total_amount}
                  </td>
                  <td className="p-3 font-mono text-xs">
                    {inv.invoice_date || "—"}
                  </td>
                  {mode === "admin" ? (
                    <td className="p-3">
                      <div className="font-medium">{inv.owner_name}</div>
                      <div className="text-xs text-muted-foreground">
                        {inv.owner_email}
                      </div>
                    </td>
                  ) : null}
                  <td className="p-3 text-xs text-muted-foreground">
                    <div className="truncate max-w-[180px]" title={inv.original_filename}>
                      {inv.original_filename}
                    </div>
                    {inv.invoice_number ? (
                      <div className="font-mono">NO. {inv.invoice_number}</div>
                    ) : null}
                  </td>
                  <td className="p-3 text-right">
                    <div className="flex justify-end gap-1.5">
                      <Button
                        type="button"
                        size="sm"
                        variant="outline"
                        disabled={busyId === inv.id}
                        onClick={() => handleDownloadOne(inv)}
                      >
                        下载重命名 PDF
                      </Button>
                      <Button
                        type="button"
                        size="sm"
                        variant="ghost"
                        disabled={busyId === inv.id}
                        onClick={() => openEditDialog(inv)}
                      >
                        编辑
                      </Button>
                      <Button
                        type="button"
                        size="sm"
                        variant="ghost"
                        className="text-destructive"
                        disabled={busyId === inv.id}
                        onClick={() => handleDeleteOne(inv)}
                      >
                        删除
                      </Button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* Edit Invoice Dialog */}
      <Dialog
        open={editTarget !== null}
        onOpenChange={(open) => {
          if (!open && !editSaving) setEditTarget(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>修改发票识别信息与重命名预览</DialogTitle>
            <DialogDescription>
              修改货物名称或合计金额后，下载和导出时的文件名将自动更新为「货物名称+合计金额.pdf」。
            </DialogDescription>
          </DialogHeader>
          <form onSubmit={handleEditSave} className="grid gap-3">
            <div>
              <Label htmlFor="edit-item-name">货物名称</Label>
              <Input
                id="edit-item-name"
                value={editItemName}
                onChange={(e) => setEditItemName(e.target.value)}
                required
              />
            </div>
            <div>
              <Label htmlFor="edit-total-amount">合计金额（元）</Label>
              <Input
                id="edit-total-amount"
                value={editAmount}
                onChange={(e) => setEditAmount(e.target.value)}
                required
              />
            </div>
            <div>
              <Label htmlFor="edit-invoice-date">开票日期</Label>
              <Input
                id="edit-invoice-date"
                type="date"
                value={editDate}
                onChange={(e) => setEditDate(e.target.value)}
              />
            </div>
            <div>
              <Label htmlFor="edit-invoice-number">发票号码（选填）</Label>
              <Input
                id="edit-invoice-number"
                value={editNumber}
                onChange={(e) => setEditNumber(e.target.value)}
              />
            </div>
            <div>
              <Label htmlFor="edit-invoice-note">备注（选填）</Label>
              <Input
                id="edit-invoice-note"
                value={editNote}
                onChange={(e) => setEditNote(e.target.value)}
              />
            </div>
            <div className="rounded-md bg-muted/50 px-3 py-2 text-xs font-mono">
              重命名后文件名：{editRenamedPreview}
            </div>
            {editError ? (
              <p className="text-sm text-destructive">{editError}</p>
            ) : null}
            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                onClick={() => setEditTarget(null)}
                disabled={editSaving}
              >
                {t.cancel}
              </Button>
              <Button type="submit" disabled={editSaving}>
                {editSaving ? t.saving : "保存修改"}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </div>
  );
}
