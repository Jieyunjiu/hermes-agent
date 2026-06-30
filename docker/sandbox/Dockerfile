FROM nikolaik/python-nodejs:python3.11-nodejs20

# libreoffice 用于文档格式互转（headless）
RUN apt-get update && apt-get install -y --no-install-recommends \
        libreoffice \
    && rm -rf /var/lib/apt/lists/*

# 数据处理 / 文档生成 / PDF / 图像（默认禁网，必须预装齐全）
RUN pip install --no-cache-dir \
        pandas numpy openpyxl \
        python-docx python-pptx reportlab \
        matplotlib \
        pdfplumber PyPDF2 Pillow

WORKDIR /workspace
