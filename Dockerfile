# --- Restore helper CT template (direction A): Alpine + python3 + dnsmasq + the
#     probe/DHCP agent, baked in and started on boot. Built here so the app can
#     upload it to PVE via the API — nothing for the operator to prepare.
FROM alpine:3.20 AS helper
RUN apk add --no-cache python3 dnsmasq iproute2 iputils openrc
COPY app/services/restore_helper_agent.py /opt/restore-helper-agent.py
RUN printf '#!/sbin/openrc-run\ncommand="/usr/bin/python3"\ncommand_args="/opt/restore-helper-agent.py"\ncommand_background=true\npidfile="/run/restore-helper.pid"\n' \
      > /etc/init.d/restore-helper \
 && chmod +x /etc/init.d/restore-helper \
 && rc-update add restore-helper default \
 && rc-update add dnsmasq default \
 && rc-update add networking boot \
 # Pack the whole (configured) rootfs as the CT template; drop virtual/volatile dirs.
 && tar --exclude=./restore-helper.tar.gz --exclude=./proc --exclude=./sys --exclude=./dev \
        -C / -czf /restore-helper.tar.gz .

FROM python:3.14-slim

WORKDIR /app

# Create non-root user
RUN groupadd --system --gid 1000 app \
    && useradd --system --uid 1000 --gid app --no-create-home app

COPY requirements.txt requirements-nodeps.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir --no-deps -r requirements-nodeps.txt

COPY app ./app
COPY templates ./templates
COPY static ./static
COPY scripts ./scripts
# The restore helper template, built above; the app uploads it to PVE on demand.
COPY --from=helper /restore-helper.tar.gz /app/assets/restore-helper.tar.gz

# Data and report directories owned by non-root user
RUN mkdir -p /data /reports && chown -R app:app /data /reports /app

ENV BSENTINEL_DATA_DIR=/data \
    BSENTINEL_REPORT_DIR=/reports \
    PYTHONUNBUFFERED=1

USER app

EXPOSE 80

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "80"]
