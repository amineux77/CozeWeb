import os
import json
import uuid
import logging
import requests
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)

# 限制上传文件大小：图片上限 10 MB；额外预留 1 MB 容纳 multipart 报文开销，
# 真正的大小校验在下面的 upload_image() 中按图片字节数严格执行。
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024 + 1024 * 1024

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("material_mechanics")

# 扣子编程项目信息
PROJECT_ID = "7692864184282873899"

# 扣子编程项目的运行域名，stream_run 接口部署在该域名下
PROJECT_DOMAIN = os.environ.get("COZE_PROJECT_DOMAIN", "gfpgvc3g9g.coze.site")
STREAM_RUN_URL = "https://" + PROJECT_DOMAIN + "/stream_run"

# 扣子开放平台地址，用于把图片文件上传到项目存储
COZE_API_BASE = os.environ.get("COZE_API_BASE", "https://api.coze.cn").rstrip("/")
FILE_UPLOAD_URL = COZE_API_BASE + "/v1/vibe_projects/files/upload"

# 仍使用原来的会话 ID，保持原有文字问答行为不变
SESSION_ID = "1Gl-6qs58oTnbmYuwJ14d"

# 允许上传的图片格式
ALLOWED_IMAGE_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".webp"
}

EXTENSION_TO_MIMETYPE = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}

MAX_IMAGE_SIZE = 10 * 1024 * 1024

# 出网超时拆分为「连接」与「读取」两段：
#   - 连接超时设短：网络不通 / 被限制时快速失败并返回明确 JSON 错误，
#     而不是让请求长时间挂住（挂住会被平台网关判为 APP_UNREACHABLE）；
#   - 读取超时放宽：给模型生成回答留出时间。
CONNECT_TIMEOUT = 5
READ_TIMEOUT = 120
REQUEST_TIMEOUT = (CONNECT_TIMEOUT, READ_TIMEOUT)


class CozeError(Exception):
    """调用扣子接口失败时抛出，携带应返回给前端的 HTTP 状态码。"""

    def __init__(self, message, status_code=502):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def error_response(message, status=400, code="error"):
    """统一的错误返回格式。"""
    return jsonify({
        "ok": False,
        "reply": message,
        "error_code": code,
    }), status


def success_response(reply):
    """统一的成功返回格式。"""
    return jsonify({
        "ok": True,
        "reply": reply,
    })


def get_token():
    """从环境变量读取扣子访问令牌；绝不打印令牌内容。"""
    token = os.environ.get("COZE_TOKEN", "").strip()
    return token or None


def describe_http_error(response):
    """把扣子接口的 HTTP 错误翻译成可读提示，不泄露任何凭证。"""
    status = response.status_code
    detail = ""
    try:
        payload = response.json()
        if isinstance(payload, dict):
            detail = payload.get("msg") or payload.get("message") or ""
    except Exception:
        detail = (response.text or "")[:200]

    if status == 401:
        return "扣子鉴权失败（401）：令牌无效或已过期，请检查服务端 COZE_TOKEN 配置。"
    if status == 403:
        return "扣子拒绝访问（403）：令牌缺少所需权限（run / uploadFileToStorage），" \
               "请在扣子开放平台为令牌勾选对应权限。"
    if status == 404:
        return "扣子接口不存在（404）：请确认项目域名与项目 ID 是否正确。"
    if status >= 500:
        return "扣子服务异常（%d）：%s" % (status, detail or "请稍后重试。")
    return "扣子接口返回错误（%d）：%s" % (status, detail or "请稍后重试。")


