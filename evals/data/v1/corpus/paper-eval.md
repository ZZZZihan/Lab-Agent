---
key: paper-eval
title: 评测约定
collection: paper_note
visibility: lab
---

评测数字必须能在没有外网权重的环境里复现。

## 指标

实验室采用的指标是 Recall@5、MRR、引用准确率和拒答正确率。

## 门禁

实验室不使用 RAGAS 的 faithfulness 自动分数作为发布门禁，因为评审要求评测可复现，并且不依赖额外的模型调用。

## 数据划分

相似度阈值和拒答阈值只在开发集上调整。测试集只报告一次，不能根据测试结果回头改阈值。
