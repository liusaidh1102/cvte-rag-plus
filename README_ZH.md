<div align="center">
  <h1>CVTE-RAG-Plus</h1>
  <p><strong>面向企业私有化场景的 RAG 知识库与 AI 写作平台</strong></p>
  <p>
    <img src="https://img.shields.io/badge/version-0.6.0-blue" alt="version">
    <a href="LICENSE"><img src="https://img.shields.io/badge/license-AGPL3.0-green" alt="license"></a>
  </p>
  <p>
    <a href="README_ZH.md">中文</a> | <a href="README.md">English</a>
  </p>
</div>

---

## 项目简介

CVTE-RAG-Plus 是一个面向企业、高校等私有化部署场景的企业级 RAG 知识库平台。

项目围绕“文档入库—知识检索—图文问答—AI 写作”构建完整链路，重点解决复杂文档解析、图文信息丢失、团队权限混乱和写作成果难以交付等问题。

## 核心能力

- **管理后台与团队权限**：用户、团队、配置、文件、知识库统一管理，支持超级管理员和团队负责人两级权限。
- **MinerU 文档解析**：支持 PDF、Word、PPT、Excel、CSV、TXT、Markdown 和图片等格式，自动识别 OCR/文本模式。
- **图文问答**：解析阶段抽取图片并与文本块关联，回答引用时同步展示相关图片。
- **图像资产管理**：知识库图像集支持搜索、预览，文本块可手动关联、替换或移除图片。
- **AI 文档撰写**：内置多种模板，支持流式生成、光标位置插入、图片插入和 Word 导出。
- **跨语言检索**：中文问题可自动翻译后检索英文知识库，降低多语言资料使用门槛。
- **可靠文档入库**：支持大文件分块上传、断点续传、批量解析调度、实时进度和失败重试。
- **多种部署方式**：支持 Docker、本地源码、GPU 和离线镜像部署。

## 快速开始

首次启动前，先复制配置模板并设置非默认的 `MANAGEMENT_ADMIN_PASSWORD`（至少 12 个字符）：

```powershell
Copy-Item docker/.env.example docker/.env
```

详细说明见 [管理后台认证配置](docs/security/jwt-secret.md)。

```powershell
# 只启动基础设施，应用在本地终端启动
.\scripts\start.ps1 infra

# 全部使用 Docker
.\scripts\start.ps1 docker -Build

# 基础设施用 Docker，应用自动在本地启动
.\scripts\start.ps1 local
```

完整环境准备、端口和手工启动命令见 [本地 / Docker 双模式启动](docs/local_dev.md)。

## 项目文档

- [快速开始](docs/quickstart/README.md)
- [本地 / Docker 双模式启动](docs/local_dev.md)
- [管理后台认证配置](docs/security/jwt-secret.md)
- [管理 API 权限说明](docs/security/management-api.md)
- [大文件分片上传](docs/multipart_upload.md)
- [常见问题](docs/question/README.md)

## 贡献

1. Fork 本仓库
2. 创建分支：`git checkout -b feature/your-feature`
3. 提交改动：`git commit -m "feat: describe your change"`
4. 推送分支：`git push origin feature/your-feature`
5. 提交 Pull Request

## 鸣谢

本项目使用以下开源组件：

- [v3-admin-vite](https://github.com/un-pany/v3-admin-vite)
- [MinerU](https://github.com/opendatalab/MinerU)

## 许可证

本项目遵循 [AGPLv3](LICENSE) 许可证。任何衍生作品需继续使用 AGPLv3 并公开源代码；通过网络提供服务时，需向用户提供对应源码。

## 声明

本项目为独立开发项目，不代表 CVTE 官方立场，也不隶属于 CVTE。
