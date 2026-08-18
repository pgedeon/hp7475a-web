# HP 7475A web controller — runtime image (Python 3.12 for vpype 1.15)
FROM python:3.12-slim

WORKDIR /app

# Backend (installs app package + deps incl. vpype)
COPY backend ./backend
RUN pip install --no-cache-dir -e backend

# Frontend bundle (built beforehand: cd frontend && npm run build)
COPY frontend/dist ./backend/app/frontend_dist

ENV HP7475A_HOST=0.0.0.0 \
    HP7475A_PORT=8750 \
    HP7475A_DATA_DIR=/app/data

VOLUME ["/app/data"]
EXPOSE 8750

# Serial device is attached at run time:
#   docker run --device=/dev/ttyUSB0 ...
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8750"]
