# Use a slim Python image and install required build tools
FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Install system packages required for some Python dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    git \
    && rm -rf /var/lib/apt/lists/*

# Copy application requirements and install Python dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source into the container
COPY . ./

# Expose Streamlit default port
EXPOSE 8501

# Default command to run the Streamlit app
CMD ["streamlit", "run", "complete_rag_appV1.0.py", "--server.port=8501", "--server.address=0.0.0.0"]
