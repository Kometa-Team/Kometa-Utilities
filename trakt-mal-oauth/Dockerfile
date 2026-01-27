# Set base image (host OS)
FROM python:3.12-alpine

# By default, listen on port 5000
EXPOSE 5000/tcp

# Set the working directory in the container
WORKDIR /app

# Copy the content of the local src directory to the working directory
COPY . .

# Install any dependencies
RUN pip install -r requirements.txt

# Specify the command to run on container start
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "app:app"]
