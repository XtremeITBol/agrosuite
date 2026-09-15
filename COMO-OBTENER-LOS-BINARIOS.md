# Cómo obtener el `.apk` y el `.exe`

El código de ambas apps está completo en este repositorio. Lo que falta es
compilarlo, y eso requiere herramientas que no se pueden ejecutar en cualquier
máquina:

* **`AgroSuite.exe`** sólo se puede compilar **desde Windows**. PyInstaller no
  hace compilación cruzada: no existe forma de generar un ejecutable de Windows
  desde Linux o macOS.
* **`AgroSuite-Campo.apk`** necesita el **SDK de Android** (`aapt2`, `d8`,
  `android.jar`), que son unos 2 GB de descarga.

Hay dos caminos. El primero no requiere instalar absolutamente nada.

---

## Camino A — GitHub compila los dos por vos (recomendado)

GitHub ofrece máquinas de compilación gratis para repositorios públicos, con
Windows y con el SDK de Android ya instalados. Los workflows ya están escritos.

### 1. Subir el proyecto a GitHub

Creá un repositorio nuevo en <https://github.com/new> y después, desde la
carpeta del proyecto:

```bash
git init
git add .
git commit -m "AgroSuite"
git branch -M main
git remote add origin https://github.com/TU-USUARIO/agrosuite.git
git push -u origin main
```

### 2. Compilar

**Opción rápida — los dos binarios de una vez.** Publicá una versión con un tag:

```bash
git tag v1.0.0
git push origin v1.0.0
```

Eso dispara los dos workflows a la vez. En 10-20 minutos vas a tener el `.apk`
y el `.exe` publicados juntos en la sección **Releases** del repositorio, listos
para descargar.

**Opción manual — uno a la vez.** En el repositorio, pestaña **Actions**,
elegí `Compilar AgroSuite.apk` o `Compilar AgroSuite.exe`, botón **Run
workflow**. Al terminar, el binario queda en **Artifacts**, al pie de la página
de esa ejecución.

### Qué hacen los workflows además de compilar

No se limitan a producir el archivo; verifican que sirva:

* El de Windows corre las 88 pruebas, **arranca el .exe** y comprueba que
  responda en `/api/health` antes de publicarlo. Si al empaquetar se perdió un
  módulo de scikit-learn, falla ahí y no en la PC de la finca.
* El de Android inspecciona el APK compilado con `aapt2` y verifica que el
  paquete, los permisos de cámara y ubicación, y el `minSdk` sean los
  correctos.

---

## Camino B — compilar en tu propia máquina

### El `.exe`, en una PC con Windows

Necesitás Python 3.11 o 3.12 de 64 bits (<https://www.python.org/downloads/>,
marcando *Add Python to PATH* al instalar). Después, en la carpeta del proyecto:

```bat
packaging\build_windows.bat
```

Tarda entre 5 y 15 minutos. El resultado queda en `dist\AgroSuite\`.

**Para distribuirlo hay que comprimir la carpeta completa**, no sólo el
`AgroSuite.exe`: el ejecutable necesita los archivos que lo acompañan.

### El `.apk`, con Android Studio

Descargá Android Studio (<https://developer.android.com/studio>), abrí la
carpeta `android/` del proyecto, esperá a que sincronice y usá
**Build › Build App Bundle(s)/APK(s) › Build APK(s)**.

Sin Android Studio, con el SDK ya instalado y `ANDROID_HOME` configurado:

```bash
cd android
gradle wrapper          # genera gradlew la primera vez
./gradlew assembleDebug
```

El APK queda en `android/app/build/outputs/apk/debug/app-debug.apk`.

---

## Instalar el APK en los teléfonos

El APK se firma con la clave de depuración estándar de Android, que permite
instalarlo directamente sin pasar por Play Store — que es lo que necesitás para
repartirlo entre la gente de la finca.

1. Pasá el `.apk` al teléfono (WhatsApp, cable, o descargándolo del release).
2. Abrilo. Android va a pedir permiso para instalar desde esa app: aceptá
   **"Permitir desde esta fuente"**.
3. La primera vez la app pide la dirección del servidor. Es la que muestra
   AgroSuite en la pantalla de la computadora de la oficina, por ejemplo
   `https://192.168.1.100:8443`. El botón **Probar conexión** verifica de
   verdad contra el servidor antes de guardar.
4. Si avisa que falta el certificado, seguí el paso que indica en pantalla. Es
   una sola vez por teléfono.

### Si querés publicarlo en Play Store

Hace falta un APK firmado con tu propia clave. Generala una vez:

```bash
keytool -genkeypair -v -keystore agrosuite.keystore \
  -alias agrosuite -keyalg RSA -keysize 2048 -validity 10000
```

Después convertila a base64 (`base64 -w0 agrosuite.keystore`) y cargá cuatro
secretos en el repositorio, en *Settings › Secrets and variables › Actions*:

| Secreto | Contenido |
|---|---|
| `ANDROID_KEYSTORE_BASE64` | el keystore en base64 |
| `ANDROID_STORE_PASSWORD` | la contraseña del almacén |
| `ANDROID_KEY_ALIAS` | `agrosuite` |
| `ANDROID_KEY_PASSWORD` | la contraseña de la clave |

Con eso presente, el workflow genera además un `AgroSuite-Campo-release.apk`
firmado. **Guardá ese keystore**: si lo perdés, Google no te deja volver a
publicar actualizaciones de la misma app, nunca.

---

## Dos advertencias antes de repartir

**El `.exe` no está firmado digitalmente.** Windows SmartScreen va a decir
"Windows protegió su PC" la primera vez. Se pasa con *Más información ›
Ejecutar de todas formas*. Firmarlo requiere un certificado de firma de código
pago, del orden de 200 a 400 dólares al año.

**Instalar la CA de la finca en un teléfono es una decisión de seguridad
real.** Esa autoridad puede firmar certificados para cualquier sitio en ese
dispositivo. La clave privada nunca sale de la PC de la finca, pero conviene
saber qué implica: cuidá esa máquina, y no instales el certificado en teléfonos
que no sean del equipo.
