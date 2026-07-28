# Exportar/importar una Obra entre entornos (local ↔ VPS)

Local y la VPS usan bases de datos Postgres físicamente separadas — no hay
forma de que una Obra cargada en un lado "aparezca" en el otro salvo
llevándola a mano. Estos dos comandos de `manage.py` hacen eso: empaquetan
una Obra completa (itinerario, marcas de tiempo/notación/tempo, y cada
Partitura con sus páginas/sistemas/barras/compases) más sus archivos media
(audio, PDFs) en un único `.zip`, y la recrean en el otro entorno como una
Obra **nueva** (nunca reemplazan ni fusionan con una existente).

Los comandos viven en `partituras/management/commands/exportar_obra.py` e
`importar_obra.py`.

---

## 1. Exportar

Desde el entorno de **origen** (donde está la Obra hoy):

```bash
python manage.py exportar_obra <id_obra> --out obra.zip
```

- `<id_obra>`: el id de la `Obra` (lo ves en la URL de `obra_detalle`, o
  buscándola en el admin).
- `--out`: ruta del `.zip` de salida. Si no se indica, usa
  `obra_<id>_<slug-del-título>.zip` en el directorio actual.

Al terminar imprime un resumen (cantidad de partituras/páginas/compases,
si tiene audio) para poder chequear a ojo que exportó lo esperado.

### Qué incluye

- La `Obra` en sí (título, compositor, arreglista, publicada).
- El **ciclo/repertorio** asignado, si tiene (se guarda por nombre, no por
  id — ver más abajo cómo se resuelve al importar).
- Itinerario completo (`Segmento`), marcas de tiempo por compás y por
  pulso (`MarcaTiempoCompas`/`MarcaTiempoPulso`), marcas de notación
  (`MarcaNotacion`) y efectos de tempo (`EfectoTempo`).
- Cada `Partitura` (parte/instrumento) de la obra, con:
  - el instrumento asignado (por nombre, ver más abajo),
  - el PDF original,
  - cada `Pagina` con TODO su estado de análisis (orientación, márgenes,
    ancla, umbrales ajustados a mano) — así no hay que re-detectar nada
    del lado de destino,
  - cada `Sistema` con sus bordes de contenido, y sus `Barra`/`Compas`.
- El audio de referencia (mp3), si tiene.

### Qué NO incluye (a propósito)

- **Preferencias de usuario** (`PreferenciaObra`/`PreferenciaParte`: zoom,
  velocidad, rango recordado) — son por-usuario y por-entorno, no tienen
  sentido transplantadas.
- **El caché de imágenes normalizadas** (`media/cache_paginas/`) — es
  pura caché derivada de la PDF + los parámetros de orientación (que sí
  se exportan); se regenera sola la primera vez que se mira una página en
  el entorno de destino.
- El **owner** original no se preserva como tal — se guarda el
  `username` de referencia (aparece en el resumen al importar), pero
  quién queda como dueño en el destino lo decidís vos con `--owner` (ver
  abajo). Los ids de usuario no son los mismos entre local y la VPS.

---

## 2. Copiar el .zip al otro entorno

Con `scp`, usando el alias `infedu` ya configurado en `~/.ssh/config`
(`Host infedu` → `infedu.com.ar`). **Siempre corriendo el `scp` desde tu
máquina local** (no desde adentro de la sesión SSH) — la VPS no te puede
"empujar" el archivo, lo bajás/subís vos:

```bash
# local -> VPS (después de exportar en local)
scp obra.zip infedu:/var/www/scoresync/obra.zip

# VPS -> local (después de exportar en la VPS)
scp infedu:/var/www/scoresync/obra.zip .
```

---

## 3. Importar

Desde el entorno de **destino**:

```bash
python manage.py importar_obra obra.zip --owner <username_destino>
```

- `--owner` (obligatorio): el `username` del usuario que va a quedar como
  dueño de la Obra y de cada Partitura en ESTE entorno. Tiene que existir
  ya en este entorno — si no, el comando avisa y no hace nada.
- `--yes`: no pedir confirmación (para correrlo desatendido). Por default
  el comando muestra el resumen de qué va a importar y pide `[s/N]` antes
  de tocar cualquier cosa.
