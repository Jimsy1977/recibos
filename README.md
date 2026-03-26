# Chavimochic - Consulta de Recibos 💧

Aplicación web para consultar y visualizar recibos del Proyecto Especial Chavimochic.

## Funcionalidad

- Ingresa tu número de suministro (es también tu contraseña)
- La app hace scraping automático del portal de Chavimochic
- Visualiza los PDFs directamente en el navegador
- Descarga los recibos en PDF

## Estructura

```
chavimochic_app/
├── app.py              # Backend Flask + lógica de scraping
├── requirements.txt    # Dependencias
├── Procfile            # Para Railway/Render
├── render.yaml         # Config de Render
└── templates/
    ├── index.html      # Login
    ├── loading.html    # Pantalla de carga
    ├── recibos.html    # Lista de recibos
    ├── visor.html      # Visor de PDF
    └── error.html      # Pantalla de error
```

## Despliegue en Render

1. Sube la carpeta `chavimochic_app/` a GitHub
2. Ve a [render.com](https://render.com) → New Web Service
3. Conecta tu repositorio
4. Render detecta automáticamente el `render.yaml`
5. Espera el build (~5 minutos por la instalación de Chrome)

## Despliegue en Railway

1. Sube la carpeta a GitHub
2. Ve a [railway.app](https://railway.app) → New Project → Deploy from GitHub
3. Agrega la variable de entorno: `PLAYWRIGHT_BROWSERS_PATH=0` (si usas playwright)
4. Railway detecta el `Procfile` automáticamente

## Variables de entorno

No se necesitan variables de entorno adicionales. La app es stateless.

## Uso local

```bash
pip install -r requirements.txt
python app.py
```

Luego abre: http://localhost:5000
