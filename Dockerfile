# Built ON the NAS (x86_64) over ssh: docker build there is native, and the
# laptop needs no docker at all. --platform pins the architecture anyway so a
# build anywhere else still produces an image the DS220+ can run.
# The database lives on a bind-mounted /data — mounted explicitly at `docker
# run`, no VOLUME line here: an anonymous volume would make a forgotten mount
# look healthy, and create_app refuses that instead. The token arrives from
# the environment and never from this image.
FROM --platform=linux/amd64 python:3.12-slim

RUN pip install --no-cache-dir "fastapi>=0.115,<1" "uvicorn>=0.30,<1"

WORKDIR /app
COPY butler.py schema.sql /app/

EXPOSE 9380

CMD ["uvicorn", "butler:create_app", "--factory", "--host", "0.0.0.0", "--port", "9380"]
