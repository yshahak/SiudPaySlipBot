FROM python:3.13-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY . .

# Download the Hebrew font at build time
RUN python scripts/download_fonts.py

ENV PYTHONUNBUFFERED=1

CMD ["python", "bot.py"]
