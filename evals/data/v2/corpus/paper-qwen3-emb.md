---
key: paper-qwen3-emb
title: 论文笔记：Qwen3-Embedding 多语言指令与套娃表征
collection: paper_note
visibility: lab
---

本文由陈予安整理，总结了 Qwen3-Embedding 系列模型在非对称检索指令模板与套娃表征学习（MRL）上的关键特性。

## 非对称指令模板

Qwen3-Embedding-8B 采用非对称编码设计：查询侧（embed_query）拼接英文任务指令模板“Instruct: {instruction}\nQuery:{query}”，而文档侧（embed_documents）直接输入原文不加任何前缀。多语言实验证实中文查询配英文 instruction 的检索效果优于中文 instruction。

## 套娃维度截断特性

得益于套娃表征训练（Matryoshka Representation Learning），模型原生 4096 维输出可直接截取前 2048 维或前 1024 维并重新做 L2 归一化，在维度减半甚至降至四分之一时仍能保留 97% 以上的检索召回率。

## 本地部署实测体会

在 Mac Studio（Apple Silicon 统一内存）上通过 Ollama 运行 qwen3-embedding:8b，单条中文查询嵌入耗时约 25 至 45 毫秒，完全满足实验室内部知识库的实时交互需求。
