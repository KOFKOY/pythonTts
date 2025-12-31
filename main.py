import base64
import hashlib
import hmac
import html
import json
import logging
import time
import uuid
from datetime import datetime
from typing import Optional, Dict
from urllib.parse import quote

import requests
from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import Response
from tenacity import (
    retry,
    wait_exponential,
    stop_after_attempt,
    retry_if_exception_type,
)
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ================== 日志配置 ==================
logging.basicConfig(level=logging.INFO)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


# ================== 常量定义 ==================
ENDPOINT_URL = "https://dev.microsofttranslator.com/apps/endpoint?api-version=1.0"
VOICES_LIST_URL = "https://eastus.api.speech.microsoft.com/cognitiveservices/voices/list"
USER_AGENT = "okhttp/4.5.0"
CLIENT_VERSION = "4.0.530a 5fe1dc6c"
USER_ID = "0f04d16a175c411e"
HOME_GEOGRAPHIC_REGION = "zh-Hans-CN"
CLIENT_TRACE_ID = "aab069b9-70a7-4844-a734-96cd78d94be9"
VOICE_DECODE_KEY = "oik6PdDdMnOXemTbwvMn9de/h9lFnfBaCWbGMMZqqoSaQaqUOqjVGm5NqsmjcBI1x+sS9ugjB55HEJWRiFXYFw=="
DEFAULT_VOICE_NAME = "zh-CN-XiaoxiaoMultilingualNeural"
DEFAULT_RATE = "0"
DEFAULT_PITCH = "0"
DEFAULT_OUTPUT_FORMAT = "audio-16khz-32kbitrate-mono-mp3"
DEFAULT_STYLE = "general"

# 如果有代理，在这里统一配置；没有就设为 None
GLOBAL_PROXIES: Optional[Dict[str, str]] = None
# GLOBAL_PROXIES = {
#     "http": "http://user:pass@proxy_host:proxy_port",
#     "https": "http://user:pass@proxy_host:proxy_port",
# }

# ================== 全局状态 ==================
endpoint: Optional[dict] = None
expired_at: Optional[int] = None
voice_list_cache = None

app = FastAPI(title="TTS API", description="GET /tts 进行语音合成", version="1.0.0")


# ================== Session 管理（核心优化） ==================
def create_session(max_retries: int = 5) -> requests.Session:
    """
    创建带重试和连接池的 Session，专门给 TTS / endpoint / voices 用。
    解决偶发 SSLEOFError / 连接中断问题。
    """
    session = requests.Session()

    retry_cfg = Retry(
        total=max_retries,
        read=max_retries,
        connect=max_retries,
        backoff_factor=0.5,  # 0.5, 1, 2, 4, ...
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["HEAD", "GET", "OPTIONS", "POST"],
        raise_on_status=False,
        respect_retry_after_header=True,
    )

    adapter = HTTPAdapter(
        max_retries=retry_cfg,
        pool_connections=20,
        pool_maxsize=50,
    )

    session.mount("https://", adapter)
    session.mount("http://", adapter)

    return session


class SessionManager:
    """
    管理带重试的 Session，并定期重建，避免复用“老掉线连接”。
    """

    def __init__(self, recreate_interval_sec: int = 600):
        self._session = create_session()
        self._last_created = time.time()
        self._recreate_interval = recreate_interval_sec

    @property
    def session(self) -> requests.Session:
        now = time.time()
        if now - self._last_created > self._recreate_interval:
            try:
                self._session.close()
            except Exception:
                pass
            self._session = create_session()
            self._last_created = now
            logger.info("重建 requests Session，避免复用过期连接")
        return self._session


session_manager = SessionManager()


