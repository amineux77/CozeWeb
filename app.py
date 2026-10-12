import os
import json
import requests
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)

COZE_URL = "https://gfpgvc3g9g.coze.site/stream_run"
PROJECT_ID = "7692864184282873899"
SESSION_ID = "1Gl-6qs58oTnbmYuwJ14d"


@app.route("/")
def index():
    response = render_template("index.html")
    print("当前加载的网页：templates/index.html")
    return response

@app.route("/chat", methods=["POST"])
def chat():
    body = request.get_json(silent=True) or {}
    question = str(body.get("message", "")).strip()

    if not question:
        return jsonify({"reply": "请输入问题后再发送。"})

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

            for line in response.iter_lines(decode_unicode=False):
                if not line:
                    continue

                # SSE 数据按 UTF-8 解码，避免中文乱码
                if isinstance(line, bytes):
                    line = line.decode("utf-8", errors="replace")

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

                # 你的接口使用 type="answer"，不是 event 字段
                if item.get("type") == "answer":
                    content = item.get("content") or {}

                    if isinstance(content, dict):
                        text = content.get("answer")

                        if isinstance(text, str) and text:
                            answer_parts.append(text)

                # 如果接口返回错误事件，记录错误
                elif item.get("type") == "error":
                    content = item.get("content") or {}
                    error_message = (
                        content.get("error")
                        if isinstance(content, dict)
                        else None
                    )

                    if error_message:
                        app.logger.error(
                            "Coze 返回错误：%s",
                            error_message
                        )

        # 按接口事件顺序拼接回答片段
        reply = "".join(answer_parts).strip()

        if not reply:
            reply = "没有提取到回答，请查看 CMD 窗口中的日志。"

        return jsonify({"reply": reply})

    except requests.RequestException as e:
        app.logger.exception("调用 Coze 接口失败")

        return jsonify({
            "reply": f"调用 Coze 接口失败：{str(e)}"
        }), 502

    except Exception:
        app.logger.exception("处理回答时发生错误")

        return jsonify({
            "reply": "处理回答时发生错误，请查看 CMD 窗口中的报错信息。"
        }), 500


if __name__ == "__main__":
    app.run(debug=True)