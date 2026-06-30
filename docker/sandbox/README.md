# 沙箱镜像 - Hermes Sandbox Docker Image

## 构建

```bash
docker build -t hermes-sandbox:latest docker/sandbox/
```

## 验证（禁网检查）

构建完毕后，在服务器上执行以下命令，确认所有依赖在 `--network=none` 隔离模式下可用：

```bash
docker run --rm --network=none hermes-sandbox:latest \
  python3 -c "import pandas, docx, pptx, reportlab, matplotlib, pdfplumber, PyPDF2, PIL; print('deps ok')"
```

预期输出：`deps ok`

## 配置

构建成功后，将镜像名称写入网关配置文件 `config.yaml` 的多租户沙箱配置项：

```yaml
security:
  multi_tenant:
    sandbox:
      image: hermes-sandbox:latest
```

## 说明

该镜像基于 `nikolaik/python-nodejs:python3.11-nodejs20`，预装了以下依赖：

- **数据处理**: pandas, numpy, openpyxl
- **文档生成**: python-docx, python-pptx, reportlab
- **PDF 处理**: pdfplumber, PyPDF2
- **图像处理**: Pillow, matplotlib
- **文档格式转换**: LibreOffice（headless 模式）

所有依赖均预装在镜像内，因为沙箱容器运行时网络隔离（`--network=none`），无法在运行时动态安装。