# ================== 签名 & SSML ==================
def sign(url_str: str) -> str:
    u = url_str.split("://")[1]
    encoded_url = quote(u, safe='')
    uuid_str = str(uuid.uuid4()).replace("-", "")
    formatted_date = datetime.utcnow().strftime(
        "%a, %d %b %Y %H:%M:%S").lower() + "gmt"
    bytes_to_sign = f"MSTranslatorAndroidApp{encoded_url}{formatted_date}{uuid_str}".lower().encode('utf-8')

    decode = base64.b64decode(VOICE_DECODE_KEY)
    hmac_sha256 = hmac.new(decode, bytes_to_sign, hashlib.sha256)
    secret_key = hmac_sha256.digest()
    sign_base64 = base64.b64encode(secret_key).decode()

    return f"MSTranslatorAndroidApp::{sign_base64}::{formatted_date}::{uuid_str}"


def get_ssml(text: str, voice_name: str, rate: str, pitch: str, style: str) -> str:
    # 简单转义文本，避免 SSML 注入
    safe_text = html.escape(text)
    return f"""
<speak xmlns="http://www.w3.org/2001/10/synthesis" xmlns:mstts="http://www.w3.org/2001/mstts" version="1.0" xml:lang="zh-CN">
  <voice name="{voice_name}">
    <mstts:express-as style="{style}" styledegree="1.0" role="default">
      <prosody rate="{rate}%" pitch="{pitch}%">
        {safe_text}
      </prosody>
    </mstts:express-as>
  </voice>
</speak>
""".strip()


# ================== 调 endpoint 拿 token ==================
@retry(
    retry=retry_if_exception_type((
        requests.exceptions.SSLError,
        requests.exceptions.ConnectionError,
        requests.exceptions.Timeout,
    )),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=5),
    reraise=True,
)
def get_endpoint(proxies=None):
    """
    获取 endpoint + token，带网络层重试。
    """
    signature = sign(ENDPOINT_URL)
    headers = {
        "Accept-Language": "zh-Hans",
        "X-ClientVersion": CLIENT_VERSION,
        "X-UserId": USER_ID,
        "X-HomeGeographicRegion": HOME_GEOGRAPHIC_REGION,
        "X-ClientTraceId": CLIENT_TRACE_ID,
        "X-MT-Signature": signature,
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json; charset=utf-8",
        "Content-Length": "0",
        "Accept-Encoding": "gzip",
    }

    session = session_manager.session
    resp = session.post(
        ENDPOINT_URL,
        headers=headers,
        proxies=proxies or GLOBAL_PROXIES,
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


# ================== 主 TTS 调用 ==================
@retry(
    retry=retry_if_exception_type((
        requests.exceptions.SSLError,
        requests.exceptions.ConnectionError,
        requests.exceptions.Timeout,
    )),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=5),
    reraise=True,
)
def get_voice(
    text: str,
    voice_name: str = "",
    rate: str = "",
    pitch: str = "",
    output_format: str = "",
    style: str = "",
    proxies=None,
) -> bytes:
    """
    调用 TTS，带网络层重试。
    只对网络类异常重试，避免逻辑错误被无限重试。
    """
    global endpoint, expired_at

    current_time = int(time.time())
    # token 过期检查（提前 60 秒刷新）
    if not expired_at or current_time > expired_at - 60:
        endpoint = get_endpoint(proxies)
        jwt = endpoint['t'].split('.')[1]
        # 补齐 base64 padding
        padding = '=' * (-len(jwt) % 4)
        decoded_jwt = json.loads(base64.b64decode(jwt + padding).decode('utf-8'))
        expired_at = decoded_jwt['exp']
        seconds_left = expired_at - current_time
        logger.info(f"刷新 token，剩余有效期 {seconds_left} 秒")
    else:
        seconds_left = expired_at - current_time
        logger.info(f"沿用 token，剩余有效期 {seconds_left} 秒")

    voice_name = voice_name or DEFAULT_VOICE_NAME
    rate = rate or DEFAULT_RATE
    pitch = pitch or DEFAULT_PITCH
    output_format = output_format or DEFAULT_OUTPUT_FORMAT
    style = style or DEFAULT_STYLE

    url = f"https://{endpoint['r']}.tts.speech.microsoft.com/cognitiveservices/v1"
    headers = {
        "Authorization": endpoint["t"],
        "Content-Type": "application/ssml+xml",
        "X-Microsoft-OutputFormat": output_format,
    }

    ssml = get_ssml(text, voice_name, rate, pitch, style)

    session = session_manager.session
    resp = session.post(
        url,
        headers=headers,
        data=ssml.encode("utf-8"),
        proxies=proxies or GLOBAL_PROXIES,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.content


# ================== 语音列表 ==================
def get_voice_list():
    """获取可用的语音列表（带简单缓存）"""
    global voice_list_cache

    if voice_list_cache is not None:
        return voice_list_cache

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/107.0.0.0 Safari/537.36 Edg/107.0.1418.26"
        ),
        "X-Ms-Useragent": "SpeechStudio/2021.05.001",
        "Content-Type": "application/json",
        "Origin": "https://azure.microsoft.com",
        "Referer": "https://azure.microsoft.com",
    }

    session = session_manager.session
    try:
        resp = session.get(
            VOICES_LIST_URL,
            headers=headers,
            timeout=10,
            proxies=GLOBAL_PROXIES,
        )
        resp.raise_for_status()
        result = resp.json()
        voice_list_cache = result
        return result
    except requests.exceptions.RequestException as e:
        logger.error(f"获取语音列表失败: {e}", exc_info=True)
        return None


