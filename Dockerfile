# syntax=docker/dockerfile:1
FROM python:3.11-slim

WORKDIR /app

# Buenas prácticas / logs inmediatos
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Dependencias
# (tu requirements.txt está en una sola línea con espacios; lo normalizamos a líneas para pip)
COPY requirements.txt /app/requirements.txt
RUN python - <<'PY'
from pathlib import Path
p = Path("/app/requirements.txt")
txt = p.read_text().strip()
# Si está separado por espacios, lo pasamos a 1 paquete por línea
if txt and "\n" not in txt and " " in txt:
    p.write_text("\n".join(txt.split()) + "\n")
PY
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copiar el código
COPY . /app

# Puerto por defecto del control plane (IDP_PORT default 8080)
EXPOSE 8080

# Ejecutar el control plane
CMD ["python", "cmd/controlplane/main.py"]

