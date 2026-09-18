FROM python:3.11-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
ENV PORT=8000 GEMINI_API_KEY="" LLM_MODEL="gemini-3.6-flash"
CMD ["sh","-c","uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]