# ================== FastAPI 路由 ==================
@app.get("/tts")
def tts_api(
    text: str = Query(..., description="要合成的文本"),
    voice_name: str = Query(DEFAULT_VOICE_NAME, description="语音名称"),
    rate: str = Query(DEFAULT_RATE, description="语速百分比，如 -20, 0, 20"),
    pitch: str = Query(DEFAULT_PITCH, description="音调百分比，如 -20, 0, 20"),
    output_format: str = Query(DEFAULT_OUTPUT_FORMAT, description="输出格式"),
    style: str = Query(DEFAULT_STYLE, description="说话风格"),
):
    """
    使用 GET 请求进行 TTS：
    /tts?text=你好&voice_name=zh-CN-XiaoxiaoMultilingualNeural
    """
    if not text.strip():
        raise HTTPException(status_code=400, detail="text 不能为空")

    try:
        audio_bytes = get_voice(
            text=text,
            voice_name=voice_name,
            rate=rate,
            pitch=pitch,
            output_format=output_format,
            style=style,
        )
    except requests.HTTPError as e:
        # 这里是 HTTP 状态码错误（4xx/5xx），不再重试
        logger.exception("TTS 请求失败（HTTPError）")
        raise HTTPException(status_code=502, detail=f"TTS 服务错误: {e}")
    except requests.exceptions.SSLError as e:
        logger.exception("TTS 请求失败（SSLError，多次重试后仍失败）")
        raise HTTPException(status_code=502, detail="TTS 网络错误，请稍后重试")
    except requests.exceptions.RequestException as e:
        logger.exception("TTS 请求失败（网络相关异常）")
        raise HTTPException(status_code=502, detail="TTS 网络错误，请稍后重试")
    except Exception as e:
        logger.exception("TTS 未知错误")
        raise HTTPException(status_code=500, detail=f"服务器内部错误: {e}")

    # 根据 output_format 简单判断 Content-Type
    if "mp3" in output_format:
        media_type = "audio/mpeg"
    elif "ogg" in output_format:
        media_type = "audio/ogg"
    elif "wav" in output_format:
        media_type = "audio/wav"
    else:
        media_type = "application/octet-stream"

    return Response(content=audio_bytes, media_type=media_type)


@app.get("/voices")
def voices_api():
    """获取语音列表"""
    voices = get_voice_list()
    if voices is None:
        raise HTTPException(status_code=502, detail="获取语音列表失败")
    return voices
