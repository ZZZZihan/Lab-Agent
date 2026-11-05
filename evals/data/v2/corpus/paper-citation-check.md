---
key: paper-citation-check
title: 论文笔记：细粒度归因校验与幻觉句子拦截
collection: paper_note
visibility: lab
---

本文由宋予涵整理，总结生成式问答中句级引用对齐（Citation Alignment）与两阶段裁判评估方法。

## 引用F1与边际裁剪

检索阶段通过 cite_margin 与 cite_k 规则从排序候选中挑选核心证据集：只保留得分不低于最高分减去 cite_margin 且不低于 score_min 的前 cite_k 个分块，避免把无关尾部候选塞入上下文。

## 生成侧标签清洗

模型生成的正文必须以 [1]、[2] 等角标显式指向候选证据；后处理函数会解析所有角标，计算实际被引用分块的精确率与召回率，并检测完全未携带引用的裸句比例（uncited_rate）。

## 独立厂商裁判协议

当使用大模型评估每句话是否被其所引用的分块支持时，为消除同族模型自偏置（self-preference bias），裁判模型必须选用与被测模型不同厂商的模型，并辅以不少于 30 条人工盲审样本核对一致率。
