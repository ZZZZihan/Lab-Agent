"use client";


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
import {
  type BasketOptions,
} from "@/lib/api";

import {
  BASKET_KIND_LABELS,
  BASKET_MAX_ITEMS,
  basketKey,
} from "./labTypesAndHelpers";
import type { LabPageContext } from "../page";

export function ReuseBasketBar({ ctx }: { ctx: LabPageContext }) {
  const {
    setStatusBanner,
    basketItems,
    basketOptions,
    setBasketOptions,
    basketPanelOpen,
    setBasketPanelOpen,
    basketConfirmClear,
    setBasketConfirmClear,
    basketActionLoading,
    clipboardFallbackText,
    setClipboardFallbackText,
    t,
    persistBasketToStorage,
    basketCounts,
    updateBasketItems,
    updateBasketOption,
    removeItemsFromBasket,
    moveBasketItem,
    handleBasketBatchCopy,
    handleBasketDownloadSignaturesZip,
    handleBasketExportSelectedDocx,
  } = ctx;

  return (
    <>
        {/* ===================================================================
            Feature 2: Floating Bottom Proposal Reuse Basket ("写本子素材篮")
            =================================================================== */}
        <div
          role="region"
          aria-label={`写本子素材篮，${basketItems.length} 项`}
          className="fixed inset-x-0 bottom-0 z-40 border-t border-border bg-card/95 backdrop-blur supports-[backdrop-filter]:bg-card/85 shadow-lg"
        >
          <div className="mx-auto max-w-6xl px-4 py-2.5 space-y-2">
            {/* Expandable Basket Item Ordering & Removal Panel */}
            {basketPanelOpen ? (
              <div className="rounded-lg border border-border bg-background p-3 space-y-2.5 max-h-64 overflow-y-auto">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex items-center gap-2">
                    <span className="font-semibold text-[13px] text-foreground">
                      {t.reuseBasketTitle}清单 ({basketItems.length} /{" "}
                      {BASKET_MAX_ITEMS})
                    </span>
                    <span className="text-[11px] text-muted-foreground">
                      支持上下调整导出顺序或单条移除
                    </span>
                  </div>
                  <div className="flex items-center gap-2">
                    <label className="flex items-center gap-1 text-[11px] text-muted-foreground">
                      签名包命名：
                      <select
                        value={basketOptions.naming}
                        onChange={(e) =>
                          updateBasketOption(
                            "naming",
                            e.target.value as BasketOptions["naming"],
                          )
                        }
                        className="h-6 rounded border border-input bg-card px-1.5 text-[11px] text-foreground"
                      >
                        <option value="seq_name">01_姓名.ext (seq_name)</option>
                        <option value="name">姓名.ext (name)</option>
                        <option value="name_id">姓名_ID.ext (name_id)</option>
                      </select>
                    </label>
                    {basketItems.length > 0 ? (
                      basketConfirmClear ? (
                        <div className="inline-flex items-center gap-1">
                          <span className="text-[11px] text-destructive">
                            确认清空？
                          </span>
                          <Button
                            type="button"
                            size="sm"
                            variant="destructive"
                            className="h-6 px-2 text-[11px]"
                            onClick={() => {
                              updateBasketItems([]);
                              setBasketConfirmClear(false);
                              setStatusBanner("已清空写本子素材篮");
                            }}
                          >
                            确定清空
                          </Button>
                          <Button
                            type="button"
                            size="sm"
                            variant="outline"
                            className="h-6 px-2 text-[11px]"
                            onClick={() => setBasketConfirmClear(false)}
                          >
                            {t.cancel}
                          </Button>
                        </div>
                      ) : (
                        <Button
                          type="button"
                          size="sm"
                          variant="outline"
                          className="h-6 px-2 text-[11px] text-destructive"
                          onClick={() => setBasketConfirmClear(true)}
                        >
                          清空素材篮
                        </Button>
                      )
                    ) : null}
                    <Button
                      type="button"
                      size="sm"
                      variant="ghost"
                      className="h-6 px-2 text-[11px]"
                      onClick={() => setBasketPanelOpen(false)}
                    >
                      收起清单
                    </Button>
                  </div>
                </div>

                {basketItems.length === 0 ? (
                  <p className="py-3 text-center text-[12px] text-muted-foreground">
                    素材篮为空。请在上方 4 个标签页或个人全息档案袋中勾选需要复用的成员、项目、成果或设备。
                  </p>
                ) : (
                  <div className="divide-y divide-border rounded border border-border text-[12px]">
                    {basketItems.map((it, idx) => (
                      <div
                        key={basketKey(it.kind, it.id)}
                        className="flex items-center justify-between gap-2 px-2.5 py-1.5"
                      >
                        <div className="flex items-center gap-2 truncate">
                          <span className="font-mono text-[11px] text-muted-foreground">
                            #{idx + 1}
                          </span>
                          <Badge variant="outline" className="text-[10px]">
                            {BASKET_KIND_LABELS[it.kind]}
                          </Badge>
                          <span className="truncate font-medium text-foreground">
                            {it.label}
                          </span>
                        </div>
                        <div className="flex shrink-0 items-center gap-1">
                          <Button
                            type="button"
                            size="sm"
                            variant="ghost"
                            className="h-6 px-1.5 text-[11px]"
                            disabled={idx === 0}
                            aria-label={`上移 ${it.label}`}
                            onClick={() => moveBasketItem(idx, -1)}
                          >
                            ↑
                          </Button>
                          <Button
                            type="button"
                            size="sm"
                            variant="ghost"
                            className="h-6 px-1.5 text-[11px]"
                            disabled={idx === basketItems.length - 1}
                            aria-label={`下移 ${it.label}`}
                            onClick={() => moveBasketItem(idx, 1)}
                          >
                            ↓
                          </Button>
                          <Button
                            type="button"
                            size="sm"
                            variant="ghost"
                            className="h-6 px-1.5 text-[11px] text-destructive"
                            aria-label={`移除 ${it.label}`}
                            onClick={() =>
                              removeItemsFromBasket(
                                new Set([basketKey(it.kind, it.id)]),
                              )
                            }
                          >
                            ✕
                          </Button>
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            ) : null}

            {/* Main Bottom Bar */}
            <div className="flex flex-wrap items-center justify-between gap-3">
              {/* Left: Summary & Toggle Drawer Button */}
              <div className="flex flex-wrap items-center gap-2.5">
                <Button
                  type="button"
                  size="sm"
                  variant={basketItems.length > 0 ? "secondary" : "outline"}
                  aria-label={`写本子素材篮，${basketItems.length} 项`}
                  className="h-8 px-2.5 text-[12px] font-medium"
                  onClick={() => setBasketPanelOpen((prev) => !prev)}
                >
                  🧺 {t.reuseBasketTitle} ({basketItems.length})
                  <span className="ml-1 text-[11px] opacity-80">
                    {basketPanelOpen ? "▾" : "▴"}
                  </span>
                </Button>
                <span className="text-[12px] font-medium text-foreground">
                  已选 {basketCounts.person} 人 · {basketCounts.project} 项项目
                  · {basketCounts.achievement} 篇成果 · {basketCounts.facility}{" "}
                  项条件
                </span>
              </div>

              {/* Center: Formatting Options */}
              <div className="flex flex-wrap items-center gap-3 text-[12px]">
                <label className="flex items-center gap-1 text-foreground">
                  <span className="text-muted-foreground">编号样式:</span>
                  <select
                    aria-label="编号样式"
                    value={basketOptions.numbering}
                    onChange={(e) =>
                      updateBasketOption(
                        "numbering",
                        e.target.value as BasketOptions["numbering"],
                      )
                    }
                    className="h-7 rounded border border-input bg-background px-1.5 text-[12px] text-foreground"
                  >
                    <option value="bracket">[1]</option>
                    <option value="arabic">1.</option>
                    <option value="none">无</option>
                  </select>
                </label>

                <label className="flex cursor-pointer items-center gap-1 text-foreground">
                  <input
                    type="checkbox"
                    checked={basketOptions.group_by_kind}
                    onChange={(e) =>
                      updateBasketOption("group_by_kind", e.target.checked)
                    }
                    className="h-3.5 w-3.5 rounded border-border"
                  />
                  按类别分组
                </label>

                <label className="flex cursor-pointer items-center gap-1 text-foreground">
                  <input
                    type="checkbox"
                    checked={
                      basketOptions.with_support || basketOptions.with_relation
                    }
                    onChange={(e) => {
                      const nextOpts: BasketOptions = {
                        ...basketOptions,
                        with_support: e.target.checked,
                        with_relation: e.target.checked,
                      };
                      setBasketOptions(nextOpts);
                      persistBasketToStorage(basketItems, nextOpts);
                    }}
                    className="h-3.5 w-3.5 rounded border-border"
                  />
                  附带支撑/关系说明
                </label>

                <label className="flex cursor-pointer items-center gap-1 text-foreground">
                  <input
                    type="checkbox"
                    checked={basketOptions.include_signatures}
                    onChange={(e) =>
                      updateBasketOption("include_signatures", e.target.checked)
                    }
                    className="h-3.5 w-3.5 rounded border-border"
                  />
                  Word 附带成员签名表
                </label>
              </div>

              {/* Right: 3 Action Buttons */}
              <div className="flex flex-wrap items-center gap-2">
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  className="h-8 px-3 text-[12px]"
                  disabled={
                    basketItems.length === 0 || basketActionLoading !== null
                  }
                  onClick={() => void handleBasketBatchCopy()}
                >
                  {basketActionLoading === "copy"
                    ? "正在复制…"
                    : t.batchCopyBasketBtn}
                </Button>
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  className="h-8 px-3 text-[12px]"
                  disabled={
                    basketCounts.person === 0 || basketActionLoading !== null
                  }
                  title={
                    basketCounts.person === 0
                      ? "素材篮中没有成员"
                      : `打包下载所选 ${basketCounts.person} 名成员的电子签名 ZIP`
                  }
                  onClick={() => void handleBasketDownloadSignaturesZip()}
                >
                  {basketActionLoading === "zip"
                    ? "正在打包…"
                    : t.downloadSignaturesZipBtn}
                </Button>
                <Button
                  type="button"
                  size="sm"
                  className="h-8 px-3 text-[12px]"
                  disabled={
                    basketItems.length === 0 || basketActionLoading !== null
                  }
                  onClick={() => void handleBasketExportSelectedDocx()}
                >
                  {basketActionLoading === "docx"
                    ? "正在导出…"
                    : t.exportSelectedDocxBtn}
                </Button>
              </div>
            </div>
          </div>
        </div>

        {/* Clipboard Fallback Dialog (§6.4) */}
        <Dialog
          open={clipboardFallbackText !== null}
          onOpenChange={(next) => {
            if (!next) setClipboardFallbackText(null);
          }}
        >
          <DialogContent className="max-w-xl">
            <DialogHeader>
              <DialogTitle>手动复制格式文本（请按 Ctrl/⌘+C）</DialogTitle>
              <DialogDescription>
                当前浏览器环境未授予剪贴板直接写入权限，已为您全选以下文本，请按
                Ctrl+C 或 ⌘+C 复制。
              </DialogDescription>
            </DialogHeader>
            <textarea
              readOnly
              rows={10}
              value={clipboardFallbackText || ""}
              onFocus={(e) => e.currentTarget.select()}
              className="w-full rounded-md border border-input bg-muted/30 p-3 font-mono text-[12px] text-foreground"
            />
            <DialogFooter>
              <Button
                type="button"
                onClick={() => setClipboardFallbackText(null)}
              >
                完成并关闭
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
    </>
  );
}
