---
key: project-aurora
title: 曙光项目：多模态表格理解与结构化抽取
collection: project
visibility: lab
---

曙光项目（Project Aurora）致力于解决科学文献与工程手表中复杂跨页表格的结构还原与数值问答。

## 项目代号与负责人

曙光项目的内部立项代号为 AU-204，由博士生宋予涵担任负责人，指导老师为沈昭，代码仓库位于 git.qingheng.lab/aurora。

## 核心数据集与规模

项目构建了中文科学图表基准 SciTab-CN，包含 18500 张高清扫描表格图像与对应的双层 HTML/LaTeX 结构化标注，覆盖化学材料、芯片手册与气象统计三个子域。

## 阶段精度指标

在十月基线评测中，曙光模型在跨页合并单元格场景下的树编辑距离相似度（TEDS）达到 94.6%，数值单元格精确匹配率（Exact Match）达到 91.2%。
