from flask import Flask, request, jsonify
from flask_cors import CORS
from rank_bm25 import BM25Okapi
import json
import jieba  # 用于中文分词，需安装 pip install jieba

app = Flask(__name__)
CORS(app)  # 允许跨域，方便开发B写前端

# 1. 加载数据
with open('knowledge_base.json', 'r', encoding='utf-8') as f:
    kb_data = json.load(f)

# 2. 准备 BM25 检索
# 把每条知识的内容用 jieba 分词
corpus = [jieba.lcut(item['text']) for item in kb_data]
bm25 = BM25Okapi(corpus)


@app.route('/api/search', methods=['POST'])
def search():
    data = request.get_json()
    query = data.get('question', '').strip()

    # 【AC-F01-02】空输入拦截
    if not query:
        return jsonify({"error": "输入不能为空"}), 400

    # 【AC-F03-05】简单敏感词过滤
    if any(word in query for word in ['验证码', '密码', '令牌']):
        return jsonify({
            "status": "rejected",
            "message": "系统不处理敏感信息，请勿输入密码或验证码。"
        }), 200

    # 分词并检索
    tokenized_query = jieba.lcut(query)
    top_5 = bm25.get_top_n(tokenized_query, kb_data, n=5)

    # 检查最高分（可选：做信息不足判断）
    # 这里给开发B留出空间：如果最高分太低，B可以在前端提示

    return jsonify({
        "status": "success",
        "results": top_5  # 返回包含 qm, page, text 的列表
    })


if __name__ == '__main__':
    # 【F06 局域网要求】必须监听 0.0.0.0
    app.run(host='0.0.0.0', port=5000, debug=True)

from flask import Flask, request, jsonify
from flask_cors import CORS
from rank_bm25 import BM25Okapi
import json
import jieba

app = Flask(__name__)
CORS(app)  # 允许跨域，方便开发B写前端

# 1. 加载刚才解析出来的数据
print("正在加载知识库...")
with open('knowledge_base.json', 'r', encoding='utf-8') as f:
    kb_data = json.load(f)

# 2. 准备 BM25 检索（分词并建立索引）
print("正在建立检索索引...")
corpus = [jieba.lcut(item['text']) for item in kb_data]
bm25 = BM25Okapi(corpus)
print("索引建立完成！服务已启动。")


@app.route('/api/search', methods=['POST'])
def search():
    data = request.get_json()
    # 获取前端传来的问题，如果没有就设为空
    query = data.get('question', '').strip() if data else ''

    # 【AC-F01-02】空输入拦截
    if not query:
        return jsonify({"error": "输入不能为空"}), 400

    # 【AC-F03-05】敏感词过滤
    if any(word in query for word in ['验证码', '密码', '令牌']):
        return jsonify({
            "status": "rejected",
            "message": "系统不处理敏感信息，请勿输入密码或验证码。"
        }), 200

    # 分词并检索 Top 5
    tokenized_query = jieba.lcut(query)
    top_5 = bm25.get_top_n(tokenized_query, kb_data, n=5)

    # 返回给前端
    return jsonify({
        "status": "success",
        "results": top_5
    })


if __name__ == '__main__':
    # 必须监听 0.0.0.0，满足局域网访问的作业要求
    app.run(host='0.0.0.0', port=5000, debug=True)