def upload_image_to_coze(image_data, filename, mimetype):
    """
    把图片上传到扣子编程项目存储，返回可用于模型理解的图片 URL。

    官方依据（扣子编程 Web SDK 源码）：
      POST https://api.coze.cn/v1/vibe_projects/files/upload
      multipart 字段：file、project_id
      响应：{ data: { url: ... } }
    """
    token = get_token()
    if not token:
        raise CozeError("尚未配置扣子令牌（COZE_TOKEN），无法上传图片。", 500)

    headers = {"Authorization": "Bearer " + token}
    files = {"file": (filename, image_data, mimetype or "application/octet-stream")}
    data = {"project_id": PROJECT_ID}

    try:
        response = requests.post(
            FILE_UPLOAD_URL,
            headers=headers,
            files=files,
            data=data,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.Timeout:
        raise CozeError("上传图片到扣子超时，请稍后重试。", 504)
    except requests.RequestException as exc:
        raise CozeError("无法连接扣子文件上传接口：%s" % exc, 502)

    if response.status_code >= 400:
        raise CozeError(describe_http_error(response), 502)

    try:
        payload = response.json()
    except ValueError:
        raise CozeError("扣子文件上传接口返回了无法解析的内容。", 502)

    image_url = _extract_url(payload)
    if not image_url:
        logger.error("上传接口未返回图片 URL，响应键：%s",
                     list(payload.keys()) if isinstance(payload, dict) else type(payload))
        raise CozeError("扣子文件上传接口未返回图片地址，图片未能进入识题流程。", 502)

    logger.info("图片上传成功：filename=%s size=%d", filename, len(image_data))
    return image_url


def _extract_url(payload):
    if not isinstance(payload, dict):
        return None
    candidates = []
    data = payload.get("data")
    if isinstance(data, dict):
        candidates += [data.get("url"), data.get("file_url"), data.get("uri")]
    candidates += [payload.get("url"), payload.get("file_url")]
    for item in candidates:
        if isinstance(item, str) and item.strip():
            return item.strip()
    return None


def run_project(prompt_items):
    """
    调用扣子编程项目的 stream_run 接口，返回模型回答文本。

    prompt_items 为官方协议中的 content.query.prompt 数组，例如：
      [{"type": "text", "content": {"text": "..."}},
       {"type": "upload_file", "content": {"upload_file": {
           "file_name": "题目.png", "url": "https://...", "file_path": ""}}}]
    """
    token = get_token()
    if not token:
        raise CozeError("尚未配置扣子令牌（COZE_TOKEN），请检查环境变量。", 500)

    payload = {
        "content": {
            "query": {
                "prompt": prompt_items
            }
        },
        "type": "query",
        "session_id": SESSION_ID,
        "project_id": PROJECT_ID,
    }

    headers = {
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }

    answer_parts = []
    error_message = None

    try:
        with requests.post(
            STREAM_RUN_URL,
            headers=headers,
            json=payload,
            stream=True,
            timeout=REQUEST_TIMEOUT,
        ) as response:

            if response.status_code >= 400:
                raise CozeError(describe_http_error(response), 502)

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

                item_type = item.get("type")
                content = item.get("content") or {}

                if item_type == "answer" and isinstance(content, dict):
                    answer = content.get("answer")
                    if isinstance(answer, str) and answer:
                        answer_parts.append(answer)

                elif item_type == "error":
                    error_message = content.get("error") or content
                    logger.error("扣子返回错误事件：%s", error_message)

    except requests.Timeout:
        raise CozeError("调用扣子接口超时，请稍后重试。", 504)
    except requests.RequestException as exc:
        raise CozeError("无法连接扣子运行接口：%s" % exc, 502)

    reply = "".join(answer_parts).strip()

    if not reply and error_message:
        raise CozeError("扣子返回错误：%s" % str(error_message)[:200], 502)

    return reply


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/chat", methods=["POST"])
def chat():
    # 兼容原来的网站 JSON 文字请求
    body = request.get_json(silent=True) or {}
    question = str(body.get("message", "")).strip()

    if not question:
        return error_response("请输入问题后再发送。", 400, "empty_message")

    try:
        reply = run_project([
            {"type": "text", "content": {"text": question}}
        ])
    except CozeError as exc:
        logger.error("文字问答调用扣子失败：%s", exc.message)
        return error_response(exc.message, exc.status_code, "coze_error")

    if not reply:
        return error_response("没有获取到回答，请稍后重试。", 502, "empty_answer")

    return success_response(reply)


@app.route("/upload-image", methods=["POST"])
def upload_image():
    """
    接收网站上传的图片（可带补充文字），把图片送入扣子编程项目进行识题。

    流程：校验 -> 上传图片到扣子项目存储 -> 组装官方协议的 prompt -> stream_run。
    """
    image_file = request.files.get("image")
    message = str(request.form.get("message", "")).strip()

    if image_file is None or not image_file.filename:
        return error_response("请先选择一张图片。", 400, "missing_image")

    filename = os.path.basename(image_file.filename)
    extension = os.path.splitext(filename)[1].lower()

    if extension not in ALLOWED_IMAGE_EXTENSIONS:
        return error_response("目前仅支持 PNG、JPG、JPEG 和 WEBP 图片。", 400, "bad_format")

    image_data = image_file.read()

    if not image_data:
        return error_response("上传的图片为空，请重新选择。", 400, "empty_file")

    if len(image_data) > MAX_IMAGE_SIZE:
        return error_response("图片不能超过 10 MB。", 413, "file_too_large")

    mimetype = (image_file.mimetype or "").lower()
    if mimetype not in EXTENSION_TO_MIMETYPE.values():
        mimetype = EXTENSION_TO_MIMETYPE[extension]

    # 第一步：把图片上传到扣子项目存储，取得可被模型读取的图片地址
    try:
        image_url = upload_image_to_coze(image_data, filename, mimetype)
    except CozeError as exc:
        logger.error("图片上传失败：%s", exc.message)
        return error_response(exc.message, exc.status_code, "upload_failed")

    # 第二步：按官方协议组装 prompt。图片使用 upload_file 内容类型。
    # 与扣子编程 Web SDK 的 insertChatContent 保持一致：
    #   {type:"upload_file", content:{upload_file:{file_id, file_name, url, file_path}}}
    prompt_items = []
    if message:
        prompt_items.append({"type": "text", "content": {"text": message}})
    else:
        # 仅上传图片时补一句默认指令，确保模型进入识题与解答流程
        prompt_items.append({
            "type": "text",
            "content": {"text": "请识别图片中的材料力学题目，分析已知条件和受力情况，并给出完整解答。"}
        })
    prompt_items.append({
        "type": "upload_file",
        "content": {
            "upload_file": {
                "file_id": uuid.uuid4().hex,
                "file_name": filename,
                "url": image_url,
                "file_path": "",
            }
        }
    })

    # 第三步：调用 stream_run，让模型读取图片并解答
    try:
        reply = run_project(prompt_items)
    except CozeError as exc:
        logger.error("图片识题调用扣子失败：%s", exc.message)
        return error_response(exc.message, exc.status_code, "coze_error")

    if not reply:
        return error_response("图片已提交，但模型没有返回内容，请稍后重试。", 502, "empty_answer")

    logger.info("图片识题完成：filename=%s has_text=%s", filename, bool(message))
    return success_response(reply)


@app.errorhandler(413)
def file_too_large(error):
    return error_response("上传文件过大，请选择 10 MB 以内的文件。", 413, "file_too_large")


@app.errorhandler(Exception)
def handle_unexpected(error):
    """兜底：任何未捕获异常都返回 JSON，绝不把 HTML 错误页抛给前端。"""
    from werkzeug.exceptions import HTTPException

    if isinstance(error, HTTPException):
        return error

    logger.exception("未捕获异常")
    return error_response("服务器内部错误，请稍后重试。", 500, "internal_error")


@app.route("/diag")
def diag():
    """
    临时诊断接口：确认部署环境能否访问扣子接口及各步骤真实耗时。
    只返回「是否已配置令牌」，绝不返回令牌内容。
    """
    import time

    result = {
        "ok": True,
        "token_configured": bool(get_token()),
        "project_id": PROJECT_ID,
        "project_domain": PROJECT_DOMAIN,
        "api_base": COZE_API_BASE,
    }

    def probe(url):
        t0 = time.time()
        try:
            r = requests.get(url, timeout=(CONNECT_TIMEOUT, 10))
            return {"endpoint": url, "status": r.status_code,
                    "seconds": round(time.time() - t0, 3)}
        except Exception as exc:  # noqa: BLE001
            return {"endpoint": url, "error": type(exc).__name__,
                    "message": str(exc)[:200],
                    "seconds": round(time.time() - t0, 3)}

    result["probes"] = {
        "coze_api_root": probe(COZE_API_BASE + "/"),
        "file_upload": probe(FILE_UPLOAD_URL),
        "stream_run": probe(STREAM_RUN_URL),
    }
    return jsonify(result)


@app.route("/diag/sleep/<int:sec>")
def diag_sleep(sec):
    """临时：纯 sleep 不联网，用来测量平台允许的最大请求时长。"""
    import time

    if sec < 0 or sec > 60:
        return error_response("sec 需在 0-60 之间。", 400, "bad_param")
    time.sleep(sec)
    return jsonify({"ok": True, "slept": sec})


@app.route("/diag/coze")
def diag_coze():
    """临时：用真实令牌做一次最小 stream_run，验证令牌有效性与扣子端到端耗时。"""
    import time

    token = get_token()
    if not token:
        return error_response("尚未配置 COZE_TOKEN。", 500, "no_token")

    payload = {
        "content": {"query": {"prompt": [
            {"type": "text", "content": {"text": "回答一个字：好"}}
        ]}},
        "type": "query",
        "session_id": SESSION_ID,
        "project_id": PROJECT_ID,
    }
    headers = {
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }

    t0 = time.time()
    try:
        r = requests.post(STREAM_RUN_URL, headers=headers, json=payload,
                          stream=True, timeout=(CONNECT_TIMEOUT, 20))
        status = r.status_code
        events = []
        for line in r.iter_lines(decode_unicode=True):
            line = (line or "").strip()
            if line.startswith("data:"):
                events.append(line[:200])
            if len(events) >= 3:
                break
        r.close()
        return jsonify({"ok": True, "status": status,
                        "seconds": round(time.time() - t0, 3),
                        "first_events": events})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": type(exc).__name__,
                        "message": str(exc)[:200],
                        "seconds": round(time.time() - t0, 3)})


