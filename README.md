## 启动
uv run uvicorn main:app --host 0.0.0.0 --port 8000


## tts
ip:port/tts?text={{java.encodeURI(speakText)}}&voice_name=zh-CN-XiaoxiaoMultilingualNeural&style=storytelling&rate=8

## 语音列表
ip:port/voices