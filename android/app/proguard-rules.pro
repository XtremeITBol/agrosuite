# La app es un contenedor de WebView: no hay lógica de negocio que ofuscar.
# Se conservan las clases del WebView y los callbacks que invoca el sistema.
-keepclassmembers class * extends android.webkit.WebChromeClient {
    public void *(android.webkit.WebView, java.lang.String);
}
-keep class android.webkit.** { *; }
-dontwarn android.webkit.**
