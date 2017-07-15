FROM alpine:latest

RUN mkdir /app

COPY requirements.txt /app/

WORKDIR /app

RUN apk add --no-cache bash \
    python-dev \
    build-base \
  && python -m ensurepip \
  && pip install --upgrade pip \
  && pip install virtualenv \
  && virtualenv -p python venv \
  && source venv/bin/activate \
  && venv/bin/pip install -r requirements.txt

COPY app.py \
     docker-compose-env.yml.template \
     docker-compose-provision.yml.template \
     docker_compose.py \
     startup.sh /app/

FROM docker:latest

MAINTAINER Mark Watson <markwatsonatx@gmail.com>

RUN apk add --no-cache bash \
    python

COPY --from=0 /app /app

WORKDIR /app

CMD ["./startup.sh"]