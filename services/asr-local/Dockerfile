FROM python:3.12-slim
WORKDIR /app
ENV HF_HOME=/app/hf-cache
COPY pyproject.toml ./
COPY src/ ./src/
RUN pip install --no-cache-dir -e .
# Model is baked into the image: startup doesn't depend on network, restart is fast.
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('small', device='cpu', compute_type='int8')"
CMD ["uvicorn", "asr_local.app:app", "--host", "0.0.0.0", "--port", "8000"]