- `--sin-backup`: saltear el respaldo automático de la base (ver
  siguiente sección) — no recomendado, pero disponible.
- `--backup-dir <carpeta>`: dónde dejar el respaldo (default: el
  directorio actual).

El comando:

1. Muestra el resumen (título, cantidad de partituras/páginas/compases,
   audio sí/no, quién quedaría como dueño) y pide confirmación.
2. Hace un **respaldo completo de la base** antes de escribir nada (ver
   abajo).
3. Crea la Obra y todo su árbol con **ids nuevos** propios de este
   entorno — nunca toca ni pisa ninguna Obra existente. Si ya había una
   versión vieja de esta misma obra en el destino, queda intacta —
   borrala a mano una vez que confirmes que la importación nueva está
   bien.

### Cómo se resuelven las referencias cruzadas

- **Ciclo/Repertorio**: se busca por nombre exacto (`Repertorio.nombre` +
  `Ciclo.nombre`) en el entorno de destino. Si no existe esa combinación
  ahí, la Obra importada queda sin ciclo asignado (se avisa por consola)
  — no se crea un Ciclo nuevo automáticamente.
- **Instrumento**: mismo criterio, por `nombre`. Si no se encuentra, esa
  Partitura queda con instrumento vacío (se avisa por consola).
- **Owner**: siempre el que indiques con `--owner`, sin excepción.

---

## 4. El respaldo automático antes de importar

Como la importación escribe en una base compartida con el resto del
ecosistema (ensayos/afinación/tempo/infedu, todas en la misma Postgres),
antes de crear cualquier fila el comando corre un `pg_dump` completo de
**toda** la base (no sólo las tablas de `partituras`) y lo guarda como
`respaldo_<nombre_db>_antes_de_importar_<fecha>.sql`. Si el `pg_dump`
falla por lo que sea, la importación se cancela — no tiene sentido seguir
sin la red de seguridad salvo que la saltees a propósito con
`--sin-backup`.

### Si algún día hay que restaurar ese respaldo

```bash
psql -h localhost -U postgres -d ensayos < respaldo_ensayos_antes_de_importar_20260101_120000.sql
```

(ajustar host/usuario/nombre de base según el `.env` de ese entorno).

### pg_dump no encontrado (típico en Windows)

El comando busca `pg_dump` primero en el `PATH`, y si no lo encuentra ahí
(o lo que encuentra es un `.bat` que Python no puede ejecutar
directamente), prueba la instalación típica de PostgreSQL para Windows:

```
C:/Program Files/PostgreSQL/*/bin/pg_dump.exe
```

Si tampoco está ahí, hay que instalar el cliente de PostgreSQL. En
Windows, el instalador oficial (o `choco install postgresql`) lo deja en
esa ruta — no hace falta agregar nada al PATH para que el comando lo
encuentre solo. Para poder tipear `pg_dump`/`psql` a mano en una terminal
Git Bash sin agregar la carpeta entera de PostgreSQL al PATH del sistema,
alcanza con un wrapper por archivo en `~/bin` (que ya está en el PATH de
Git Bash), por ejemplo:

```bash
mkdir -p ~/bin
cat > ~/bin/pg_dump.bat << 'EOF'
@echo off
"C:\Program Files\PostgreSQL\18\bin\pg_dump.exe" %*
EOF
```

(mismo patrón para `psql.bat`/`pg_dumpall.bat`, ajustando la versión de
la carpeta).

---

## 5. Verificación hecha al construir esto (2026-07-28)

Se probó un ida y vuelta real, en el mismo entorno local, con "A Little
Concert Suite" (9 partituras, 26 páginas, 1721 compases, audio, ciclo
asignado, y una renumeración manual de movimiento 201→205 en un sistema
puntual): exportar + importar reprodujo exactamente los mismos conteos y
los mismos números/repeticiones/geometría en la obra importada. La obra
de prueba se borró después de verificar (no quedó en la base real).

**Todavía no probado**: un ida y vuelta real contra la VPS (sólo se probó
local→local). Antes de confiar en esto para mover una obra real
local↔VPS, conviene hacer una primera prueba con una obra chica.
