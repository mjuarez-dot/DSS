FROM python:3.10
ENV PYTHONUNBUFFERED=1
WORKDIR /web/
COPY docker/requirements.txt /web/
RUN pip install -r requirements.txt
RUN apt-get update -y && apt-get install -yq \
    build-essential \
    libssl-dev \
    libpq-dev
RUN pip install psycopg2-binary --no-binary psycopg2-binary     
COPY . /web/