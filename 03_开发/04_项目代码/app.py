# -*- coding: utf-8 -*-
"""
栖木家居客服知识库 —— 网站启动文件（修复版）
================================================
背景：上一版直接调用阿里云百炼 RAG，实测出现三方面问题（见《02_问题跟踪与复测》）：
  1) 实时/未知类问题（订单状态、库存、App 功能）仍被当作“有答案”，未提示无法确认；
  2) 答案复用少数固定话术、与问题错配；每条结果 QM 编号 4~17 个、top1 命中封面/目录页；
  3) 开发 A 解析好的 knowledge_base.json（219 条）完全未被使用，无上传/解析/索引管理界面。

本版改为以「本地结构化索引（knowledge_base.json）」为唯一检索来源：
  - 每条索引对应一个 QM 主题、真实页码、资料版本 2026-10，无文件路径泄露；
  - 检索为纯标准库实现（字符二元组 + 型号/意图加权），可离线、可复现、可核对；
  - 对资料无法覆盖的实时状态/未知能力做规则拦截，明确返回“无法确认”；
  - 对未指明型号的保温类问题先追问，多型号对比时分别给出各型号参数；
  - 补齐资料导入 / 解析 / 索引状态管理接口（AC-F04-01~04）。

运行方式（详见 README.md）：
    python app.py
    浏览器打开： http://127.0.0.1:8000
    索引管理页： http://127.0.0.1:8000/admin
"""

import json
import math
import os
import re
import ssl
import threading
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# 项目根目录（app.py 所在目录）
BASE_DIR = Path(__file__).resolve().parent
DEFAULT_KB_FILE = BASE_DIR / "knowledge_base.json"


# ---------------------------------------------------------------------------
# 配置加载：优先环境变量，其次同目录 .env 文件
# ---------------------------------------------------------------------------
def load_env_file():
    """简单读取 .env（KEY=VALUE，每行一条），不覆盖已存在的环境变量。"""
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_env_file()

# 运行配置
HOST = os.environ.get("HOST", "0.0.0.0")  # 作业要求：监听所有 IPv4 接口
PORT = int(os.environ.get("PORT", "8000"))
TOP_K = int(os.environ.get("TOP_K", "6"))
# 相关度低于该阈值时判定“资料可能未覆盖”，提示无法确认、不编造
LOW_CONFIDENCE_THRESHOLD = float(os.environ.get("LOW_CONFIDENCE_THRESHOLD", "0.20"))
# 用于统计“相关内容”条数的明确相关度下限（避免把弱相关条目计入展示数量）
RELEVANT_FLOOR = float(os.environ.get("RELEVANT_FLOOR", "0.6"))
# 问题长度上限（字符），超出拦截，避免超长输入与接口成本风险
MAX_QUESTION_LEN = int(os.environ.get("MAX_QUESTION_LEN", "500"))
# 资料版本（随每条结果展示，便于复核）
KB_VERSION = os.environ.get("KB_VERSION", "2026-10")


# ---------------------------------------------------------------------------
# 知识库解析
# ---------------------------------------------------------------------------
_QM_RE = re.compile(r"QM-[DPS]\d{3}")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
_ASCII_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9\-]*")

# 正文字段标记（用于切分字段 + 抽取答案）
FIELD_MARKERS = [
    "规格与价格", "构造与配件", "保温性能", "使用方法", "清洁与维护", "使用注意",
    "常见异常", "常见问题", "处理说明", "核对与限制", "操作步骤", "适用边界",
    "客服答复示例", "处理指引", "客户情况", "处理要点", "适用原则", "核实事项", "处理与交接",
]

FOOTER_MARK = "栖木家居｜客服知识库"

MODELS = ("GB-400", "BX-500", "BH-1000")

# 中文常见叫法 -> 型号（仅保留“唯一对应”的别名；“保温杯”按测试要求视为未指明型号，需先追问）
UNAMBIGUOUS_ALIAS = {
    "玻璃杯": "GB-400",
    "玻璃水杯": "GB-400",
    "保温壶": "BH-1000",
    "按压出水保温壶": "BH-1000",
}


def _full_to_half(text):
    """全角字符转半角（字母/数字/符号），避免全角影响匹配。"""
    out = []
    for ch in text:
        code = ord(ch)
        if code == 0x3000:
            out.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    return "".join(out)


def _clean_text(text):
    """去掉切片尾部的页脚（栖木家居｜… 200页 等）。"""
    text = text or ""
    idx = text.find(FOOTER_MARK)
    if idx != -1:
        text = text[:idx]
    return text.strip()


