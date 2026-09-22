# GDI Sync System

Herramienta para descargar y mantener una copia local de los datos de tu municipio desde GDI Latam.

## Requisitos

- Python 3.8 o superior (sin dependencias externas)
- Credenciales provistas por GDI Latam (API Key + URL del servidor)

## Instalacion

### Opcion A — con git (recomendada)

Es la unica forma en la que el script puede avisarte solo cuando hay una version nueva.

```bash
# 1. Clonar
git clone https://github.com/GDI-AGPLv3/GDI-SyncSystem.git
cd GDI-SyncSystem

# 2. Configurar credenciales
cp config.example.env .env
# Editar .env con los valores que te proveyó GDI Latam
```

Para actualizar, mas adelante: `git pull`.

### Opcion B — sin git (descarga directa)

Si no tenés git, bajá el ZIP y descomprimilo:

<https://github.com/GDI-AGPLv3/GDI-SyncSystem/archive/refs/heads/main.zip>

Funciona igual, con una diferencia: **sin git el script no puede avisarte de versiones
nuevas** (el chequeo se saltea en silencio). Si vas por esta via, revisá la pagina del
repositorio cada tanto, o pedinos que te avisemos por mail.

Despues, igual que arriba: `cp config.example.env .env` y completá los valores.

## Uso

```bash
# Sync incremental (solo descarga lo que cambió desde la última vez)
python sync.py

# Descarga completa desde cero
python sync.py --full

# Sync incremental + descargar PDFs firmados
python sync.py --pdfs

# Todo desde cero, incluyendo PDFs
python sync.py --full --pdfs
```

### Salida de ejemplo

```
GDI Sync System v3.2.0
  Gateway : https://tu-municipio-gateway.gdilatam.com
  DB      : backup.db
  Modo    : INCREMENTAL

  Tenant  : 999_municipio
  Tablas  : 36

  OK   cases                                      +12 nuevas
  OK   official_documents                         +47 nuevas
  OK   users                                      sin cambios
  OK   departments                                sin cambios
  ...

  Total: 59 filas nuevas/actualizadas.
```

## Resultado

El script genera `backup.db`, una base de datos SQLite con todas las tablas de tu municipio. Podés consultarla con cualquier herramienta compatible con SQLite:

- [DB Browser for SQLite](https://sqlitebrowser.org/) (gratuito, Windows/Mac/Linux)
- [DBeaver](https://dbeaver.io/) (gratuito)
- Desde Python, Excel, o cualquier herramienta de análisis de datos

## Automatizar (opcional)

**Linux/Mac — cron** (ejecutar cada hora):
```
0 * * * * cd /ruta/gdi-sync-system && python sync.py >> sync.log 2>&1
```

**Windows — Task Scheduler:**
1. Abrir Task Scheduler
2. Crear tarea básica
3. Acción: `python C:\ruta\gdi-sync-system\sync.py`
4. Trigger: diario o cada X horas

## Tablas disponibles

| Grupo | Tablas |
|-------|--------|
| Estructura | departments, sectors, ranks, city_seals |
| Usuarios | users, user_roles, user_seals, user_sector_permissions, estado_users |
| Documentos | official_documents, document_signers, document_rejections |
| Tipos | document_types, document_types_allowed_by_rank, enabled_document_types_by_sector |
| Expedientes | cases, case_movements, case_templates, case_template_allowed_departments, case_official_documents, case_proposed_documents, case_responsibles, case_favorites |
| Notas | notes_recipients, notes_openings, memo_recipients |
| Legajos (RLM) | registry_families, registry_family_permissions, records, record_history, record_relations, record_case_links, record_document_links |
| Ciudadanos (TAD) | citizens |
| Actividad por usuario | case_user_views, notification_dismissals |

> La lista la define el servidor, no el script: la version exacta que te corresponde la
> devuelve `GET /api/v1/sync/schema` y el sync crea solo las tablas que falten.

## Actualizaciones

Cuando GDI Latam agrega **campos o tablas nuevas**, no tenés que hacer nada especial:
el sync los detecta y los incorpora solo a tu `backup.db` en la próxima corrida
(agrega columnas y tablas que falten automáticamente).

Solo necesitás **actualizar el script** cuando hay una nueva versión de `sync.py`
(una corrección o mejora). Si lo instalaste con git (opción A), no hace falta estar
pendiente: **el propio script avisa** al arrancar cuando detecta una versión nueva.

```
GDI Sync System v3.2.0
  AVISO   : hay una version nueva (3.2.1, esta es 3.2.0). Actualizar con: git pull
```

En ese caso:

```bash
git pull            # traer la última versión
python sync.py      # tu backup.db se sigue usando; se auto-migra si hace falta
```

El chequeo consulta el repositorio publico y **no manda ningun dato tuyo**. Si no hay
git, no hay red, o tu copia no es un clon, se saltea **en silencio**: nunca interrumpe
el sync y nunca se auto-actualiza — bajar la version nueva lo decidis vos.

## Licencia

AGPL v3 — ver [LICENSE](LICENSE). El codigo es abierto: podés auditarlo, correrlo y
modificarlo. Es tu copia de tus datos; tenés derecho a ver exactamente qué hace.

Copyright (C) 2026 [Tecnología Acuario](https://gdilatam.com).

## Soporte

contacto@gdilatam.com
