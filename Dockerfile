# --- Restore helper CT template (direction A): Alpine + python3 + dnsmasq + the
#     probe/DHCP agent, baked in and started on boot. Built here so the app can
#     upload it to PVE via the API — nothing for the operator to prepare.
FROM alpine:3.20 AS helper
COPY app/services/restore_helper_agent.py /agent.py
RUN mkdir -p /rootfs \
 && apk add --no-cache --root /rootfs --initdb \
      --repository https://dl-cdn.alpinelinux.org/alpine/v3.20/main \
      alpine-base python3 dnsmasq iproute2 iputils openrc \
 && cp /agent.py /rootfs/opt/restore-helper-agent.py \
 && printf '#!/sbin/openrc-run\ncommand="/usr/bin/python3"\ncommand_args="/opt/restore-helper-agent.py"\ncommand_background=true\npidfile="/run/restore-helper.pid"\n' \
      > /rootfs/etc/init.d/restore-helper \
 && chmod +x /rootfs/etc/init.d/restore-helper \
 && mkdir -p /rootfs/etc/runlevels/default \
 && ln -sf /etc/init.d/restore-helper /rootfs/etc/runlevels/default/restore-helper \
 && ln -sf /etc/init.d/dnsmasq /rootfs/etc/runlevels/default/dnsmasq \
 && tar -C /rootfs -czf /restore-helper.tar.gz .

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
