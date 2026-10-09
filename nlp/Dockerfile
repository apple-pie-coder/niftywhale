# The news desk's sentiment scorer (server.py): FinBERT on onnxruntime, CPU only.
FROM python:3.12-slim
RUN pip install --no-cache-dir onnxruntime==1.20.1 tokenizers==0.21.0 numpy==2.2.1
WORKDIR /app
COPY model /model
COPY server.py .
RUN useradd --uid 1000 --create-home nlp
USER nlp
ENV PYTHONUNBUFFERED=1 MODEL_DIR=/model PORT=8000
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5).status == 200 else 1)"
CMD ["python", "server.py"]
