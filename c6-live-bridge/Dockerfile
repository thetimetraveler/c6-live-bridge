FROM python:3.13-alpine
WORKDIR /app
COPY app.py index.html ./
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8099
CMD ["python3", "/app/app.py"]
