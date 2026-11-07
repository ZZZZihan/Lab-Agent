"use client";


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

import {
  EDITABLE_FIELDS,
} from "./labTypesAndHelpers";
import type { LabPageContext } from "../page";

export function EntityEditDialog({ ctx }: { ctx: LabPageContext }) {
  const {
    me,
    editModalOpen,
    setEditModalOpen,
    editType,
    editItem,
    editForm,
    setEditForm,
    editSaving,
    setEditSaving,
    editError,
    restoringFieldKey,
    setRestoringFieldKey,
    t,
    editSessionSeqRef,
    handleSaveEdit,
    handleRestoreImported,
    renderFieldProvenanceBadge,
  } = ctx;

  return (
    <>
        {/* Phase 2 Online Edit & Field Provenance Restore Dialog */}
        <Dialog
          open={editModalOpen}
          onOpenChange={(nextOpen) => {
            if (!nextOpen) {
              editSessionSeqRef.current += 1;
              setEditSaving(false);
              setRestoringFieldKey(null);
            }
            setEditModalOpen(nextOpen);
          }}
        >
          <DialogContent className="max-w-2xl max-h-[88vh] overflow-y-auto">
            <DialogHeader>
              <DialogTitle>
                {t.editAssetBtn} · {t.fieldProvenanceTitle}
              </DialogTitle>
              <DialogDescription>
                修改保存后将自动生成人工溯源记录（优先于新旧申报书导入值）。带有人工覆盖的字段可随时一键「
                {t.restoreImportedBtn}」。
              </DialogDescription>
            </DialogHeader>

            {editError ? (
              <p role="alert" className="text-[13px] text-destructive">
                {editError}
              </p>
            ) : null}

            {editItem ? (
              <form
                onSubmit={handleSaveEdit}
                className="space-y-3.5 text-[13px]"
              >
                {EDITABLE_FIELDS[editType].map((fieldCfg) => {
                  if (fieldCfg.adminOnly && me?.role !== "admin") return null;
                  const prov = editItem.provenance?.[fieldCfg.key];
                  const opKey = `${editItem.id}:${fieldCfg.key}`;
                  return (
                    <div key={fieldCfg.key} className="space-y-1">
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <Label
                          htmlFor={`edit-field-${fieldCfg.key}`}
                          className="text-[12px] font-medium text-foreground"
                        >
                          {fieldCfg.label}{" "}
                          <span className="font-mono text-[11px] text-muted-foreground">
                            ({fieldCfg.key})
                          </span>
                        </Label>
                        <div className="flex flex-wrap items-center gap-1.5">
                          {renderFieldProvenanceBadge(prov)}
                          {prov?.has_manual_override ? (
                            <Button
                              type="button"
                              size="sm"
                              variant="outline"
                              className="h-6 px-2 text-[11px]"
                              disabled={restoringFieldKey !== null || editSaving}
                              onClick={() =>
                                handleRestoreImported(
                                  editType,
                                  editItem.id,
                                  fieldCfg.key,
                                )
                              }
                            >
                              {restoringFieldKey === opKey
                                ? t.loading
                                : t.restoreImportedBtn}
                            </Button>
                          ) : null}
                        </div>
                      </div>
                      {fieldCfg.type === "textarea" ? (
                        <textarea
                          id={`edit-field-${fieldCfg.key}`}
                          rows={3}
                          value={editForm[fieldCfg.key] ?? ""}
                          onChange={(e) =>
                            setEditForm((prev) => ({
                              ...prev,
                              [fieldCfg.key]: e.target.value,
                            }))
                          }
                          className="w-full rounded-md border border-input bg-background px-3 py-2 text-[13px] text-foreground"
                        />
                      ) : fieldCfg.type === "select" && fieldCfg.options ? (
                        <select
                          id={`edit-field-${fieldCfg.key}`}
                          value={editForm[fieldCfg.key] ?? ""}
                          onChange={(e) =>
                            setEditForm((prev) => ({
                              ...prev,
                              [fieldCfg.key]: e.target.value,
                            }))
                          }
                          className="h-9 w-full rounded-md border border-input bg-background px-3 text-[13px] text-foreground"
                        >
                          {fieldCfg.options.map((opt) => (
                            <option key={opt.value} value={opt.value}>
                              {opt.label}
                            </option>
                          ))}
                        </select>
                      ) : (
                        <Input
                          id={`edit-field-${fieldCfg.key}`}
                          type={fieldCfg.type || "text"}
                          value={editForm[fieldCfg.key] ?? ""}
                          onChange={(e) =>
                            setEditForm((prev) => ({
                              ...prev,
                              [fieldCfg.key]: e.target.value,
                            }))
                          }
                          className="h-9 text-[13px]"
                        />
                      )}
                    </div>
                  );
                })}

                <DialogFooter className="pt-2">
                  <Button
                    type="button"
                    variant="outline"
                    onClick={() => {
                      editSessionSeqRef.current += 1;
                      setEditSaving(false);
                      setRestoringFieldKey(null);
                      setEditModalOpen(false);
                    }}
                  >
                    {t.cancel}
                  </Button>
                  <Button
                    type="submit"
                    disabled={editSaving || restoringFieldKey !== null}
                  >
                    {editSaving ? t.saving : t.save}
                  </Button>
                </DialogFooter>
              </form>
            ) : null}
          </DialogContent>
        </Dialog>
    </>
  );
}
