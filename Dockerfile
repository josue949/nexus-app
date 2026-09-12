FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Install system dependencies (if any)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy requirement file and install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY . .

# Expose Streamlit default port
EXPOSE 8501

# Ensure a persistent volume for SQLite (if you keep using it)
VOLUME /data

# Run the Streamlit app
CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.headless=true"]
