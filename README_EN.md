<div align="center">
  <h1>CVTE-RAG-Plus</h1>
  <p><strong>An enterprise RAG knowledge base and AI writing platform for private deployments</strong></p>
  <p>
    <img src="https://img.shields.io/badge/version-0.6.0-blue" alt="version">
    <a href="LICENSE"><img src="https://img.shields.io/badge/license-AGPL3.0-green" alt="license"></a>
  </p>
  <p>
    <a href="README.md">中文</a> | <a href="README_EN.md">English</a>
  </p>
</div>

---

## Overview

CVTE-RAG-Plus is an enterprise RAG knowledge platform designed for private deployments in enterprises and universities.

It covers the full workflow from document ingestion and knowledge retrieval to image-grounded question answering and AI-assisted writing, with a focus on complex document parsing, visual information loss, team permission isolation, and deliverable writing results.

## Highlights

- **Admin console and team permissions**: manage users, teams, configuration, files, and knowledge bases with administrator and team-owner roles.
- **MinerU document parsing**: supports PDF, Word, PPT, Excel, CSV, TXT, Markdown, and images, with automatic OCR/text-mode selection.
- **Image-grounded QA**: images are extracted during parsing, linked to text chunks, and shown alongside citations.
- **Image asset management**: browse, search, preview, attach, replace, or remove images associated with chunks.
- **AI document writing**: built-in templates, streaming generation, cursor-aware insertion, image insertion, and Word export.
- **Cross-language retrieval**: translate Chinese queries into English before searching English knowledge bases.
- **Reliable ingestion**: chunked uploads, resumable uploads, batch parsing, real-time progress, and retry support.
- **Flexible deployment**: Docker, local source, GPU, and offline image deployment.

## Quick Start

Before the first start, copy the configuration template and set a non-default `MANAGEMENT_ADMIN_PASSWORD` (at least 12 characters):

```powershell
Copy-Item docker/.env.example docker/.env
```

See [management authentication](docs/security/jwt-secret.md) for details.

```powershell
# Start infrastructure only; run applications locally
.\scripts\start.ps1 infra

# Run everything in Docker
.\scripts\start.ps1 docker -Build

# Start infrastructure in Docker and applications locally
.\scripts\start.ps1 local
```

See [local / Docker startup](docs/local_dev.md) for prerequisites, ports, and manual commands.

## Documentation

- [Quick start](docs/quickstart/README.md)
- [Local / Docker startup](docs/local_dev.md)
- [Management authentication](docs/security/jwt-secret.md)
- [Management API permissions](docs/security/management-api.md)
- [Chunked upload](docs/multipart_upload.md)
- [FAQ](docs/question/README.md)

## Contributing

1. Fork this repository.
2. Create a branch: `git checkout -b feature/your-feature`
3. Commit your changes: `git commit -m "feat: describe your change"`
4. Push the branch: `git push origin feature/your-feature`
5. Open a pull request.

## Acknowledgements

- [v3-admin-vite](https://github.com/un-pany/v3-admin-vite)
- [MinerU](https://github.com/opendatalab/MinerU)

## License

This project is licensed under [AGPLv3](LICENSE). Derivative works must remain AGPLv3 and publish their source code. If the software is provided as a network service, users must be able to obtain the corresponding source code.

## Disclaimer

This is an independent project. It does not represent the official position of CVTE and is not affiliated with CVTE.
