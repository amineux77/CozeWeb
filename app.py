import os
import json
import requests
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)

# 限制上传文件大小：10 MB
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024

COZE_URL = "https://gfpgvc3g9g.coze.site/stream_run"
PROJECT_ID = "7692864184282873899"
SESSION_ID = "1Gl-6qs58oTnbmYuwJ14d"

# 允许上传的图片格式
ALLOWED_IMAGE_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".webp"
}


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/chat", methods=["POST"])
def chat():
    # 兼容原来的网站 JSON 文字请求
    body = request.get_json(silent=True) or {}
    question = str(body.get("message", "")).strip()

    if not question:
        return jsonify({"reply": "请输入问题后再发送。"})

    return ask_coze(question)


@app.route("/upload-image", methods=["POST"])
def upload_image():
    """
    接收网站上传的图片。
    当前只进行格式和大小校验，不会假装图片已经传给 Coze。
    """
    image_file = request.files.get("image")

    if image_file is None or not image_file.filename:
        return jsonify({
            "success": False,
            "reply": "请先选择一张图片。"
        }), 400

    filename = os.path.basename(image_file.filename)
    extension = os.path.splitext(filename)[1].lower()

    if extension not in ALLOWED_IMAGE_EXTENSIONS:
        return jsonify({
            "success": False,
            "reply": "目前仅支持 PNG、JPG、JPEG 和 WEBP 图片。"
        }), 400

    # 读取图片并检查实际文件大小
    image_data = image_file.read()

    if not image_data:
        return jsonify({
            "success": False,
            "reply": "上传的图片为空，请重新选择。"
        }), 400

    if len(image_data) > 10 * 1024 * 1024:
        return jsonify({
            "success": False,
            "reply": "图片不能超过 10 MB。"
        }), 413

    # 重要：这里尚未把图片传给 Coze。
    # 下一步需要接入 Coze 实际支持的图片传输方式。
    return jsonify({
        "success": True,
        "reply": "图片已上传到网站后端，但还没有发送给 Coze。下一步需要接通图片识别接口。",
        "filename": filename,
        "size": len(image_data)
    })


def ask_coze(question):
    token = os.environ.get("COZE_TOKEN")

    if not token:
        return jsonify({
            "reply": "尚未配置 Coze Token，请检查环境变量。"
        }), 500

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }

    payload = {
        "content": {
            "query": {
                "prompt": [
                    {
                        "type": "text",
                        "content": {
                            "text": question
                        }
                    }
                ]
            }
        },
        "type": "query",
        "session_id": SESSION_ID,
        "project_id": PROJECT_ID
    }

    try:
        answer_parts = []

        with requests.post(
            COZE_URL,
            headers=headers,
            json=payload,
            stream=True,
            timeout=120
        ) as response:

            response.raise_for_status()

            for line in response.iter_lines(decode_unicode=True):
                if not line:
                    continue

                line = line.strip()

                if not line.startswith("data:"):
                    continue

                data_text = line[5:].strip()

                if not data_text or data_text == "[DONE]":
                    continue

                try:
                    item = json.loads(data_text)
                except json.JSONDecodeError:
                    continue

                if not isinstance(item, dict):
                    continue

                if item.get("type") == "answer":
                    content = item.get("content") or {}

                    if isinstance(content, dict):
                        answer = content.get("answer")

                        if isinstance(answer, str) and answer:
                            answer_parts.append(answer)

                elif item.get("type") == "error":
                    app.logger.error(
                        "Coze 返回错误：%s",
                        item.get("content")
                    )

        reply = "".join(answer_parts).strip()

        if not reply:
            reply = "没有获取到回答，请检查服务日志。"

        return jsonify({"reply": reply})

    except requests.RequestException:
        app.logger.exception("调用 Coze 接口失败")

        return jsonify({
            "reply": "调用 Coze 接口失败，请检查服务日志。"
        }), 502

    except Exception:
        app.logger.exception("处理回答时发生错误")

        return jsonify({
            "reply": "处理回答时发生错误，请检查服务日志。"
        }), 500


@app.errorhandler(413)
def file_too_large(error):
    return jsonify({
        "success": False,
        "reply": "上传文件过大，请选择 10 MB 以内的文件。"
    }), 413


if __name__ == "__main__":
    app.run(debug=True)
