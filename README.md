# AgroSuite

Plataforma modular para agricultura extensiva con cuatro capacidades en un solo
sistema: **optimización de la cadena de suministro**, **predicción de plagas**,
**atención al cliente automatizada** y **analítica de producción**.

Prototipo funcional, no una maqueta: los cuatro módulos ejecutan algoritmos
reales sobre una base de datos real y están cubiertos por 42 pruebas.

---

Corre en tres formas, todas sobre el mismo código: **servidor local**,
**ejecutable de Windows** y **app instalable en Android** (PWA) con monitoreo a
campo sin señal.

## Arranque en 30 segundos

```bash
pip install -r requirements.txt
python run.py demo          # genera datos + entrena modelos + levanta el servidor
```

Abrí <http://127.0.0.1:8000>.

Por separado:

```bash
python run.py seed          # base con 2 años de histórico sintético
python run.py train         # entrena el predictor de plagas y el clasificador de intención
python run.py serve         # API + tablero (sólo esta PC)
python run.py serve --https # además: red local + HTTPS, para los celulares
python run.py sync-weather  # trae clima real de Open-Meteo (gratis, sin API key)
python -m unittest discover -s tests -v    # 88 pruebas
```

Estructura de las tres plataformas:

```
agrosuite/
├── agrosuite/            núcleo: 4 módulos, API, modelos
├── web/                  tablero + PWA de campo (offline, GPS, foto)
├── android/              proyecto Android nativo → AgroSuite-Campo.apk
├── packaging/            PyInstaller → AgroSuite.exe
└── .github/workflows/    compilan APK y EXE automáticamente
```

---

## Windows: generar el .exe

**No se puede compilar desde Linux ni macOS.** PyInstaller no hace compilación
cruzada: un ejecutable de Windows se genera únicamente desde Windows. Hay dos
caminos y los dos están listos en el repositorio.

**Opción A — en una PC con Windows** (Python 3.11 o 3.12 de 64 bits):

```bat
packaging\build_windows.bat
```

Queda en `dist\AgroSuite\AgroSuite.exe`. Para distribuirlo hay que comprimir la
**carpeta completa**, no sólo el .exe.

**Opción B — sin tener Windows a mano:** `.github/workflows/build-windows.yml`
compila el binario en un runner de Windows de GitHub (gratis en repositorios
públicos). Se dispara a mano desde la pestaña Actions, o publicando un tag
`v1.0.0`. Además corre las 70 pruebas y **verifica que el .exe arranque de
verdad** y responda `/api/health` antes de publicarlo — así un módulo oculto
que falte se detecta en el pipeline y no en la PC del usuario.

Qué hace el ejecutable al abrirse: prepara la base y entrena los modelos la
primera vez, genera el certificado de la red, levanta el servidor en toda la
red local, abre el tablero en el navegador y muestra en pantalla la dirección
para los celulares.

Decisiones del empaquetado que importan:

| Decisión | Motivo |
|---|---|
| `onedir`, no `onefile` | Un `onefile` se descomprime entero en `%TEMP%` en **cada** arranque: con scipy y sklearn adentro son ~20 s de espera por vez, y varios antivirus lo marcan. |
| Sin UPX | La compresión UPX es una de las principales causas de falsos positivos de antivirus. |
| `hiddenimports` explícitos | sklearn y scipy cargan submódulos por nombre en runtime; el análisis estático no los ve y el .exe crashea al primer modelo. |
| Consola visible | La consola **es** la interfaz: muestra la URL para los celulares y el paso de instalación del certificado. |
| Datos fuera del bundle | `sys._MEIPASS` se borra al cerrar. La base, los modelos, los certificados y las fotos van a una carpeta junto al .exe, o al perfil del usuario si esa ubicación es de sólo lectura. |

Tamaño esperado: **300-400 MB** descomprimido. Casi todo es scipy y
scikit-learn; es el precio de que el MILP y los modelos corran sin instalar nada.

---

## Android: app instalable con monitoreo sin señal

El teléfono **no** ejecuta Python. Compilar scipy y scikit-learn para ARM es
frágil, el APK quedaría enorme, y un celular no es el lugar para resolver un
MILP ni entrenar un gradient boosting. El celular es un cliente de la misma API
que ya usa el tablero.

