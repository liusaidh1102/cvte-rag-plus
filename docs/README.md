<div align="center">
  <h1>CVTE-RAG-Plus</h1>
  <p>面向企业私有化场景的 RAG 知识库与 AI 写作平台</p>
  <img src="https://img.shields.io/badge/version-0.6.0-blue" alt="version">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-AGPL3.0-green" alt="license"></a>
</div>

---

## 简介

CVTE-RAG-Plus 是一个面向企业、高校等私有化部署场景的企业级 RAG 知识库平台。

项目覆盖“文档入库—知识检索—图文问答—AI 写作”完整链路，重点解决复杂文档解析、图文信息丢失、团队权限隔离和写作成果交付等问题。

## 核心能力

- 管理后台与团队权限
- MinerU 文档解析与 OCR
- 图文问答与图像资产管理
- AI 文档撰写与 Word 导出
- 跨语言检索
- 大文件分片上传与批量解析调度
- Docker、本地源码、GPU 和离线部署

## 文档导航

- [快速开始](quickstart/README.md)
- [本地 / Docker 双模式启动](local_dev.md)
- [管理后台认证配置](security/jwt-secret.md)
- [管理 API 权限说明](security/management-api.md)
- [大文件分片上传](multipart_upload.md)
- [常见问题](question/README.md)

## 声明

本项目为独立开发项目，不代表 CVTE 官方立场，也不隶属于 CVTE。
