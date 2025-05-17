# Use an official Python runtime as the base image
FROM python:3.11-slim

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file into the container
COPY requirements.txt .

# Install the application dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code to the container
COPY . .

ENV PORT=2397
ENV HOST=0.0.0.0
ENV WORKERS=2

# Command to run the application
CMD gunicorn --bind ${HOST}:${PORT} --workers ${WORKERS} --graceful-timeout 5 --timeout 5 main:app
