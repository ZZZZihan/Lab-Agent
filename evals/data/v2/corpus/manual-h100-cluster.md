---
key: manual-h100-cluster
title: 清衡-H100 节点集群运维与作业提交手册
collection: knowledge
visibility: lab
---

清衡-H100 是实验室于 2026 年秋季新建的第二代高性能训练集群，与第一代清衡-A100 集群独立编排调度。

## 节点配置与互联

清衡-H100 集群共包含 12 台计算节点（主机名 h100-node01 至 h100-node12），每台节点配备 8 张 NVIDIA H100 SXM5 80GB 加速卡、双路 AMD EPYC 9654 处理器及 2 TB DDR5 内存。节点内通过 900 GB/s NVSwitch 全互联，节点间配备 400 Gbps InfiniBand 网卡。

## Slurm 队列与配额

作业调度采用 Slurm 24.05，登录入口为 h100-login.qingheng.lab。集群设有两个分区：train 分区单次作业最长运行 72 小时，不允许续约；debug 分区最多申请 2 张 H100 卡，单次上限 90 分钟。

## 容器镜像与挂载规范

所有多机训练作业必须通过 Pyxis 插件以 Enroot 容器方式启动，基础镜像须存放在 /nas/containers/ 目录。严禁在计算节点根分区（/tmp）写入超过 50 GB 的临时文件，高速暂存一律使用 /scratch/。
