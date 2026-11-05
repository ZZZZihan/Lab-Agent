---
key: manual-infiniband
title: RoCEv2 与 InfiniBand 高速互联交换机维护手册
collection: knowledge
visibility: lab
---

本手册记录清衡计算集群计算网（InfiniBand NDR）与存储网（100GbE RoCEv2）交换机的拓扑与故障排查方法。

## 网络拓扑与网段

清衡-H100 计算网采用 2 台 Quantum-2 QM9700 交换机组成无阻塞胖树拓扑，子网管理器（OpenSM）主备实例运行在 h100-node01 与 h100-node02 上；存储网采用 100GbE RoCEv2 交换机，使用独立内网网段 10.42.0.0/16。

## 无损流控参数

RoCEv2 存储网在优先级队列 3（Priority 3）上启用基于优先级的流量控制（PFC）与显式拥塞通知（ECN）：水线最小阈值设为 150 KB，最大阈值设为 1200 KB，最大传输单元（MTU）统一配置为 9000 字节巨型帧。

## 光模块误码告警

当端口符号误码率（Symbol Error Rate）超过 1e-12 或光功率低于 -9.5 dBm 时，网管系统自动触发端口隔离告警；更换 OSFP 光模块前必须使用光纤端面显微镜确认无灰尘污染。
