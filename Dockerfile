# Use an official Python runtime as the base image
FROM python:3.11-slim

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file into the container
COPY requirements.txt .

# Install the application dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code to the container
COPY main.py engine.py chapar_api.py ./

# Scripts run as this user, never as root.
RUN useradd --system --no-create-home --uid 10001 chapar
USER 10001

ENV PORT=2397
ENV HOST=0.0.0.0
ENV WORKERS=2
ENV PYTHONDONTWRITEBYTECODE=1

# A script is killed after its own timeout (at most 60s); the worker
# timeout only has to outlast that.
CMD gunicorn --bind ${HOST}:${PORT} --workers ${WORKERS} --graceful-timeout 5 --timeout 75 main:app