@app.route("/diag/coze2", methods=["GET", "POST"])
def diag_coze2():
    """
    临时隔离实验：区分 GET/POST、只读前 N 个事件 / 读完整个流，
    定位到底是哪种组合会让应用挂掉。
    """
    import time

    token = get_token()
    if not token:
        return error_response("尚未配置 COZE_TOKEN。", 500, "no_token")

    mode = request.args.get("mode", "partial")

    payload = {
        "content": {"query": {"prompt": [
            {"type": "text", "content": {"text": "回答一个字：好"}}
        ]}},
        "type": "query",
        "session_id": SESSION_ID,
        "project_id": PROJECT_ID,
    }
    headers = {
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }

    t0 = time.time()
    events = []
    total_lines = 0
    status = None

    try:
        with requests.post(STREAM_RUN_URL, headers=headers, json=payload,
                           stream=True, timeout=(CONNECT_TIMEOUT, 20)) as r:
            status = r.status_code
            for line in r.iter_lines(decode_unicode=True):
                total_lines += 1
                text = (line or "").strip()
                if text.startswith("data:"):
                    events.append(text[:160])
                if mode == "partial" and len(events) >= 3:
                    break

        return jsonify({
            "ok": True,
            "mode": mode,
            "method": request.method,
            "status": status,
            "total_lines": total_lines,
            "event_count": len(events),
            "last_events": events[-3:],
            "seconds": round(time.time() - t0, 3),
        })
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "mode": mode, "method": request.method,
                        "error": type(exc).__name__, "message": str(exc)[:200],
                        "seconds": round(time.time() - t0, 3)})


