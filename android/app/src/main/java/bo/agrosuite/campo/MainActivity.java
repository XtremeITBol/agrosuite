package bo.agrosuite.campo;

import android.Manifest;
import android.app.AlertDialog;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.net.http.SslError;
import android.os.Build;
import android.os.Bundle;
import android.view.View;
import android.webkit.GeolocationPermissions;
import android.webkit.PermissionRequest;
import android.webkit.SslErrorHandler;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Toast;

import androidx.activity.OnBackPressedCallback;
import androidx.annotation.NonNull;
import androidx.appcompat.app.AppCompatActivity;
import androidx.core.app.ActivityCompat;
import androidx.core.content.ContextCompat;
import androidx.swiperefreshlayout.widget.SwipeRefreshLayout;

/**
 * Contenedor de la app de campo.
 *
 * El trabajo real lo hace la PWA que sirve el servidor de la finca: el
 * formulario de monitoreo, la cola sin señal en IndexedDB y la sincronización
 * ya viven ahí, y son exactamente el mismo código que corre en el navegador.
 * Esta actividad aporta lo que un navegador no puede dar:
 *
 *  - Confianza en la CA local de la finca (ver network_security_config.xml).
 *    Es la razón principal de que el APK exista.
 *  - Un ícono en el cajón de aplicaciones y arranque a pantalla completa, sin
 *    depender de que alguien acierte el menú "Agregar a pantalla de inicio".
 *  - Permisos nativos de cámara y ubicación, concedidos una sola vez.
 *  - Una dirección de servidor guardada, en lugar de tipear una IP cada vez.
 */
public class MainActivity extends AppCompatActivity {

    public static final String PREFS = "agrosuite";
    public static final String KEY_SERVER = "server_url";

    private WebView web;
    private SwipeRefreshLayout refresh;
    private ValueCallback<Uri[]> filePicker;
    private String serverUrl;

    private static final int REQ_PERMISSIONS = 100;
    private static final int REQ_FILE = 101;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        SharedPreferences prefs = getSharedPreferences(PREFS, MODE_PRIVATE);
        serverUrl = prefs.getString(KEY_SERVER, null);
        if (serverUrl == null || serverUrl.isEmpty()) {
            startActivity(new Intent(this, SetupActivity.class));
            finish();
            return;
        }

        setContentView(R.layout.activity_main);
        refresh = findViewById(R.id.refresh);
        web = findViewById(R.id.web);

        configureWebView();
        askPermissions();

        refresh.setOnRefreshListener(new SwipeRefreshLayout.OnRefreshListener() {
            @Override public void onRefresh() { web.reload(); }
        });

        // El botón atrás navega dentro de la app antes de salir: en un
        // formulario de monitoreo, salirse por accidente es perder el trabajo.
        getOnBackPressedDispatcher().addCallback(this, new OnBackPressedCallback(true) {
            @Override public void handleOnBackPressed() {
                if (web.canGoBack()) {
                    web.goBack();
                } else {
                    setEnabled(false);
                    getOnBackPressedDispatcher().onBackPressed();
                }
            }
        });

