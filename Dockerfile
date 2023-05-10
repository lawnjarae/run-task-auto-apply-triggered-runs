FROM python:3.11-slim as builder

RUN mkdir /install
WORKDIR /install

COPY requirements.txt /requirements.txt

RUN pip install --no-cache-dir --prefix /install -r /requirements.txt

FROM python:3.11-slim as final

COPY --from=builder /install /usr/local

WORKDIR /app
COPY . .

EXPOSE 8000

CMD exec gunicorn --threads 12 -b 0.0.0.0:8000 run_task:app