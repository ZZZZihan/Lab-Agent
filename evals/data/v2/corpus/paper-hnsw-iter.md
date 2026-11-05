---
key: paper-hnsw-iter
title: 论文笔记：pgvector 过滤检索与迭代式图扫描
collection: paper_note
visibility: lab
---

本文由赵知行撰写，分析 pgvector 0.8.0 引入的迭代式 HNSW 图扫描（iterative scan）如何解决带 WHERE 过滤条件时的召回塌缩问题。

## 传统后过滤缺陷

在没有迭代式扫描的早期向量数据库中，HNSW 索引只返回全局最近的 ef_search 个候选再做 SQL WHERE 过滤；当某用户仅有权访问全库 5% 的受限文档时，固定候选池极易被无权文档占满而导致返回结果为空。

## 迭代式扫描机制

pgvector 0.8.0 支持设置 SET LOCAL hnsw.iterative_scan = relaxed_order，当首批图搜索命中的满足 ACL WHERE 条件的记录不足 LIMIT 时，引擎会自动继续沿图向外扩展搜索直至凑齐所需条数或达到扫描上限。

## 前缀表达式索引

结合 pgvector 0.7+ 的 subvector 函数，可对 4096 维 halfvec 列的前 1024 维建立 HNSW 表达式索引做粗召回，再在外层 SQL 用完整 4096 维精确重排，兼顾百万级规模的索引速度与全维精度。