def parse_entry(entry):
    """把一条知识条目解析成 {qm, page, title, fields, text}。"""
    text = entry.get("text") or ""
    qm = (entry.get("qm") or "").strip()
    page = entry.get("page")
    body = _clean_text(text)

    title = ""
    fields = {}
    current = None
    buf = []

    for raw in body.split("\n"):
        s = raw.strip()
        if not s:
            continue

        matched = None
        for mk in FIELD_MARKERS:
            if s.startswith(mk) and len(s) > len(mk) and s[len(mk)] in ("：", "|", "｜", ":"):
                matched = mk
                break

        if matched:
            content = s[len(matched) + 1:].strip()
            # QM-S 条目首行的“处理指引｜资料版本 2026-10”是节标题，不是正文字段
            if matched == "处理指引" and content.startswith("资料版本"):
                continue
            if current:
                fields[current] = " ".join(buf).strip()
            current = matched
            buf = [content]
            continue

        if current is None:
            if not title and not s.startswith("资料版本") and not s.startswith("处理指引"):
                title = s
            continue

        buf.append(s)

    if current:
        fields[current] = " ".join(buf).strip()

    # 只去掉“QM-xxx ”开头的主题编号前缀，保留正文标题（目录行中间的 QM 区间保留）
    title = re.sub(r"^QM-[DPS]\d{3}\s*", "", title).strip()
    return {
        "qm": qm,
        "page": page,
        "title": title or qm,
        "fields": fields,
        "text": body,
    }


def _cjk_bigrams(text):
    """提取中文字符二元组集合（用于召回打分）。"""
    segs = _CJK_RE.findall(text)
    out = set()
    for seg in segs:
        for i in range(len(seg) - 1):
            out.add(seg[i:i + 2])
        if len(seg) == 1:
            out.add(seg)
    return out


def _ascii_tokens(text):
    """提取 ASCII 词元（型号、数字等），去掉单字符噪声。"""
    return [t for t in _ASCII_RE.findall(text.upper()) if len(t) >= 2]


def _load_knowledge_base():
    """加载本地索引；索引缺失/解析失败时返回空（服务判定为未就绪）。"""
    path = DEFAULT_KB_FILE
    if not path.exists():
        return [], None, None

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"知识库索引 {path.name} 解析失败：{exc}")

    all_entries = [parse_entry(it) for it in raw if isinstance(it, dict)]
    # 过滤封面/目录（page <= 2），只保留正文条目
    body_entries = []
    for e in all_entries:
        p = e.get("page")
        if isinstance(p, int) and p > 2:
            e["_bigrams"] = _cjk_bigrams(e["text"])
            e["_upper"] = e["text"].upper()
            body_entries.append(e)
    return body_entries, all_entries, str(path.name)


# 全局索引
KNOWLEDGE_BASE, RAW_KB, KB_SOURCE = _load_knowledge_base()
INDEX_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# 检索打分
# ---------------------------------------------------------------------------
# 问题意图 -> 优先抽取的字段（按顺序尝试）
INTENT_FIELD_ORDER = {
    "保温": ["保温性能", "处理说明", "客服答复示例"],
    "清洗": ["清洁与维护", "处理说明", "客服答复示例"],
    "组成": ["构造与配件", "处理说明", "客服答复示例"],
    "适配": ["适用边界", "核对与限制", "处理说明", "客服答复示例"],
    "规格": ["规格与价格", "处理说明", "客服答复示例"],
    "漏水": ["常见异常", "处理说明", "客服答复示例"],
    "温差": ["使用注意", "处理说明", "客服答复示例"],
    "裂纹": ["处理说明", "客服答复示例", "使用注意"],
    "售后": ["处理要点", "处理说明", "客服答复示例"],
    "物流": ["处理要点", "处理说明", "客服答复示例"],
    "版本": ["处理要点", "处理说明", "客服答复示例"],
    "默认": ["客服答复示例", "处理说明", "处理要点", "保温性能", "使用方法", "规格与价格"],
}