@app.route("/diag/run")
def diag_run():
    """临时：直接调用 run_project，判断问题是否出在该函数内部。"""
    import time

    t0 = time.time()
    try:
        reply = run_project([
            {"type": "text", "content": {"text": "回答一个字：好"}}
        ])
        return jsonify({"ok": True, "reply": reply,
                        "seconds": round(time.time() - t0, 3)})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": type(exc).__name__,
                        "message": str(exc)[:200],
                        "seconds": round(time.time() - t0, 3)})


@app.route("/diag/status/<int:code>")
def diag_status(code):
    """临时：故意返回指定状态码，验证 Faable 网关是否会改写 502。"""
    return jsonify({"ok": False, "reply": "deliberate_status", "code": code}), code


@app.route("/diag/raw")
def diag_raw():
    """临时：抓取扣子返回的原始 SSE 全流，查看事件类型与 answer 事件。"""
    import time

    token = get_token()
    if not token:
        return error_response("尚未配置 COZE_TOKEN。", 500, "no_token")

    q = request.args.get("q", "什么是应力？请用一句话回答。")
    payload = {
        "content": {"query": {"prompt": [
            {"type": "text", "content": {"text": q}}
        ]}},
        "type": "query",
        "session_id": SESSION_ID,
        "project_id": PROJECT_ID,
    }
    headers = {
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }

    t0 = time.time()
    raw = []
    event_types = []
    status = None
    try:
        with requests.post(STREAM_RUN_URL, headers=headers, json=payload,
                           stream=True, timeout=(CONNECT_TIMEOUT, 60)) as r:
            status = r.status_code
            for line in r.iter_lines(decode_unicode=True):
                s = (line or "").strip()
                if not s:
                    continue
                raw.append(s[:600])
                if s.startswith("data:"):
                    try:
                        item = json.loads(s[5:].strip())
                        if isinstance(item, dict):
                            event_types.append(item.get("type"))
                    except Exception:  # noqa: BLE001
                        pass
        return jsonify({
            "ok": True,
            "status": status,
            "seconds": round(time.time() - t0, 3),
            "line_count": len(raw),
            "event_types": event_types,
            "raw": raw[:60],
        })
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": type(exc).__name__,
                        "message": str(exc)[:200],
                        "seconds": round(time.time() - t0, 3)})


if __name__ == "__main__":
    app.run(debug=True)