La app de campo está en `/campo`. Se instala desde el navegador ("Agregar a
pantalla de inicio") y queda como una app más, con su ícono, a pantalla
completa.

### Lo que hace a campo

* Registra monitoreo: lote, plaga, incidencia, fecha, responsable y acción.
* Filtra las plagas por el cultivo del lote y avisa al superar el umbral de
  daño económico, en el momento de cargar el dato.
* Toma foto (se reescala en el teléfono a 1280 px antes de viajar) y
  coordenadas GPS del punto muestreado.
* **Funciona sin señal**: guarda en IndexedDB y sincroniza sola cuando vuelve
  la conexión, vía Background Sync — incluso con la app cerrada.
* Lo cargado entra directo a `pest_observations` y alimenta el modelo predictivo.

### El requisito que no se puede saltear: HTTPS

Una PWA sólo instala, registra service worker, usa GPS y abre la cámara en un
**contexto seguro**. `localhost` cuenta; `http://192.168.1.50:8000` **no**. Sin
HTTPS la app funciona como página web común, sin offline y sin GPS.

Como una IP de red local no puede tener un certificado público, el sistema
genera su propia autoridad certificadora:

```bash
python run.py serve --https        # o simplemente abrir AgroSuite.exe
```

Y en cada celular, **una sola vez**:

1. Abrir `https://<ip-de-la-pc>:8443/agrosuite-ca.crt`
2. Aceptar la advertencia y descargar.
3. Ajustes › Seguridad › Cifrado y credenciales › Instalar un certificado ›
   Certificado de CA.
4. Volver a `https://<ip-de-la-pc>:8443/campo` e instalar la app.

El certificado del servidor se regenera solo si cambia la IP de la PC (típico
con DHCP) o si está por vencer; la CA instalada en los teléfonos sigue valiendo.

Si alguien entra por `http://`, la app lo detecta, lo avisa en pantalla y sigue
funcionando en modo limitado en lugar de romperse en silencio.

### El APK nativo

Además de la PWA hay un **proyecto Android nativo completo** en `android/`
(Java + WebView, sin npm ni Capacitor). Compila a `AgroSuite-Campo.apk`.

No es un capricho: el APK resuelve un problema que la PWA **no puede** resolver.
Desde Android 7, una app ignora por defecto las autoridades certificadoras que
instala el usuario. Chrome las respeta para navegar, pero un WebView dentro de
una app, no — salvo que la app lo declare. Eso es
`res/xml/network_security_config.xml`, y es la razón principal de que el APK
exista. Lo demás que aporta:

* ícono propio y arranque a pantalla completa, sin depender de que alguien
  acierte el menú "Agregar a pantalla de inicio";
* permisos nativos de cámara y ubicación, concedidos una sola vez;
* la dirección del servidor guardada y **verificada de verdad** contra
  `/api/health` antes de aceptarla, con mensajes distintos según el fallo sea
  de red o de certificado;
* botón atrás que navega dentro del formulario en vez de cerrar la app.

Lo que **no** hace es duplicar lógica: el formulario, la cola sin señal y la
sincronización siguen siendo la misma PWA que sirve el servidor.

El APK nunca acepta un certificado inválido en silencio — hay una prueba
automatizada que lo verifica. Si la CA falta, muestra el paso que hay que dar.

**Cómo obtener los binarios:** ver [`COMO-OBTENER-LOS-BINARIOS.md`](COMO-OBTENER-LOS-BINARIOS.md).
Ni el `.exe` ni el `.apk` se pueden compilar desde este entorno (PyInstaller no
compila cruzado; el SDK de Android pesa 2 GB), así que hay dos workflows de
GitHub Actions que los generan automáticamente, verifican que funcionen y los
publican para descargar.

---

## Decisiones de arquitectura

El requisito fue *empezar de cero con bajo costo*, así que cada pieza se eligió
para que el costo marginal de operación sea cercano a cero:

| Necesidad | Elección | Por qué |
|---|---|---|
| Persistencia | **SQLite** (stdlib) | Cero infraestructura. El SQL es estándar: migrar a PostgreSQL es cambiar la conexión. |
| Solver de optimización | **HiGHS** vía `scipy.optimize.milp` | Solver MILP de grado industrial, ya viene con SciPy. Sin licencia (a diferencia de Gurobi/CPLEX). |
| Modelos | **scikit-learn** | Suficiente para tabular. Nada de deep learning donde no aporta. |
| Clima | **Open-Meteo** | Gratuito, sin API key, con histórico y pronóstico. Con degradación a datos locales si no hay red. |
| API | **Flask** | Mínima, sin build step. |
| Frontend | **Un archivo HTML** | Sin npm, sin bundler, sin CDN. Se sirve desde el mismo proceso y funciona offline. |
| LLM | **Opcional y desactivado** | El chatbot funciona sin ningún LLM. Ver "Chatbot" abajo. |

Estructura:

```
agrosuite/
├── run.py                    # CLI: seed | train | serve | demo | sync-weather
├── agrosuite/
│   ├── config.py             # configuración por variables de entorno
│   ├── db.py                 # esquema y acceso a datos
│   ├── seed.py               # generador de datos sintéticos con señal causal
│   ├── api.py                # API REST (Flask)
│   ├── integrations/weather.py
│   └── modules/
│       ├── supply_chain.py   # MILP + ruteo + política de inventario
│       ├── pest.py           # grados-día + gradient boosting
│       ├── chatbot.py        # intención + entidades + acciones + recuperación
│       └── analytics.py      # KPIs + rinde + anomalías + pronóstico
├── web/index.html            # tablero
└── tests/test_agrosuite.py   # 42 pruebas
```

---

## Módulo 1 — Cadena de suministro

**Red de acopio y despacho.** Problema de transbordo lote → silo → planta/puerto
formulado como MILP: variables continuas de flujo más binarias de activación de
silo (costo fijo por operar cada uno). Restricciones: evacuación total de la
cosecha, capacidad libre por silo, conservación de flujo, techo de demanda por
destino y activación con big-M sobre entradas *y* salidas.

El objetivo minimiza **costo logístico + margen perdido por demanda no
atendida**. Es equivalente a maximizar ingreso menos costo, pero no inventa
utilidad sobre grano que ya estaba en stock — una formulación de "margen" ingenua
reporta millones de utilidad fantasma al vender inventario sin costo de
mercadería.

**Ruteo de flota.** CVRP con horizonte de un día. La capacidad diaria se reparte
entre los lotes de forma proporcional al tonelaje pendiente, y los viajes se
construyen **contra la capacidad real de cada camión** (nunca se arma un viaje de
34 t que sólo un camión de 25 t pueda tomar). Cada camión se llena por vecino más
cercano y las rutas multi-parada se refinan con 2-opt. Reporta el backlog en días.

**Inventario.** Política (s, Q) por SKU: punto de reorden `μ_L + z·σ√L` con nivel
de servicio configurable, y EOQ de Wilson. Clasifica en OK / REPONER / CRÍTICO.

## Módulo 2 — Predicción de plagas

Enfoque híbrido, que es lo que funciona en agronomía:

1. **Capa agronómica** — grados-día acumulados desde la siembra, con temperatura
   base y umbral fenológico por especie. Funciona el día uno, sin histórico.
2. **Capa de aprendizaje** — `HistGradientBoostingClassifier` sobre 22 variables:
   fenología, ventanas móviles de 7/14/30 días (temperatura, humedad, lluvia,
   días de mojado foliar), distancia al óptimo de humedad de la especie y la
   última lectura de monitoreo.

La salida es la decisión real del productor: **probabilidad de superar el umbral
de daño económico en los próximos 14 días**, con recomendación específica por
especie.

Seis plagas modeladas con umbrales agronómicos reales: cogollero, oruga de las
leguminosas, heliotis, chinche marrón, roya asiática y vaquita de San Antonio.

**Validación.** Partición **temporal** (entrena con el pasado, evalúa con el
futuro) — una partición aleatoria en series de tiempo infla las métricas. Se
reportan dos baselines: persistencia (la última lectura) y la regla de grados-día
sola. Resultado típico con los datos sintéticos:

```
ROC-AUC 0.98   PR-AUC 0.94   Brier 0.04
baseline persistencia 0.94   baseline grados-día 0.32
```

El baseline de grados-día por debajo de 0.5 no es un error: la acumulación
térmica sigue creciendo después del pico poblacional, así que por sí sola no es
monótona respecto del riesgo. Ese es exactamente el motivo por el que el modelo
la combina con clima en vez de usarla aislada.

Si no hay modelo entrenado, el sistema **no falla**: cae a la regla de grados-día
pura y lo declara en la respuesta.

## Módulo 3 — Atención al cliente

Cascada escalonada, de barata a cara:

1. **Intención** — regresión logística sobre TF-IDF de palabras *y* de n-gramas
   de caracteres. Los char n-grams importan: por WhatsApp llegan "cogoyero",
   "quando llega", sin tildes.
2. **Entidades** — número de pedido, SKU, producto, tonelaje, plaga (con
   variantes ortográficas).
3. **Acciones sobre datos reales** — consulta la base y responde con el dato
   concreto: estado de un pedido, stock y cobertura de un insumo, precio promedio
   contratado, riesgo vigente de una plaga. No con texto genérico.
4. **Recuperación** — TF-IDF + coseno sobre la base de conocimiento, para lo
   informativo.
5. **Escalamiento** — por baja confianza o por intención (reclamo, pedido de
   hablar con una persona). Un bot que inventa es peor que un bot que deriva.

**Sobre el LLM.** `AGROSUITE_LLM_PROVIDER` es opcional y viene apagado. Cuando se
activa, el LLM **sólo reescribe** una respuesta ya fundamentada en datos; nunca
es la fuente de los hechos. Es lo que evita que el bot alucine un precio o un
estado de entrega, y mantiene el costo en cero por defecto.

## Módulo 4 — Analítica de producción

* **KPIs** con comparación contra el período anterior (una cifra sin referencia
  no es un KPI).
* **Rendimiento por lote** contra el objetivo del cultivo, con la brecha en
  toneladas y la correlación con presión de plagas — etiquetada como asociación,
  no causalidad.
* **Anomalías** con Isolation Forest sobre el perfil multivariado de cada
  cosecha, estandarizado, indicando qué variable dispara cada detección.
* **Pronóstico de rendimiento** con RandomForest y validación cruzada, comparado
  contra el baseline de predecir la media.

---

## API

| Método | Endpoint | Qué devuelve |
|---|---|---|
| GET | `/api/field/catalog` | datos maestros que la PWA guarda para operar sin señal |
| POST | `/api/field/observations` | alta de monitoreo, idempotente por `client_uuid` |
| GET | `/api/field/observations` | monitoreo reciente, filtrable por origen |
| GET | `/api/field/server-info` | direcciones de red y estado del certificado |
| GET | `/api/health` | estado y conteo de registros |
| GET | `/api/fields` | lotes con cultivo |
| GET | `/api/supply/network` | plan MILP de acopio y despacho |
| GET | `/api/supply/routes` | plan de despacho del día |
| GET | `/api/supply/inventory` | política (s, Q) por SKU |
| GET | `/api/pest/risk` | riesgo a 14 días por lote y especie |
| GET | `/api/pest/observations` | histórico de monitoreo |
| POST | `/api/pest/train` | reentrena y devuelve las métricas |
| POST | `/api/chat/message` | `{"message": "..."}` → respuesta del bot |
| GET | `/api/chat/stats` | contención, intenciones, canales |
| GET | `/api/chat/kb` | base de conocimiento |
| GET | `/api/analytics/kpis` | KPIs con comparación |
| GET | `/api/analytics/yield` | rendimiento por lote |
| GET | `/api/analytics/anomalies` | cosechas anómalas |
| GET | `/api/analytics/forecast` | pronóstico del próximo ciclo |
| GET | `/api/analytics/series` | serie temporal de producción |
| POST | `/api/weather/sync` | actualiza clima desde Open-Meteo |

Parámetros de consulta validados con rango; los errores devuelven JSON con
`{"error": ..., "type": "validacion"}` y HTTP 400.

## Configuración

Todo por variables de entorno, con valores por defecto que funcionan sin tocar
nada:

```bash
AGROSUITE_DB=data/agrosuite.db
AGROSUITE_DATA_DIR=                # dónde guardar base, modelos, certificados y fotos
AGROSUITE_PORT=8000
AGROSUITE_HTTPS=0                  # 1 para HTTPS con CA local (necesario para la PWA)
AGROSUITE_HTTPS_PORT=8443
AGROSUITE_WEATHER_ONLINE=0        # 1 para consultar Open-Meteo
AGROSUITE_LLM_PROVIDER=none       # none | anthropic | openai
AGROSUITE_LLM_API_KEY=
AGROSUITE_BOT_FLOOR=0.35          # confianza mínima antes de derivar a un humano
AGROSUITE_SEED=42
```

---

## Limitaciones honestas de este prototipo

Importan para no sacar conclusiones equivocadas de las métricas:

1. **Los datos son sintéticos, con señal causal deliberada.** El generador crea
   la incidencia de plagas a partir de grados-día, humedad y lluvia, así que el
   modelo aprende una relación verdadera — pero *más limpia* que la realidad.
   Con datos de campo hay que esperar un AUC bastante menor.
2. **La exactitud del clasificador de intención es 1.00 y eso no es real.** Los
   tickets sintéticos salen de plantillas, y por lo tanto son trivialmente
   separables. Con mensajes reales de WhatsApp la cifra baja mucho. La
   arquitectura sirve; la métrica no.
3. **El R² del pronóstico de rendimiento es optimista.** `target_yield` y
   `cycle_days` son constantes por cultivo, así que el modelo aprende sobre todo
   a identificar el cultivo. Con datos reales: quitá esas dos variables o validá
   por campaña completa (`GroupKFold` por año).
4. **Las distancias son geodésicas por un factor de sinuosidad de 1.32**, no
   distancias de ruta reales. Para producción, conectá OSRM o Google Distance
   Matrix.
5. **El servidor de Flask es de desarrollo.** Para producción: gunicorn detrás de
   nginx, y PostgreSQL en lugar de SQLite si hay más de un proceso escribiendo.
6. **No hay autenticación.** Es un prototipo de un solo tenant. Cualquiera en la
   red WiFi de la finca puede abrir el tablero y cargar monitoreo. Para una red
   cerrada puede ser aceptable; para exponerlo fuera de la finca, la
   autenticación es lo primero.
7. **El .exe no está firmado digitalmente.** Windows SmartScreen va a mostrar
   "Aplicación no reconocida" la primera vez (se pasa con "Más información ›
   Ejecutar de todas formas"). Firmarlo requiere un certificado de firma de
   código pago, del orden de 200-400 USD al año.
8. **Instalar una CA en el teléfono es una decisión de seguridad real.** Esa CA
   podría firmar certificados para cualquier sitio en ese dispositivo. La clave
   privada queda sólo en la PC de la finca, pero conviene saber qué implica:
   protegé esa máquina, y no instales la CA en teléfonos que no sean del
   equipo.

## Qué haría a continuación

En orden de retorno sobre esfuerzo:

1. **Conectores de ingesta** que reemplacen `seed.py`: balanza, ERP, planillas de
   monitoreo. Es el paso que convierte esto en un sistema con valor real.
2. **Distancias de ruta reales** (OSRM autohospedado es gratis) — el ahorro
   logístico calculado depende directamente de esto.
3. **Registro de aplicaciones fitosanitarias** para cerrar el ciclo del módulo de
   plagas: hoy predice, pero no aprende de qué se aplicó y con qué resultado.
4. **App móvil de monitoreo a campo** (PWA sobre la misma API) para que el
   scouting entre directo, con foto y geolocalización, en lugar de en papel.
5. **Autenticación, multi-tenancy y auditoría.**
6. **Visión por computadora** para identificar plaga a partir de la foto del
   monitoreo — es lo más vistoso, y a propósito va último: sin los cinco puntos
   anteriores no cambia ninguna decisión.