# 意图类别 -> 该类别字段内容中必须出现的关键词（命中才给“内容锚点”加分，用于区分相近意图）
INTENT_ANCHORS = {
    "保温": ("保温", "水温", "小时", "静置", "℃", "度"),
    "清洗": ("清洗", "清洁", "洗洁精", "晾干", "刷", "洗碗机", "微波炉"),
    "组成": ("组成", "配置", "包含", "配件", "密封圈"),
    "适配": ("适配", "互换", "通用", "专用", "配件"),
    "规格": ("容量", "毫升", "价格", "材质", "内胆", "重量", "尺寸", "溢出"),
    "漏水": ("漏水", "渗水", "渗漏", "水迹"),
    "温差": ("温差", "冷藏", "预温", "室温", "恢复"),
    "裂纹": ("裂纹", "崩口", "破裂", "停止使用"),
    "售后": ("售后", "退款", "退货", "换货", "运费", "破损", "赔偿"),
    "物流": ("物流", "配送", "快递", "订单", "收货"),
    "版本": ("版本", "来源", "冲突", "标题", "编号", "生效"),
}

# 保温类意图的触发词（不含裸“保温/温度”，避免“保温壶如何清洗”被误判为保温意图）
INSULATION_TRIGGERS = (
    "保冷", "保温时长", "保温时间", "保温效果", "保温性能", "能保温",
    "保温多久", "保温几小时", "多长时间", "多少度", "几小时",
)

# (触发词, 意图类别, 权重)
_INTENT_RULES = [
    (INSULATION_TRIGGERS, "保温", 2.0),
    (("清洗", "清洁", "异味", "气味", "茶渍", "手洗", "洗碗机", "微波炉", "消毒", "明火"), "清洗", 1.6),
    (("互换", "通用", "适配", "专用", "不通用", "配件适配", "能否互换", "能不能换"), "适配", 1.6),
    (("组成", "包含", "部件", "构造", "标配", "标准配置", "有什么", "由什么"), "组成", 1.6),
    (("容量", "毫升", "水位", "装多少", "多少升", "装满", "倒满", "ml", "价格", "多少钱", "售价", "零售价", "材质", "内胆", "不锈钢", "硅胶", "材料", "尺寸", "重量"), "规格", 1.6),
    (("漏水", "渗水", "渗漏", "水迹", "装反"), "漏水", 1.6),
    (("温差", "冷藏", "冰箱", "倒热水", "预温"), "温差", 1.6),
    (("裂纹", "开裂", "崩口", "破裂", "裂缝", "炸裂"), "裂纹", 1.6),
    (("售后", "退款", "退货", "换货", "申请", "保修", "破损", "损坏", "瑕疵", "质量问题", "赔偿", "运费", "邮费", "快递费"), "售后", 1.2),
    (("发货", "物流", "快递", "配送", "订单", "收货"), "物流", 1.2),
    (("版本", "冲突", "不一致", "来源", "生效", "文档"), "版本", 1.2),
]

# 查询扩展：把口语化问法映射到资料常用词，提升召回（仅用于检索打分，不改变展示）
KEYWORD_EXPANSION = [
    (("保温", "保冷", "保温时长", "保温多久", "能保温"), "保温性能 水温 静置 小时"),
    (("温差", "冷藏", "冰箱", "倒热水", "预温"), "温差 室温 恢复 预温"),
    (("裂纹", "开裂", "崩口", "破裂", "裂缝"), "裂纹 破损 停止使用"),
    (("漏水", "渗水", "渗漏", "水迹", "装反"), "漏水 密封圈 渗漏"),
    (("清洗", "清洁", "异味", "气味", "茶渍", "手洗", "洗碗机", "微波炉", "消毒", "明火"), "清洗 清洁 晾干"),
    (("密封圈", "杯盖", "旋盖"), "杯盖 密封圈"),
    (("容量", "毫升", "水位", "装多少"), "容量 水位线 标称容量"),
    (("倒满", "装满", "倒到杯口", "溢出"), "最高水位 溢出 水位线"),
    (("退款", "退货", "换货", "售后", "保修"), "售后 退款 退货 换货"),
    (("运费", "邮费", "快递费", "谁承担", "谁出"), "运费 退货 换货 承担"),
    (("快递", "配送", "物流", "发货", "收货"), "订单 配送 物流"),
    (("版本", "冲突", "不一致", "来源", "生效", "文档"), "资料版本 标题 编号 来源 冲突"),
    (("互换", "通用", "适配", "专用", "配件"), "配件 适配 型号 互换"),
    (("破损", "损坏", "瑕疵", "质量问题"), "破损 照片 证据 售后"),
    (("型号", "款式", "区分", "怎么选"), "型号 标签 区分 容量"),
]


