---
key: paper-rag-refusal
title: 论文笔记：知识库问答中的可信拒答与阈值校准
collection: paper_note
visibility: lab
---

本文由裴敬撰写，探讨企业级检索增强生成系统如何通过后验分位数校准避免在不可答题上产生幻觉回答。

## 误答与误拒权衡

RAG 系统的可信度取决于两项对抗指标：不可答题上的误答率（False Answer Rate）与可答题上的误拒率（False Refusal Rate）。未校准的系统若盲目回答低相似度证据，会在诱饵题与近邻不可答题上产生严重幻觉。

## 指纹绑定校准协议

由于不同嵌入模型、量化精度、截断维度甚至查询指令模板都会显著改变余弦相似度的绝对数值分布，阈值配置文件必须严格绑定嵌入器指纹（embedder_fingerprint），一旦指纹不匹配则自动回退至保守高阈值（0.99）故障闭合。

## 开发集与测试集隔离

所有阈值（evidence_score_min、rare_coverage_min、cite_margin、cite_k）只允许在 dev 划分上拟合；若调参函数检测到输入行包含 test 划分样本，必须立即抛出异常阻断运行。
