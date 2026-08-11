# Pull base image
FROM python:3.14

# Install system dependencies for GeoDjango
RUN apt-get update && apt-get install --no-install-recommends -y \
    binutils \
    libproj-dev \
    gdal-bin \
    libgdal-dev \
    && rm -rf /var/lib/apt/lists/*

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIPENV_PYTHON=/usr/local/bin/python

# Set work directory
WORKDIR /code

# Install dependencies
COPY Pipfile Pipfile.lock /code/
RUN pip install pipenv && pipenv install --system --deploy

# Copy the full project
COPY . /code/

ENV SECRET_KEY=build-time-dummy
RUN python manage.py collectstatic --noinput --clear