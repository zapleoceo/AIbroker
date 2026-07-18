FROM python:3.12-slim
WORKDIR /app
ENV HF_HOME=/app/hf-cache
COPY pyproject.toml ./
COPY src/ ./src/
RUN pip install --no-cache-dir -e .
# Model is baked into the image: startup doesn't depend on network, restart is fast.
# large-v3-turbo (2026-07-18, up from small): same encoder as large-v3 (the
# part that drives multilingual accuracy — Stepan2's leads are mostly Bahasa),
# decoder pruned 32->4 layers by OpenAI for speed. int8 keeps it under the
# 1.5GB container cap (docker-compose.yml) on this 2-core/3.7GB shared host.
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('Systran/faster-whisper-large-v3-turbo', device='cpu', compute_type='int8')"
CMD ["uvicorn", "asr_local.app:app", "--host", "0.0.0.0", "--port", "8000"]
