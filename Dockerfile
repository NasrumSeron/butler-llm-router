FROM python:3.11-slim

# Don't buffer stdout — otherwise `docker logs` shows nothing until the buffer
# fills, which makes a hanging bot look identical to a working one.
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Copy requirements first so Docker caches the pip layer and a code change
# doesn't reinstall every package.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run as a non-root user. Butler has no business being root inside its own
# container, and this costs nothing.
RUN useradd --create-home --uid 1000 butler && chown -R butler:butler /app
USER butler

CMD ["python", "main.py"]
