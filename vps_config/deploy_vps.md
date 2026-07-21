# Deploy ScoreSync en VPS — paso a paso

Ejecutar todos los comandos conectado por SSH como `jchelquer`. Mismo
esquema que ensayos/afinacion/tempo/infedu-jch (subdominio propio, gunicorn
+ nginx, Postgres compartido) — ver [[ecosystem_shared_auth]] si hace falta
repasar el patrón general.

---

## 1. Instalar dependencias del sistema

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip
```

`opencv-python-headless` y `pymupdf` instalan como wheels precompilados
(no hace falta compilar nada), pero **si `import cv2` falla en runtime**
con un error de librerías compartidas faltantes (`libGL.so.1` y
similares — pasa en imágenes mínimas de Debian/Ubuntu aunque sea la
variante "headless"), instalar:

```bash
sudo apt install -y libglib2.0-0 libsm6 libxext6 libxrender1
```

---

## 2. Crear la estructura del proyecto

```bash
sudo mkdir -p /var/www/scoresync
sudo chown jchelquer:www-data /var/www/scoresync
```

---

## 3. Subir el proyecto

Desde la laptop (Git Bash o WSL), reemplazando `IP_VPS`:

```bash
rsync -av --exclude='venv' --exclude='__pycache__' --exclude='*.pyc' \
      --exclude='.env' --exclude='media' --exclude='muestras' \
      scoresync/ jchelquer@IP_VPS:/var/www/scoresync/
```

Subir el `.env` **por separado** (nunca por rsync/git):

```bash
scp scoresync/.env jchelquer@IP_VPS:/var/www/scoresync/.env
```

Y agregar la línea de producción al `.env` ya en la VPS:

```bash
echo "DJANGO_ENV=production" >> /var/www/scoresync/.env
```

**Importante**: `SSO_SECRET` en este `.env` tiene que ser **exactamente
el mismo valor** que el de ensayos/afinacion/tempo — es el secreto
compartido que firma los tokens de SSO entre apps. Si no coincide, el
login cruzado entre apps falla en silencio (redirige pero no loguea a
nadie).

---

## 4. Crear entorno virtual e instalar dependencias

```bash
cd /var/www/scoresync
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
pip install gunicorn
```

---

## 5. Base de datos

ScoreSync **no crea una base propia** — se conecta a la misma Postgres
compartida que ya usan ensayos/afinacion/tempo (`DB_NAME=ensayos` por
default, ver `config/settings.py`). Sólo hace falta correr las
migraciones de las apps propias de ScoreSync contra esa base ya
existente:

```bash
cd /var/www/scoresync
source venv/bin/activate
python manage.py migrate
```

No hace falta `createsuperuser` — los usuarios ya existen en la tabla
compartida `usuarios_usuario`.

---

## 6. Recolectar archivos estáticos

```bash
cd /var/www/scoresync
source venv/bin/activate
python manage.py collectstatic --noinput
```

Esto genera `/var/www/scoresync/staticfiles/` **con el manifest** que
WhiteNoise necesita (`CompressedManifestStaticFilesStorage` — ver
`config/settings.py`) — sin este paso, cualquier `{% static %}` de un
archivo que no esté en el manifest tira error 500, no un 404 silencioso.

---

## 7. Permisos de carpetas

```bash
sudo chown -R jchelquer:www-data /var/www/scoresync
sudo chmod -R 755 /var/www/scoresync
sudo mkdir -p /var/www/scoresync/media
sudo chown -R jchelquer:www-data /var/www/scoresync/media
```

---

## 8. Instalar el servicio gunicorn

Los logs de gunicorn van a `/var/log/scoresync/` — **no** a `/var/log/nginx/`
(esa carpeta es de nginx, `root:adm`, jchelquer no puede escribir ahí; si
se salta este paso el servicio queda en crash-loop con
`PermissionError` al abrir el logfile — pasó de verdad la primera vez).

```bash
sudo mkdir -p /var/log/scoresync
sudo chown jchelquer:www-data /var/log/scoresync

sudo cp /var/www/scoresync/vps_config/scoresync.service /etc/systemd/system/scoresync.service

sudo systemctl daemon-reload
sudo systemctl enable scoresync
sudo systemctl start scoresync

sudo systemctl status scoresync
```

---

## 9. Instalar la config de nginx (HTTP, sin SSL todavía)

```bash
sudo cp /var/www/scoresync/vps_config/scoresync.conf /etc/nginx/sites-available/scoresync.conf
sudo ln -s /etc/nginx/sites-available/scoresync.conf /etc/nginx/sites-enabled/scoresync.conf

sudo nginx -t
sudo systemctl reload nginx
```

En este punto `http://scoresync.infedu.com.ar` ya debería responder
(sin HTTPS todavía) — vale la pena probarlo antes de seguir con certbot.

---

## 10. Obtener certificado SSL para el subdominio

```bash
sudo certbot --nginx -d scoresync.infedu.com.ar
```

> Certbot busca un bloque de nginx ya instalado para ese dominio y lo
> edita in-place agregando `listen 443 ssl` + el redirect HTTP→HTTPS —
> **por eso este paso va DESPUÉS del 9, no antes**: sin un bloque ya
> instalado para `scoresync.infedu.com.ar`, certbot no tiene qué editar.
> Si se prefiere manual, usar `certonly` y apuntar los paths a mano.

---

## 11. Verificación final

```bash
sudo systemctl status scoresync
sudo nginx -t

sudo journalctl -u scoresync -f
sudo tail -f /var/log/nginx/scoresync.error.log
```

Probar en el navegador: subir un PDF chico, correr "Enderezar PDF" y
confirmar que no tarda más que el `proxy_read_timeout`/`--timeout`
configurados (ver notas en `scoresync.conf`/`scoresync.service`).

---

## Actualizaciones futuras

```bash
cd /var/www/scoresync
source venv/bin/activate
# subir archivos nuevos con rsync...
python manage.py migrate                    # sólo si hay migraciones nuevas
python manage.py collectstatic --noinput    # sólo si cambió algún estático — ver nota abajo
sudo systemctl restart scoresync
```

**No te olvides de `collectstatic` cuando cambia CUALQUIER estático**
(un logo nuevo, un favicon, un CSS) — con `ManifestStaticFilesStorage`,
un archivo que no pasó por `collectstatic` simplemente no existe para
`{% static %}`, aunque el template ya esté desplegado y el archivo ya
esté en el filesystem. Esto ya pasó de verdad en Tempo (footer con logo
nuevo commiteado y pusheado, pero sin `collectstatic` en el servidor) —
el síntoma es "el HTML ya cambió pero la imagen no aparece", no un error
visible.

## Pendiente, no bloqueante

- **SSO cruzado**: ninguna app hermana tiene todavía a ScoreSync en su
  `APPS` dict de `sso.py` (ver `ensayos/usuarios/sso.py`), así que por
  ahora no hay botón para saltar DESDE ensayos/afinación/tempo/infedu-jch
  HACIA ScoreSync ya logueado. Agregar `'scoresync': 'https://scoresync.infedu.com.ar'`
  a cada una cuando se quiera esa integración — no hace falta para que
  ScoreSync funcione solo.