def expand_query(question):
    """在原问题基础上追加领域关键词（用于检索打分，不改变展示）。"""
    extra = set()
    for triggers, terms in KEYWORD_EXPANSION:
        if any(t in question for t in triggers):
            for term in terms.split():
                extra.add(term)
    if not extra:
        return question
    return question + " " + " ".join(sorted(extra))


def _intent_hits(query):
    """返回命中的意图类别列表（去重、按规则顺序）。"""
    hits = []
    for triggers, category, weight in _INTENT_RULES:
        if any(t in query for t in triggers):
            hits.append(category)
    seen = []
    for h in hits:
        if h not in seen:
            seen.append(h)
    return seen


def _score_entry(e, q_norm, q_bigrams, q_ascii, intent_categories):
    """对单条条目打分（越高越相关）。"""
    upper = e["_upper"]
    s = 0.0

    if q_bigrams:
        overlap = len(q_bigrams & e["_bigrams"])
        s += 1.6 * overlap / len(q_bigrams)

    for tok in q_ascii:
        if tok and tok in upper:
            s += 0.6

    q_up = q_norm.upper()
    for m in MODELS:
        if m in q_up and m in upper:
            s += 3.0
        # 标题直接含型号（如“BX-500 旋盖保温杯”）更可能是该型号的主条目
        if m in q_up and m in e["title"].upper():
            s += 1.5

    for alias, m in UNAMBIGUOUS_ALIAS.items():
        if alias in q_norm and m in upper:
            s += 1.2

    fields = e["fields"]
    for category in intent_categories:
        order = INTENT_FIELD_ORDER.get(category, INTENT_FIELD_ORDER["默认"])
        hit = next((f for f in order if f in fields), None)
        if hit:
            fcontent = fields[hit]
            fbigrams = _cjk_bigrams(fcontent)
            fov = len(q_bigrams & fbigrams) / (len(q_bigrams) or 1)
            s += 1.6 * (1.0 + fov)
            # 字段内容锚点：字段里确实出现该意图的典型词，说明这条确实是该主题
            anchors = INTENT_ANCHORS.get(category, ())
            if anchors and any(a in fcontent for a in anchors):
                s += 2.0

    return s


def _to_score(s):
    """把原始打分映射到 0~1 的相关度（单调递增、上界收敛）。"""
    if s <= 0:
        return 0.0
    return round(1.0 - math.exp(-s / 3.0), 4)


def search_local(query, top_k=TOP_K):
    """在本地索引中检索，返回 (results, total)。total 为达到明确相关度的条目数。"""
    q_norm = _full_to_half(query or "").strip()
    q_expanded = _full_to_half(expand_query(q_norm))
    q_bigrams = _cjk_bigrams(q_expanded)
    q_ascii = _ascii_tokens(q_expanded)
    intent_categories = _intent_hits(q_norm)

    scored = []
    for e in KNOWLEDGE_BASE:
        raw = _score_entry(e, q_norm, q_bigrams, q_ascii, intent_categories)
        score = _to_score(raw)
        if score > 0:
            scored.append((score, e))

    scored.sort(key=lambda x: x[0], reverse=True)

    # 只要有任何命中（score>0）就返回 top_k，供客服结合低置信提示核对；
    # “明确相关”的条数用于统计总数
    results = [make_result(s, e) for s, e in scored[:top_k]]
    total = sum(1 for s, _ in scored if s >= RELEVANT_FLOOR)
    return results, total


# ---------------------------------------------------------------------------
# 无法确认检测（BUG-01）：实时状态 / 未知能力，静态资料无法回答
# ---------------------------------------------------------------------------
REALTIME_ORDER_PATTERNS = (
    "发货了吗", "发货了没", "有没有发货", "是否发货", "已经发货", "发货没有",
    "发货状态", "什么时候发货", "何时发货", "什么时候到", "何时到", "预计送达",
    "物流到", "物流信息", "物流状态", "物流进度", "物流查询", "快递到", "快递信息",
    "快递进度", "快递到哪", "到哪了", "订单状态", "订单进度", "订单到哪",
    "我的订单", "我的快递", "我的物流", "我的包裹", "包裹到哪",
)

REALTIME_STOCK_PATTERNS = (
    "库存", "现货", "还有多少", "有没有货", "有货吗", "补货", "到货", "缺货",
)

CAPABILITY_PATTERNS = (
    "app", "蓝牙", "wifi", "温度曲线", "连接手机", "智能连接", "传感器",
    "显示温度曲线", "手机端", "语音", "联网",
)

