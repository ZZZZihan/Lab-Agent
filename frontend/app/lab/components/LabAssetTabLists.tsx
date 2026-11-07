"use client";


import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { cn } from "cn";

import {
  normalizeNameKey,
} from "./labTypesAndHelpers";
import type { LabPageContext } from "../page";

export function LabAssetTabLists({ ctx }: { ctx: LabPageContext }) {
  const {
    loading,
    activeTab,
    copiedId,
    expandedBioId,
    setExpandedBioId,
    t,
    fetchDossierById,
    copyToClipboard,
    personByNormalizedName,
    filteredPeople,
    filteredProjects,
    filteredAchievements,
    filteredFacilities,
    currentTabBasketCandidates,
    isItemSelected,
    handleRowCheckboxClick,
    getProjectText,
    getAchievementText,
    handleDownloadPersonSignature,
    canEditEntity,
    openEditModal,
    renderEntitySourceBadges,
    renderCardProvenanceSection,
  } = ctx;

  return (
    <>
        {loading ? (
          <p className="text-[13px] text-muted-foreground">{t.loading}</p>
        ) : activeTab === "people" ? (
          <div className="grid gap-3">
            {filteredPeople.length === 0 ? (
              <p className="text-[13px] text-muted-foreground">
                暂无匹配的团队成员记录。
              </p>
            ) : (
              filteredPeople.map((person, idx) => {
                const checked = isItemSelected("person", person.id);
                const hasSig = Boolean(person.has_signature);
                return (
                  <div
                    key={person.id}
                    className={cn(
                      "rounded-lg border bg-card p-4 transition-colors hover:bg-muted/10",
                      checked
                        ? "border-foreground/40 bg-muted/5"
                        : "border-border",
                    )}
                  >
                    <div className="flex flex-wrap items-start justify-between gap-3">
                      <div className="flex items-start gap-2.5">
                        <input
                          type="checkbox"
                          aria-label={`选择成员 ${person.name}`}
                          checked={checked}
                          onChange={(e) => {
                            const ev = e.nativeEvent as MouseEvent;
                            handleRowCheckboxClick(
                              "people",
                              idx,
                              currentTabBasketCandidates,
                              e.target.checked,
                              Boolean(ev?.shiftKey),
                            );
                          }}
                          className="mt-1 h-4 w-4 rounded border-border"
                        />
                        <div>
                          <div className="flex flex-wrap items-center gap-2">
                            <button
                              type="button"
                              onClick={() => void fetchDossierById(person.id)}
                              className="text-left font-medium text-[15px] text-foreground underline-offset-4 hover:underline"
                              title="点击打开个人全息档案袋"
                            >
                              {person.name}
                            </button>
                            <Badge
                              variant={
                                person.project_role === "applicant"
                                  ? "secondary"
                                  : "outline"
                              }
                            >
                              {person.project_role === "applicant"
                                ? "课题负责人"
                                : "项目组成员"}
                            </Badge>
                            {hasSig ? (
                              <Badge
                                variant="secondary"
                                className="border-emerald-500/40 bg-emerald-500/15 text-[11px] font-normal text-emerald-800 dark:text-emerald-300"
                              >
                                {t.signatureExtractedBadge}
                              </Badge>
                            ) : (
                              <Badge
                                variant="outline"
                                className="border-border bg-muted/20 text-[11px] font-normal text-muted-foreground"
                              >
                                {t.signatureMissingBadge}
                              </Badge>
                            )}
                            {person.degree ? (
                              <Badge variant="outline">{person.degree}</Badge>
                            ) : null}
                            {person.title && person.title !== "无" ? (
                              <Badge variant="outline">{person.title}</Badge>
                            ) : null}
                            {person.tutor_role && person.tutor_role !== "无" ? (
                              <Badge variant="outline">
                                {person.tutor_role}
                              </Badge>
                            ) : null}
                            {renderEntitySourceBadges(
                              person.as_of_time,
                              person.provenance,
                            )}
                          </div>
                          <p className="mt-1 text-[13px] text-muted-foreground">
                            {[
                              person.affiliation,
                              person.admin_duty && person.admin_duty !== "无"
                                ? person.admin_duty
                                : null,
                              person.research_specialty
                                ? `研究专长：${person.research_specialty}`
                                : null,
                            ]
                              .filter(Boolean)
                              .join(" · ")}
                          </p>
                          <p className="mt-1.5 font-serif text-[13px] text-foreground/90">
                            {person.application_text}
                          </p>
                          {renderCardProvenanceSection("people", person)}
                          {person.bio ? (
                            <div className="mt-2">
                              <button
                                type="button"
                                className="text-[12px] font-medium text-muted-foreground underline hover:text-foreground"
                                onClick={() =>
                                  setExpandedBioId((prev) =>
                                    prev === person.id ? null : person.id,
                                  )
                                }
                              >
                                {expandedBioId === person.id
                                  ? "收起学术简历"
                                  : "展开学术简历"}
                              </button>
                              {expandedBioId === person.id ? (
                                <p className="mt-1.5 rounded-md bg-muted/40 p-2.5 font-serif text-[13px] leading-[1.6] text-foreground">
                                  {person.bio}
                                </p>
                              ) : null}
                            </div>
                          ) : null}
                        </div>
                      </div>
                      <div className="flex flex-wrap gap-1.5">
                        <Button
                          size="sm"
                          variant="secondary"
                          className="h-7 px-2.5 text-[12px]"
                          onClick={() => void fetchDossierById(person.id)}
                        >
                          {t.dossierTitle}
                        </Button>
                        <Button
                          size="sm"
                          variant="outline"
                          className="h-7 px-2.5 text-[12px]"
                          disabled={!hasSig}
                          title={
                            hasSig
                              ? `下载 ${person.name} 的电子签名`
                              : "该成员尚未上传或提取电子签名"
                          }
                          onClick={() =>
                            void handleDownloadPersonSignature(
                              person.id,
                              person.name,
                              person.signature_mime,
                              person.signature_sha256,
                            )
                          }
                        >
                          {t.downloadSignatureBtn}
                        </Button>
                        {canEditEntity("people", person) ? (
                          <Button
                            size="sm"
                            variant="outline"
                            className="h-7 px-2.5 text-[12px]"
                            onClick={() => openEditModal("people", person)}
                          >
                            {t.editAssetBtn}
                          </Button>
                        ) : null}
                        <Button
                          size="sm"
                          variant="outline"
                          className="h-7 px-2.5 text-[12px]"
                          onClick={() =>
                            void copyToClipboard(
                              person.application_text,
                              person.id,
                            )
                          }
                        >
                          {copiedId === person.id
                            ? t.copiedGrantFormat
                            : t.copyGrantFormat}
                        </Button>
                        {person.bio ? (
                          <Button
                            size="sm"
                            variant="outline"
                            className="h-7 px-2.5 text-[12px]"
                            onClick={() =>
                              void copyToClipboard(
                                person.application_text_with_bio,
                                `${person.id}-bio`,
                              )
                            }
                          >
                            {copiedId === `${person.id}-bio`
                              ? t.copiedGrantFormat
                              : "复制学术简历"}
                          </Button>
                        ) : null}
                      </div>
                    </div>
                  </div>
                );
              })
            )}
          </div>
        ) : activeTab === "projects" ? (
          <div className="grid gap-3">
            {filteredProjects.length === 0 ? (
              <p className="text-[13px] text-muted-foreground">
                暂无匹配的承担项目记录。
              </p>
            ) : (
              filteredProjects.map((proj, idx) => {
                const displayAppText = getProjectText(proj);
                const checked = isItemSelected("project", proj.id);
                const matchedPi = proj.pi_name
                  ? personByNormalizedName.get(normalizeNameKey(proj.pi_name))
                  : undefined;
                return (
                  <div
                    key={proj.id}
                    className={cn(
                      "rounded-lg border bg-card p-4 transition-colors hover:bg-muted/10",
                      checked
                        ? "border-foreground/40 bg-muted/5"
                        : "border-border",
                    )}
                  >
                    <div className="flex flex-wrap items-start justify-between gap-3">
                      <div className="flex items-start gap-2.5">
                        <input
                          type="checkbox"
                          aria-label={`选择项目 ${proj.title}`}
                          checked={checked}
                          onChange={(e) => {
                            const ev = e.nativeEvent as MouseEvent;
                            handleRowCheckboxClick(
                              "projects",
                              idx,
                              currentTabBasketCandidates,
                              e.target.checked,
                              Boolean(ev?.shiftKey),
                            );
                          }}
                          className="mt-1 h-4 w-4 rounded border-border"
                        />
                        <div>
                          <div className="flex flex-wrap items-center gap-2">
                            <span className="font-medium text-[15px] text-foreground">
                              《{proj.title}》
                            </span>
                            {proj.grant_no ? (
                              <Badge variant="outline" className="font-mono">
                                {proj.grant_no}
                              </Badge>
                            ) : null}
                            <Badge
                              variant={
                                proj.status === "ongoing"
                                  ? "secondary"
                                  : "outline"
                              }
                            >
                              {proj.status_label}
                              {proj.status_source === "inferred"
                                ? "（按基准期推断）"
                                : ""}
                            </Badge>
                            {renderEntitySourceBadges(
                              proj.as_of_time,
                              proj.provenance,
                            )}
                          </div>
                          <div className="mt-1 flex flex-wrap items-center gap-1.5 text-[13px] text-muted-foreground">
                            {proj.pi_name ? (
                              matchedPi ? (
                                <span>
                                  负责人：
                                  <button
                                    type="button"
                                    className="font-medium text-foreground underline underline-offset-2 hover:text-primary"
                                    onClick={() =>
                                      void fetchDossierById(matchedPi.id)
                                    }
                                  >
                                    {proj.pi_name}
                                  </button>
                                </span>
                              ) : (
                                <span>负责人：{proj.pi_name}</span>
                              )
                            ) : null}
                            {proj.funder ? <span>· {proj.funder}</span> : null}
                            {proj.start_date ? (
                              <span>· 立项：{proj.start_date.slice(0, 7)}</span>
                            ) : null}
                          </div>
                          <p className="mt-1.5 whitespace-pre-wrap font-serif text-[13px] text-foreground/90">
                            {displayAppText}
                          </p>
                          {renderCardProvenanceSection("projects", proj)}
                        </div>
                      </div>
                      <div className="flex flex-wrap gap-1.5">
                        {canEditEntity("projects", proj) ? (
                          <Button
                            size="sm"
                            variant="outline"
                            className="h-7 px-2.5 text-[12px]"
                            onClick={() => openEditModal("projects", proj)}
                          >
                            {t.editAssetBtn}
                          </Button>
                        ) : null}
                        <Button
                          size="sm"
                          variant="outline"
                          className="h-7 px-2.5 text-[12px]"
                          onClick={() =>
                            void copyToClipboard(displayAppText, proj.id)
                          }
                        >
                          {copiedId === proj.id
                            ? t.copiedGrantFormat
                            : t.copyGrantFormat}
                        </Button>
                      </div>
                    </div>
                  </div>
                );
              })
            )}
          </div>
        ) : activeTab === "achievements" ? (
          <div className="grid gap-3">
            {filteredAchievements.length === 0 ? (
              <p className="text-[13px] text-muted-foreground">
                暂无匹配的前期成果记录。
              </p>
            ) : (
              filteredAchievements.map((ach, idx) => {
                const displayAchText = getAchievementText(ach);
                const checked = isItemSelected("achievement", ach.id);
                const matchedAuthor = ach.first_author
                  ? personByNormalizedName.get(
                      normalizeNameKey(ach.first_author),
                    )
                  : undefined;
                return (
                  <div
                    key={ach.id}
                    className={cn(
                      "rounded-lg border bg-card p-4 transition-colors hover:bg-muted/10",
                      checked
                        ? "border-foreground/40 bg-muted/5"
                        : "border-border",
                    )}
                  >
                    <div className="flex flex-wrap items-start justify-between gap-3">
                      <div className="flex items-start gap-2.5">
                        <input
                          type="checkbox"
                          aria-label={`选择成果 ${ach.title}`}
                          checked={checked}
                          onChange={(e) => {
                            const ev = e.nativeEvent as MouseEvent;
                            handleRowCheckboxClick(
                              "achievements",
                              idx,
                              currentTabBasketCandidates,
                              e.target.checked,
                              Boolean(ev?.shiftKey),
                            );
                          }}
                          className="mt-1 h-4 w-4 rounded border-border"
                        />
                        <div>
                          <div className="flex flex-wrap items-center gap-2">
                            <Badge variant="secondary">{ach.kind_label}</Badge>
                            <span className="font-medium text-[15px] text-foreground">
                              《{ach.title}》
                            </span>
                            {ach.year ? (
                              <Badge variant="outline" className="font-mono">
                                {ach.year}
                              </Badge>
                            ) : null}
                            {renderEntitySourceBadges(
                              ach.as_of_time,
                              ach.provenance,
                            )}
                          </div>
                          <div className="mt-1 flex flex-wrap items-center gap-1.5 text-[13px] text-muted-foreground">
                            {ach.first_author ? (
                              matchedAuthor ? (
                                <span>
                                  第一作者：
                                  <button
                                    type="button"
                                    className="font-medium text-foreground underline underline-offset-2 hover:text-primary"
                                    onClick={() =>
                                      void fetchDossierById(matchedAuthor.id)
                                    }
                                  >
                                    {ach.first_author}
                                  </button>
                                </span>
                              ) : (
                                <span>第一作者：{ach.first_author}</span>
                              )
                            ) : null}
                            {ach.venue ? <span>· {ach.venue}</span> : null}
                          </div>
                          <p className="mt-1.5 font-serif text-[13px] text-foreground/90">
                            {displayAchText}
                          </p>
                          {renderCardProvenanceSection("achievements", ach)}
                        </div>
                      </div>
                      <div className="flex flex-wrap gap-1.5">
                        {canEditEntity("achievements", ach) ? (
                          <Button
                            size="sm"
                            variant="outline"
                            className="h-7 px-2.5 text-[12px]"
                            onClick={() => openEditModal("achievements", ach)}
                          >
                            {t.editAssetBtn}
                          </Button>
                        ) : null}
                        <Button
                          size="sm"
                          variant="outline"
                          className="h-7 px-2.5 text-[12px]"
                          onClick={() =>
                            void copyToClipboard(displayAchText, ach.id)
                          }
                        >
                          {copiedId === ach.id
                            ? t.copiedGrantFormat
                            : t.copyGrantFormat}
                        </Button>
                      </div>
                    </div>
                  </div>
                );
              })
            )}
          </div>
        ) : (
          <div className="grid gap-3">
            {filteredFacilities.length === 0 ? (
              <p className="text-[13px] text-muted-foreground">
                暂无匹配的设备与支撑条件记录。
              </p>
            ) : (
              filteredFacilities.map((fac, idx) => {
                const checked = isItemSelected("facility", fac.id);
                return (
                  <div
                    key={fac.id}
                    className={cn(
                      "rounded-lg border bg-card p-4 transition-colors hover:bg-muted/10",
                      checked
                        ? "border-foreground/40 bg-muted/5"
                        : "border-border",
                    )}
                  >
                    <div className="flex flex-wrap items-start justify-between gap-3">
                      <div className="flex items-start gap-2.5">
                        <input
                          type="checkbox"
                          aria-label={`选择设备 ${fac.name}`}
                          checked={checked}
                          onChange={(e) => {
                            const ev = e.nativeEvent as MouseEvent;
                            handleRowCheckboxClick(
                              "facilities",
                              idx,
                              currentTabBasketCandidates,
                              e.target.checked,
                              Boolean(ev?.shiftKey),
                            );
                          }}
                          className="mt-1 h-4 w-4 rounded border-border"
                        />
                        <div>
                          <div className="flex flex-wrap items-center gap-2">
                            <Badge variant="secondary">
                              {fac.category === "compute"
                                ? "算力设备"
                                : "科研设备"}
                            </Badge>
                            <span className="font-medium text-[15px] text-foreground">
                              {fac.name}
                            </span>
                            {fac.value_wan ? (
                              <Badge variant="outline" className="font-mono">
                                {fac.value_wan} 万元
                              </Badge>
                            ) : null}
                            {renderEntitySourceBadges(
                              fac.as_of_time,
                              fac.provenance,
                            )}
                          </div>
                          <p className="mt-1.5 font-serif text-[13px] text-foreground/90">
                            {fac.application_text}
                          </p>
                          {renderCardProvenanceSection("facilities", fac)}
                        </div>
                      </div>
                      <div className="flex flex-wrap gap-1.5">
                        {canEditEntity("facilities", fac) ? (
                          <Button
                            size="sm"
                            variant="outline"
                            className="h-7 px-2.5 text-[12px]"
                            onClick={() => openEditModal("facilities", fac)}
                          >
                            {t.editAssetBtn}
                          </Button>
                        ) : null}
                        <Button
                          size="sm"
                          variant="outline"
                          className="h-7 px-2.5 text-[12px]"
                          onClick={() =>
                            void copyToClipboard(fac.application_text, fac.id)
                          }
                        >
                          {copiedId === fac.id
                            ? t.copiedGrantFormat
                            : t.copyGrantFormat}
                        </Button>
                      </div>
                    </div>
                  </div>
                );
              })
            )}
          </div>
        )}
    </>
  );
}
