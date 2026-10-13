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

# 会话 ID：
# 旧的硬编码会话（1Gl-6qs58oTnbmYuwJ14d）内部残留了一张无法解码的图片，
# 复用它会让每次运行都稳定报 "image data 0 failed: Unable to decode the image"，
# 连纯文字提问也会失败。因此默认每次调用都生成全新会话，避免历史会话状态污染；
# 如确需固定会话，可通过环境变量 COZE_SESSION_ID 指定。
FIXED_SESSION_ID = os.environ.get("COZE_SESSION_ID", "").strip() or None

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
#   - 连接超时设短：网络不通 / 被限制时快速失败并返回明确 JSON 错误；
#   - 读取超时放宽：给模型生成回答留出时间。
CONNECT_TIMEOUT = 5
READ_TIMEOUT = 120
REQUEST_TIMEOUT = (CONNECT_TIMEOUT, READ_TIMEOUT)

# 重要（已在线上实测确认）：Faable 网关会把应用返回的 502 / 503 / 504
# 一律改写为它自己的 {"error":"APP_UNREACHABLE"}，把我们的错误信息吞掉。
# 因此所有「服务端已处理并给出明确原因」的错误统一使用 500，确保能透传到前端。
SERVER_ERROR_STATUS = 500


class CozeError(Exception):
    """调用扣子接口失败时抛出，携带应返回给前端的 HTTP 状态码。"""

    def __init__(self, message, status_code=SERVER_ERROR_STATUS):
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
        return "扣子拒绝访问（403）：令牌缺少所需权限，请在扣子开放平台为令牌勾选对应权限。"
    if status == 404:
        return "扣子接口不存在（404）：请确认项目域名与项目 ID 是否正确。"
    if status == 429:
        return ("扣子接口触发限流（429）：模型调用额度已用尽或请求过于频繁，"
                "请在扣子账户中提高额度，或稍后重试。")
    if status >= 500:
        return "扣子服务异常（%d）：%s" % (status, detail or "请稍后重试。")
    return "扣子接口返回错误（%d）：%s" % (status, detail or "请稍后重试。")


def friendly_coze_error(raw):
    """
    把扣子返回的原始错误翻译成用户能读懂的中文提示。

    重点场景：账户模型额度用尽（SetLimitExceeded / 429）。
    """
    text = str(raw)
    lowered = text.lower()
    if ("setlimit exceeded" in lowered.replace(" ", "")
            or "usage limit" in lowered
            or "ratelimiterror" in lowered
            or "429" in text):
        return ("扣子账户的模型使用额度已用尽（模型 deepseek-v4-1-flash）。"
                "这不是程序故障：请在扣子编程 / 扣子开放平台中提高或重置该模型的使用限额，"
                "额度恢复后即可继续问答与识题。")
    return "扣子返回错误：%s" % text[:200]


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
        raise CozeError("尚未配置扣子令牌（COZE_TOKEN），无法上传图片。",
                        SERVER_ERROR_STATUS)

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
        raise CozeError("上传图片到扣子超时，请稍后重试。", SERVER_ERROR_STATUS)
    except requests.RequestException as exc:
        raise CozeError("无法连接扣子文件上传接口：%s" % exc, SERVER_ERROR_STATUS)

    if response.status_code >= 400:
        raise CozeError(describe_http_error(response), SERVER_ERROR_STATUS)

    try:
        payload = response.json()
    except ValueError:
        raise CozeError("扣子文件上传接口返回了无法解析的内容。", SERVER_ERROR_STATUS)

    image_url = _extract_url(payload)
    if not image_url:
        logger.error("上传接口未返回图片 URL，响应键：%s",
                     list(payload.keys()) if isinstance(payload, dict) else type(payload))
        raise CozeError("扣子文件上传接口未返回图片地址，图片未能进入识题流程。",
                        SERVER_ERROR_STATUS)

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
        raise CozeError("尚未配置扣子令牌（COZE_TOKEN），请检查环境变量。",
                        SERVER_ERROR_STATUS)

    payload = {
        "content": {
            "query": {
                "prompt": prompt_items
            }
        },
        "type": "query",
        # 每次调用使用全新会话，避免复用历史会话导致的图片状态污染
        "session_id": FIXED_SESSION_ID or uuid.uuid4().hex,
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
                raise CozeError(describe_http_error(response), SERVER_ERROR_STATUS)

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

                elif item_type == "message_end" and isinstance(content, dict):
                    # 扣子把运行失败原因放在 message_end.content.message_end.message
                    end_info = content.get("message_end")
                    if isinstance(end_info, dict) and end_info.get("message"):
                        error_message = end_info.get("message")
                        logger.error("扣子 message_end 错误：%s", error_message)

    except requests.Timeout:
        raise CozeError("调用扣子接口超时，请稍后重试。", SERVER_ERROR_STATUS)
    except requests.RequestException as exc:
        raise CozeError("无法连接扣子运行接口：%s" % exc, SERVER_ERROR_STATUS)

    reply = "".join(answer_parts).strip()

    if not reply and error_message:
        raise CozeError(friendly_coze_error(error_message), SERVER_ERROR_STATUS)

    return reply


@app.after_request
def no_cache_html(response):
    """
    页面 HTML 不做缓存：避免浏览器长期沿用旧版前端，
    导致「旧前端 + 新后端」时出现奇怪的前端解析报错。
    """
    if response.mimetype == "text/html":
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


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
        return error_response("没有获取到回答，请稍后重试。",
                              SERVER_ERROR_STATUS, "empty_answer")

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
        return error_response("图片不能超过 10 MB。", 400, "file_too_large")

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
        return error_response("图片已提交，但模型没有返回内容，请稍后重试。",
                              SERVER_ERROR_STATUS, "empty_answer")

    logger.info("图片识题完成：filename=%s has_text=%s", filename, bool(message))
    return success_response(reply)


@app.errorhandler(413)
def file_too_large(error):
    return error_response("上传文件过大，请选择 10 MB 以内的文件。", 400, "file_too_large")


@app.errorhandler(Exception)
def handle_unexpected(error):
    """兜底：任何未捕获异常都返回 JSON，绝不把 HTML 错误页抛给前端。"""
    from werkzeug.exceptions import HTTPException

    if isinstance(error, HTTPException):
        return error

    logger.exception("未捕获异常")
    return error_response("服务器内部错误，请稍后重试。",
                          SERVER_ERROR_STATUS, "internal_error")


if __name__ == "__main__":
    app.run(debug=True)
