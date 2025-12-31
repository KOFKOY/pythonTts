import base64
import hashlib
import hmac
import html
import json
import logging
import time
import uuid
from datetime import datetime
from urllib.parse import quote

import requests
from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import Response
from tenacity import retry, wait_exponential, stop_after_attempt

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

#session = requests.Session()


# 常量定义
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
# audio-24khz-48kbitrate-mono-mp3
# audio-16khz-32kbitrate-mono-mp3
# audio-16khz-16kbitrate-mono-mp3
DEFAULT_OUTPUT_FORMAT = "audio-16khz-16kbitrate-mono-mp3"
DEFAULT_STYLE = "general"

endpoint = None
expired_at = None
voice_list_cache = None

app = FastAPI(title="TTS API", description="GET /tts 进行语音合成", version="1.0.0")


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


def get_endpoint(proxies=None):
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

    response = requests.post(ENDPOINT_URL, headers=headers, proxies=proxies, timeout=10)
    response.raise_for_status()
    return response.json()


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


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=5))
def get_voice(
    text: str,
    voice_name: str = "",
    rate: str = "",
    pitch: str = "",
    output_format: str = "",
    style: str = "",
    proxies=None,
) -> bytes:
    global endpoint, expired_at

    current_time = int(time.time())
    # token 过期检查
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

    # 这里可以复用 endpoint，不必每次再 get_endpoint
    # endpoint = get_endpoint(proxies)  # 如需强制刷新可打开

    url = f"https://{endpoint['r']}.tts.speech.microsoft.com/cognitiveservices/v1"
    headers = {
        "Authorization": endpoint["t"],
        "Content-Type": "application/ssml+xml",
        "X-Microsoft-OutputFormat": output_format,
    }

    ssml = get_ssml(text, voice_name, rate, pitch, style)

    response = requests.post(url, headers=headers, data=ssml.encode("utf-8"), proxies=proxies, timeout=30)
    response.raise_for_status()
    return response.content


def get_voice_list():
    """获取可用的语音列表（带简单缓存）"""
    global voice_list_cache

    if voice_list_cache is not None:
        return voice_list_cache

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/107.0.0.0 Safari/537.36 Edg/107.0.1418.26",
        "X-Ms-Useragent": "SpeechStudio/2021.05.001",
        "Content-Type": "application/json",
        "Origin": "https://azure.microsoft.com",
        "Referer": "https://azure.microsoft.com"
    }

    try:
        response = requests.get(VOICES_LIST_URL, headers=headers, timeout=10)
        response.raise_for_status()
        result = response.json()
        voice_list_cache = result
        return result
    except requests.exceptions.RequestException as e:
        logger.error(f"获取语音列表失败: {e}")
        return None


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
        logger.exception("TTS 请求失败")
        raise HTTPException(status_code=502, detail=f"TTS 服务错误: {e}")
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