SENSITIVE_WORDS = ("验证码", "密码", "令牌", "token", "password")

# 知识库领域词：用于判断问题是否属于“产品/售后客服”范畴；不含单字“杯/壶”以免误伤“世界杯”等
DOMAIN_TERMS = (
    "保温", "保冷", "玻璃", "水杯", "杯子", "杯盖", "杯身", "杯口", "杯内",
    "壶身", "壶盖", "壶内", "出水口", "密封圈", "旋盖", "按压", "容量", "毫升",
    "水位", "材质", "内胆", "不锈钢", "硅胶", "配件", "部件", "型号", "产品",
    "清洗", "清洁", "异味", "温差", "冷藏", "冰箱", "漏水", "渗水", "渗漏",
    "裂纹", "开裂", "破损", "退款", "退货", "换货", "售后", "订单", "物流",
    "快递", "配送", "发货", "收货", "保修", "资料", "版本", "来源", "冲突",
    "gb-400", "bx-500", "bh-1000", "gb400", "bx500", "bh1000",
)


def is_domain_related(question):
    """判断问题是否落在知识库领域内（出现任一领域词即视为相关）。"""
    q = _full_to_half(question or "").lower()
    return any(t in q for t in DOMAIN_TERMS)


def detect_unanswerable(question):
    """返回 (is_unanswerable, reason, guidance) 或 (False, None, None)。"""
    q = _full_to_half(question)
    ql = q.lower()

    for word in SENSITIVE_WORDS:
        if word in ql:
            return (True, "sensitive",
                    "系统不处理密码、验证码、令牌等敏感信息，请勿在问题中输入。")

    for p in REALTIME_ORDER_PATTERNS:
        if p in q:
            return (True, "realtime_order",
                    "现有资料仅包含订单修改、配送规则等处理说明，无法查询具体订单的实时发货/物流状态。"
                    "请引导用户提供订单号，并说明需通过订单系统或仓储/物流核实，不能从现有资料确认。")

    for p in REALTIME_STOCK_PATTERNS:
        if p in q:
            return (True, "realtime_stock",
                    "现有资料不包含实时库存信息，无法确认当前库存数量。"
                    "请引导用户提供具体型号，并说明需通过库存系统核实，不能从现有资料确认。")

    for p in CAPABILITY_PATTERNS:
        if p in ql:
            return (True, "capability",
                    "现有资料未提及该功能（如连接手机 App、蓝牙/Wi-Fi、温度曲线显示等），无法确认是否支持。"
                    "请引导用户核对产品说明或咨询供应商，不要凭猜测答复。")

    return (False, None, None)


# ---------------------------------------------------------------------------
# 型号消歧与对比回答（BUG-04）
# ---------------------------------------------------------------------------
def _find_model_entry(model):
    """定位某型号的产品主条目（QM-D00x）。"""
    for e in KNOWLEDGE_BASE:
        if e["qm"].startswith("QM-D") and model in e["_upper"]:
            return e
    for e in KNOWLEDGE_BASE:
        if model in e["title"].upper():
            return e
    return None


def _insulation_intent(query):
    return any(w in query for w in INSULATION_TRIGGERS)


def _field_value(entry, category):
    """按意图类别抽取字段内容。"""
    if not entry:
        return ""
    order = INTENT_FIELD_ORDER.get(category, INTENT_FIELD_ORDER["默认"])
    for f in order:
        if f in entry["fields"]:
            val = entry["fields"][f]
            if val:
                return val
    for v in entry["fields"].values():
        if v:
            return v
    return entry["text"]


def _model_label(model):
    return {
        "GB-400": "GB-400 带盖玻璃水杯",
        "BX-500": "BX-500 旋盖保温杯",
        "BH-1000": "BH-1000 按压出水保温壶",
    }.get(model, model)


