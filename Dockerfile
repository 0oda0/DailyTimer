FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY dailytimer ./dailytimer
ENV DAILYTIMER_DATA=/data DAILYTIMER_HOST=0.0.0.0 DAILYTIMER_PORT=8080 PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8080
CMD ["python", "-m", "dailytimer"]
