"""
PDF 文本抽取。
"""

import pypdfium2


def _extract_pdf_text(pdf_path, title):
    """从 PDF 文件中抽取文本内容。"""
    try:
        with open(pdf_path, "rb") as f:
            pdf_document = pypdfium2.PdfDocument(f, autoclose=True)
            text_parts = []
            for page in pdf_document:
                text_page = page.get_textpage()
                text_parts.append(text_page.get_text_range())
                text_page.close()
                page.close()
        text = "".join(text_parts)
        print(f"✅ 文本抽取成功: {title} ({len(text)} 字符)")
        return text
    except Exception as e:
        print(f"⚠️ 文本抽取失败 ({title}): {e}")
        return None