def build_model_compare_answer(query):
    """多型号对比 / 未指明型号先追问：返回 (answer, entries) 或 (None, None)。"""
    if not _insulation_intent(query):
        return None, None

    q_up = query.upper()
    present = [m for m in MODELS if m in q_up]

    if not present:
        if "保温壶" in query:
            present = ["BH-1000"]
        elif "玻璃杯" in query or "玻璃水杯" in query:
            present = ["GB-400"]
        elif "保温杯" in query or "保温" in query:
            bx = _find_model_entry("BX-500")
            bh = _find_model_entry("BH-1000")
            lines = ["请先确认具体型号，不同型号的保温性能不同："]
            if bx:
                lines.append(f"• BX-500 旋盖保温杯：{_field_value(bx, '保温')}")
            if bh:
                lines.append(f"• BH-1000 按压出水保温壶：{_field_value(bh, '保温')}")
            lines.append("请确认用户使用的是哪一款，以便给出准确答复。")
            entries = [e for e in (bx, bh) if e]
            return ("\n".join(lines), entries)

    if len(present) >= 2:
        lines = []
        entries = []
        for m in present:
            e = _find_model_entry(m)
            if e:
                lines.append(f"• {_model_label(m)}：{_field_value(e, '保温')}")
                entries.append(e)
        return ("\n".join(lines), entries)

    return None, None


# ---------------------------------------------------------------------------
# 结果转前端结构（每条单一 QM、页码、版本，不含文件路径）
# ---------------------------------------------------------------------------
def make_result(score, e):
    return {
        "qm": e["qm"],
        "page": e["page"],
        "version": KB_VERSION,
        "title": e["title"],
        "content": e["text"],
        "score": score,
    }


# ---------------------------------------------------------------------------
# 简短回答抽取（本地索引，按意图抽取对应字段，不编造）
# ---------------------------------------------------------------------------
def build_short_answer(results, query):
    """从 top1 条目中按问题意图抽取最相关的字段作为简短回答。"""
    if not results:
        return ""
    q_norm = _full_to_half(query or "")
    categories = _intent_hits(q_norm)
    category = categories[0] if categories else "默认"
    qm = results[0].get("qm")
    for e in KNOWLEDGE_BASE:
        if e["qm"] == qm:
            return _field_value(e, category)
    return results[0].get("content", "")


# ---------------------------------------------------------------------------
# 资料导入 / 解析 / 索引状态（AC-F04-01~04）
# ---------------------------------------------------------------------------
def rebuild_index(entries, source_name):
    """用新解析的条目重建全局索引。"""
    body = []
    for e in entries:
        p = e.get("page")
        if isinstance(p, int) and p > 2:
            e["_bigrams"] = _cjk_bigrams(e["text"])
            e["_upper"] = e["text"].upper()
            body.append(e)
    global KNOWLEDGE_BASE, RAW_KB, KB_SOURCE
    with INDEX_LOCK:
        KNOWLEDGE_BASE = body
        RAW_KB = entries
        KB_SOURCE = source_name
    return len(body)


def parse_pdf_bytes(data):
    """解析 PDF 字节，返回知识条目列表。需要 PyMuPDF（fitz）。"""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        raise RuntimeError(
            "PDF 解析需要 PyMuPDF（fitz）依赖，当前环境未安装。"
            "可改用已解析的 knowledge_base.json 上传，或先执行 pip install pymupdf。"
        )

    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise RuntimeError(
            "PDF 解析失败：文件不是有效的 PDF 或已损坏，请重新导出后上传。"
            "（如需解析《栖木家居客服知识资料》200 页 PDF，请确认本机已安装 PyMuPDF：pip install pymupdf）"
        )

    if doc.page_count == 0:
        raise RuntimeError("PDF 无有效页面，无法解析。")

    qm_pattern = re.compile(r"(QM-[DPS]\d{3})")
    blocks = []
    current_qm = None
    current_text = []
    current_page = None

    for page_num in range(doc.page_count):
        page = doc[page_num]
        text = page.get_text("text") or ""
        for line in text.split("\n"):
            m = qm_pattern.search(line)
            if m:
                if current_qm:
                    blocks.append({
                        "qm": current_qm,
                        "page": current_page,
                        "text": "\n".join(current_text).strip(),
                    })
                current_qm = m.group(1)
                current_page = page_num + 1
                current_text = [line]
            elif current_qm:
                current_text.append(line)

    if current_qm:
        blocks.append({
            "qm": current_qm,
            "page": current_page,
            "text": "\n".join(current_text).strip(),
        })

    if not blocks:
        raise RuntimeError("PDF 中未找到任何 QM 主题编号，请确认文件为《栖木家居客服知识资料》200 页 PDF。")
    return blocks


