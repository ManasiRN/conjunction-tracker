# ---- Bonus visualization dashboard image (Streamlit) ----
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY dashboard/requirements.txt ./
RUN pip install -r requirements.txt

COPY dashboard/dashboard.py ./

# API base URL the dashboard queries (the api service inside the compose net).
ENV API_BASE_URL=http://api:8000

EXPOSE 8501

CMD ["streamlit", "run", "dashboard.py", \
     "--server.address=0.0.0.0", "--server.port=8501", \
     "--server.headless=true"]
