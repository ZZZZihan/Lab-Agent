---
key: project-orion
title: 猎户座项目：长文档分块重叠窗口评测摘要
collection: project
visibility: lab
---

猎户座项目（Project Orion，内部立项代号 OR-820）是清衡实验室针对无标题纯文本与多页 PDF 手册开展的切块策略专项研究，由博士生宋予涵与工程师赵知行联合负责，代码归档于 git.qingheng.lab/orion。

实验表明，针对不含二级标题的会议速记与设备巡检单，采用最大 800 字符窗口配合 80 字符句子边界重叠（chunk_plain），相比固定 500 字符硬切分，跨段事实问答的 Recall@5 提升了 6.8 个百分点，且分块数量仅增加 11%。项目结论已正式合入 Lab Agent 的默认切块器。
