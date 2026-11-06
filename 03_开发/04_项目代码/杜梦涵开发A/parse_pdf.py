import fitz  # PyMuPDF
import re
import json


def extract_knowledge(pdf_path, output_json):
    doc = fitz.open(pdf_path)
    knowledge_blocks = []

    # 用于临时存放当前正在读取的 QM 块
    current_qm = None
    current_text = []
    current_page = None

    # 正则匹配 QM-D001, QM-P008 等
    qm_pattern = re.compile(r'(QM-[DPS]\d{3})')

    for page_num in range(len(doc)):
        page = doc[page_num]
        text = page.get_text("text")

        # 按行处理，寻找 QM 标识
        lines = text.split('\n')
        for line in lines:
            match = qm_pattern.search(line)
            if match:
                # 如果之前有正在收集的块，保存起来
                if current_qm:
                    knowledge_blocks.append({
                        "qm": current_qm,
                        "page": current_page,
                        "text": "\n".join(current_text).strip()
                    })

                # 开始新的块
                current_qm = match.group(1)
                current_page = page_num + 1  # 页码从1开始
                current_text = [line]
            else:
                if current_qm:
                    current_text.append(line)

    # 保存最后一个块
    if current_qm:
        knowledge_blocks.append({
            "qm": current_qm,
            "page": current_page,
            "text": "\n".join(current_text).strip()
        })

    # 保存为 JSON 文件
    with open(output_json, 'w', encoding='utf-8') as f:
        json.dump(knowledge_blocks, f, ensure_ascii=False, indent=2)

    print(f"解析完成！共提取 {len(knowledge_blocks)} 个知识条目，已保存至 {output_json}")


if __name__ == "__main__":
    # 请确保 PDF 文件在同一目录下
    extract_knowledge("栖木家居_客服知识资料_200页(2).pdf", "knowledge_base.json")