def parse_json_bytes(data):
    """解析 knowledge_base.json 字节，返回知识条目列表。"""
    try:
        raw = json.loads(data.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(f"JSON 解析失败：{exc}")
    if not isinstance(raw, list):
        raise RuntimeError("JSON 结构不正确：应为知识条目数组。")
    entries = []
    for it in raw:
        if not isinstance(it, dict) or "qm" not in it or "text" not in it:
            raise RuntimeError("JSON 条目缺少 qm/text 字段，结构不正确。")
        entries.append(parse_entry(it))
    return entries


def index_status():
    """生成索引状态摘要。"""
    with INDEX_LOCK:
        body = list(KNOWLEDGE_BASE)
        raw = list(RAW_KB or [])
        source = KB_SOURCE

    topics = {"QM-D": 0, "QM-P": 0, "QM-S": 0}
    for e in body:
        prefix = (e["qm"] or "")[:4]
        if prefix in topics:
            topics[prefix] += 1

    pages = [e["page"] for e in body if isinstance(e.get("page"), int)]
    page_range = [min(pages), max(pages)] if pages else None

    sections = []
    for e in raw:
        p = e.get("page")
        if isinstance(p, int) and p <= 2 and e.get("title"):
            title = re.sub(r"\.{3,}.*$", "", e["title"]).strip()
            if title:
                sections.append(title)

    return {
        "ready": len(body) > 0,
        "source": source,
        "entry_count": len(body),
        "version": KB_VERSION,
        "page_range": page_range,
        "topics": topics,
        "sections": sections[:24],
    }


# ---------------------------------------------------------------------------
# HTTP 服务
# ---------------------------------------------------------------------------
MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "QimuKB/2.0"

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, rel_path):
        full = (BASE_DIR / rel_path).resolve()
        if BASE_DIR.resolve() not in full.parents and full != BASE_DIR.resolve():
            self.send_error(403, "Forbidden")
            return
        if not full.is_file():
            self.send_error(404, "Not Found")
            return
        data = full.read_bytes()
        mime = MIME_TYPES.get(full.suffix.lower(), "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _log(self, message):
        print(f"[{self.log_date_time_string()}] {message}")

    # ---------- 路由 ----------
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send_file("index.html")
        elif path == "/admin":
            self._send_file("admin.html")
        elif path == "/static/style.css":
            self._send_file("static/style.css")
        elif path == "/static/app.js":
            self._send_file("static/app.js")
        elif path == "/static/admin.js":
            self._send_file("static/admin.js")
        elif path == "/api/health":
            self._send_json({"status": "ok", "index_ready": len(KNOWLEDGE_BASE) > 0})
        elif path == "/api/index/status":
            self._send_json({"status": "ok", "index": index_status()})
        else:
            self.send_error(404, "Not Found")

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/search":
            self._handle_search()
        elif path == "/api/import":
            self._handle_import()
        else:
            self.send_error(404, "Not Found")

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b""

    # ---------- 检索接口 ----------
    def _handle_search(self):
        # BUG-08：索引未就绪属于服务故障，必须显式报错，不能伪装成“资料不足”
        if not KNOWLEDGE_BASE:
            self._send_json({
                "status": "error",
                "message": "知识库索引未就绪（缺少 knowledge_base.json 或解析失败），请先在 /admin 导入资料后再检索。",
            }, 502)
            return

        raw = self._read_body()
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            self._send_json({"status": "error", "message": "请求体不是合法 JSON。"}, 400)
            return

        question = (body.get("question") or "").strip()

        if not question:
            self._send_json({"status": "error", "message": "请输入要查询的问题。"}, 400)
            return

        if len(question) > MAX_QUESTION_LEN:
            self._send_json({
                "status": "error",
                "message": f"问题过长（当前 {len(question)} 字符），请精简到 {MAX_QUESTION_LEN} 字符以内。",
            }, 400)
            return

        lower_q = question.lower()
        for word in SENSITIVE_WORDS:
            if word in lower_q:
                self._send_json({
                    "status": "rejected",
                    "message": "系统不处理密码、验证码、令牌等敏感信息，请勿在问题中输入。",
                })
                return

        # 1) 无法确认检测（实时状态 / 未知能力）
        unanswerable, reason, guidance = detect_unanswerable(question)
        if unanswerable:
            self._send_json({
                "status": "success",
                "question": question,
                "answer": guidance,
                "insufficient": True,
                "reason": reason,
                "guidance": guidance,
                "total": 0,
                "shown": 0,
                "results": [],
                "index": index_status(),
            })
            return

        # 2) 型号消歧 / 多型号对比
        compare_answer, compare_entries = build_model_compare_answer(question)
        if compare_answer is not None:
            results = [make_result(0.9, e) for e in (compare_entries or [])]
            self._send_json({
                "status": "success",
                "question": question,
                "answer": compare_answer,
                "insufficient": False,
                "reason": None,
                "guidance": None,
                "total": len(results),
                "shown": len(results),
                "results": results,
                "top_score": 0.9,
                "index": index_status(),
            })
            return

        # 3) 本地检索
        results, total = search_local(question)
        top_score = results[0]["score"] if results else 0.0
        related = is_domain_related(question)

        # 无任何命中，或最高相关度低于阈值，判为“资料可能未覆盖”；
        # 非领域问题需要更高相关度才算有答案，否则判“无关”
        if not related:
            insufficient = (not results) or (top_score < RELEVANT_FLOOR)
        else:
            insufficient = (not results) or (top_score < LOW_CONFIDENCE_THRESHOLD)

        answer = ""
        guidance = None
        if results and not insufficient:
            answer = build_short_answer(results, question)

        if insufficient:
            reason = "unrelated" if not related else "low_confidence"
            if not related:
                answer = ("该问题与本知识库无关，现有资料未覆盖，无法确认。"
                          "本知识库仅覆盖产品使用、清洁维护、订单配送与售后处理等内容。")
            else:
                answer = ("现有资料未能确认该问题的答案，请向用户补充必要信息"
                          "（如产品型号、订单状态、使用场景等），不要凭猜测作答。")
            guidance = answer
            results = []
            total = 0

        self._send_json({
            "status": "success",
            "question": question,
            "answer": answer,
            "insufficient": insufficient,
            "reason": reason if insufficient else None,
            "guidance": guidance,
            "total": total,
            "shown": len(results),
            "results": results,
            "top_score": top_score,
            "low_confidence_threshold": LOW_CONFIDENCE_THRESHOLD,
            "index": index_status(),
        })

    # ---------- 资料导入接口 ----------
    def _handle_import(self):
        data = self._read_body()
        filename = (self.headers.get("X-File-Name") or "").strip() or "upload"

        if not data:
            self._send_json({"status": "error", "message": "未收到文件内容。"}, 400)
            return

        try:
            stripped = data.lstrip()
            if stripped[:4] == b"%PDF":
                entries = parse_pdf_bytes(data)
                kind = "PDF"
            elif stripped[:1] in (b"[", b"{"):
                entries = parse_json_bytes(data)
                kind = "JSON"
            else:
                self._send_json({
                    "status": "error",
                    "message": "无法识别的文件类型：请上传《栖木家居客服知识资料》PDF 或已解析的 knowledge_base.json。",
                }, 400)
                return

            count = rebuild_index(entries, filename)
            if count == 0:
                raise RuntimeError("解析完成但没有正文条目（页码 > 2 的条目为空），请检查文件。")

            out = [{"qm": e.get("qm"), "page": e.get("page"), "text": e.get("text", "")}
                   for e in entries]
            DEFAULT_KB_FILE.write_text(
                json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

            self._send_json({
                "status": "success",
                "message": f"导入成功：解析到 {len(entries)} 个主题，正文条目 {count} 条，索引已重建。",
                "kind": kind,
                "index": index_status(),
            })
        except RuntimeError as exc:
            self._send_json({"status": "error", "message": str(exc)}, 400)
        except Exception as exc:
            self._log(f"import error: {exc}")
            self._send_json({"status": "error", "message": f"导入失败：{exc}"}, 500)


def main():
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as e:
        print("=" * 60)
        print("启动失败：端口被占用或无法监听。")
        print(f"  错误信息：{e}")
        print(f"  常见原因：已有另一个 app.py 实例在运行（端口 {PORT}）。")
        print("  解决办法：")
        print("    1) 关闭之前运行的终端/进程；或")
        print("    2) 在 .env 中把 PORT 改成其他端口（如 8001）后重启。")
        print("=" * 60)
        return

    url = f"http://127.0.0.1:{PORT}"
    print("=" * 60)
    print("栖木家居客服知识库 已启动")
    print(f"  本机访问：  {url}")
    print(f"  索引管理：  {url}/admin")
    print(f"  局域网访问：http://<本机局域网IPv4地址>:{PORT}  （监听 {HOST}）")
    print(f"  索引来源：  {KB_SOURCE}，正文条目 {len(KNOWLEDGE_BASE)} 条")
    print("  停止服务：  按 Ctrl+C")
    print("  正在自动打开浏览器，如未弹出请手动访问上面的地址")
    print("=" * 60)

    threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止。")
        server.server_close()


if __name__ == "__main__":
    main()
