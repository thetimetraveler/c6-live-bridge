FROM python:3.13-alpine
WORKDIR /app
# zoneinfo needs the system time zone database to evaluate shows in local time.
RUN apk add --no-cache tzdata
COPY app.py index.html ./
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8099
CMD ["python3", "/app/app.py"]
