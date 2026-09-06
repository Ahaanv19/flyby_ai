FROM docker.io/python:3.12

WORKDIR /

# --- [Install python and pip] ---
RUN apt-get update && apt-get upgrade -y && \
    apt-get install -y python3 python3-pip git
COPY . /

RUN pip install --no-cache-dir -r requirements.txt
RUN pip install gunicorn

# Port 8659 everywhere — main.py (localhost), this container, docker-compose and
# the nginx reverse proxy all agree, so there is one number to change.
#
# One worker keeps SQLite writes in a single process (multiple processes cause
# "database is locked"), while threads give real concurrency for the slow
# outbound calls this app makes (Duffel flight search, Gemini). The long timeout
# is for those same upstream calls.
ENV GUNICORN_CMD_ARGS="--workers=1 --threads=8 --worker-class=gthread --timeout=120 --bind=0.0.0.0:8659"

EXPOSE 8659

# Define environment variable
ENV FLASK_ENV=production

CMD [ "gunicorn", "main:app" ]
