FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY payroll/app.pyc ./payroll/app.pyc
COPY dispatch/app.pyc ./dispatch/app.pyc
COPY finance/app.pyc ./finance/app.pyc
COPY seed.bin ./seed.bin
ENV SEED_BUNDLE=/srv/seed.bin \
    PAYROLL_DB=/data/payroll.db
VOLUME /data
