FROM python:3.12-slim
WORKDIR /app
ENV HF_HOME=/app/hf-cache
COPY pyproject.toml ./
COPY src/ ./src/
RUN pip install --no-cache-dir -e .
# Model is baked into the image: startup doesn't depend on network, restart is fast.
# STAYS on small (2026-07-18): tried bumping to large-v3-turbo and medium —
# both OOM-killed (exit 137) loading directly on this host, even in an
# unconstrained throwaway container, well before the app's own 1.5GB
# mem_limit came into play. Swap was already 100% full at test time (no
# headroom for the transient peak during download+int8 quantization, which
# runs meaningfully higher than the final resident size). See
# docs/deploy-ops.md "Local ASR" for the numbers. Revisit if this host gets
# more RAM or a dedicated one is stood up for asr-local.
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('small', device='cpu', compute_type='int8')"
CMD ["uvicorn", "asr_local.app:app", "--host", "0.0.0.0", "--port", "8000"]