        if (savedInstanceState != null) {
            web.restoreState(savedInstanceState);
        } else {
            web.loadUrl(fieldUrl());
        }
    }

    private String fieldUrl() {
        String base = serverUrl.endsWith("/") ? serverUrl.substring(0, serverUrl.length() - 1) : serverUrl;
        return base + "/campo";
    }

    @SuppressWarnings("deprecation")
    private void configureWebView() {
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        // DOM storage e IndexedDB son la cola sin señal: sin esto la app
        // pierde lo cargado en el lote.
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setGeolocationEnabled(true);
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);
        s.setSupportZoom(false);
        s.setMediaPlaybackRequiresUserGesture(false);
        // Se usa la caché del service worker; LOAD_DEFAULT respeta sus reglas.
        s.setCacheMode(WebSettings.LOAD_DEFAULT);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_COMPATIBILITY_MODE);

        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest req) {
                Uri u = req.getUrl();
                String scheme = u.getScheme();
                // El certificado de la CA se abre con el instalador del sistema,
                // no dentro del WebView.
                if (u.toString().endsWith(".crt")) {
                    startActivity(new Intent(Intent.ACTION_VIEW, u));
                    return true;
                }
                if ("http".equals(scheme) || "https".equals(scheme)) {
                    return false;
                }
                try {
                    startActivity(new Intent(Intent.ACTION_VIEW, u));
                } catch (Exception ignored) { }
                return true;
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                refresh.setRefreshing(false);
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest req, WebResourceError err) {
                if (req.isForMainFrame()) {
                    refresh.setRefreshing(false);
                    // Sin señal la PWA se sirve desde su propia caché, así que
                    // un error de red en el marco principal significa que el
                    // servidor no está accesible, no que falte conexión.
                    showConnectionProblem(String.valueOf(err.getDescription()));
                }
            }

            @Override
            public void onReceivedSslError(WebView view, SslErrorHandler handler, SslError error) {
                // Nunca se acepta un certificado inválido en silencio. Si la CA
                // de la finca no está instalada, se explica el paso que falta.
                handler.cancel();
                refresh.setRefreshing(false);
                showCertificateHelp();
            }
        });

        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onGeolocationPermissionsShowPrompt(String origin,
                                                           GeolocationPermissions.Callback cb) {
                boolean granted = ContextCompat.checkSelfPermission(MainActivity.this,
                        Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED;
                cb.invoke(origin, granted, false);
                if (!granted) {
                    Toast.makeText(MainActivity.this, R.string.need_location, Toast.LENGTH_LONG).show();
                    askPermissions();
                }
            }

            @Override
            public void onPermissionRequest(final PermissionRequest request) {
                // Sólo se conceden recursos del propio servidor de la finca.
                if (request.getOrigin().toString().startsWith(serverUrl)) {
                    request.grant(request.getResources());
                } else {
                    request.deny();
                }
            }

            @Override
            public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback,
                                             FileChooserParams params) {
                // Es lo que hace funcionar <input type="file" capture="environment">:
                // sin esto, tocar "Foto" en el formulario no abre nada.
                if (filePicker != null) filePicker.onReceiveValue(null);
                filePicker = callback;
                try {
                    startActivityForResult(params.createIntent(), REQ_FILE);
                } catch (Exception e) {
                    filePicker = null;
                    Toast.makeText(MainActivity.this, R.string.no_camera_app, Toast.LENGTH_LONG).show();
                    return false;
                }
                return true;
            }
        });
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        if (requestCode == REQ_FILE) {
            if (filePicker != null) {
                filePicker.onReceiveValue(
                        WebChromeClient.FileChooserParams.parseResult(resultCode, data));
                filePicker = null;
            }
            return;
        }
        super.onActivityResult(requestCode, resultCode, data);
    }

    private void askPermissions() {
        String[] wanted = {
                Manifest.permission.ACCESS_FINE_LOCATION,
                Manifest.permission.ACCESS_COARSE_LOCATION,
                Manifest.permission.CAMERA
        };
        boolean missing = false;
        for (String p : wanted) {
            if (ContextCompat.checkSelfPermission(this, p) != PackageManager.PERMISSION_GRANTED) {
                missing = true;
                break;
            }
        }
        if (missing) {
            ActivityCompat.requestPermissions(this, wanted, REQ_PERMISSIONS);
        }
    }

    private void showCertificateHelp() {
        new AlertDialog.Builder(this)
                .setTitle(R.string.cert_title)
                .setMessage(getString(R.string.cert_body, serverUrl))
                .setPositiveButton(R.string.cert_download, (d, w) -> {
                    try {
                        startActivity(new Intent(Intent.ACTION_VIEW,
                                Uri.parse(serverUrl + "/agrosuite-ca.crt")));
                    } catch (Exception ignored) { }
                })
                .setNeutralButton(R.string.change_server, (d, w) -> openSetup())
                .setNegativeButton(R.string.retry, (d, w) -> web.loadUrl(fieldUrl()))
                .setCancelable(false)
                .show();
    }

    private void showConnectionProblem(String detail) {
        new AlertDialog.Builder(this)
                .setTitle(R.string.conn_title)
                .setMessage(getString(R.string.conn_body, serverUrl, detail))
                .setPositiveButton(R.string.retry, (d, w) -> web.loadUrl(fieldUrl()))
                .setNegativeButton(R.string.change_server, (d, w) -> openSetup())
                .setCancelable(true)
                .show();
    }

    private void openSetup() {
        startActivity(new Intent(this, SetupActivity.class));
        finish();
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, @NonNull String[] permissions,
                                           @NonNull int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == REQ_PERMISSIONS && web != null) {
            web.reload();
        }
    }

    @Override
    protected void onSaveInstanceState(@NonNull Bundle outState) {
        super.onSaveInstanceState(outState);
        if (web != null) web.saveState(outState);
    }
}
