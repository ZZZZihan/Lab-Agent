---
key: manual-ceph-storage
title: 清衡 Ceph 分布式对象存储操作指南
collection: knowledge
visibility: lab
---

本手册指导实验室成员使用内网 Ceph 集群（代号 QH-Ceph-Reef）进行大规模训练语料与模型权重的对象存储读写。

## 架构与容量规格

QH-Ceph-Reef 集群由 6 台存储服务器组成，共挂载 48 块企业级 NVMe 固态盘，裸容量总计 320 TB，采用三副本冗余策略（size=3, min_size=2），折合可用安全容量约 100 TB。

## S3 网关端点与鉴权

集群通过 RadosGW 对内网提供兼容 S3 协议的访问入口，服务端点为 http://s3.qingheng.lab:7480。每个项目组分配独立的 Bucket 与只读/读写密钥对，密钥必须保存在宿主机 ~/.config/ceph/ 目录下（权限 600）。

## 分片上传与带宽限制

体积超过 5 GB 的单个权重文件必须启用 S3 Multipart Upload（推荐分片大小 128 MB）。为防止拉取数据打满存储网，单个客户端 IP 的并发下载带宽上限设置为 2.5 GB/s。
