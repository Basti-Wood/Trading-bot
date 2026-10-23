FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.lock.txt .
RUN pip install --no-cache-dir -r requirements.lock.txt
COPY app ./app
COPY dashboard ./dashboard
COPY config ./config
COPY run.py .
EXPOSE 1337
CMD ["python", "run.py"]
