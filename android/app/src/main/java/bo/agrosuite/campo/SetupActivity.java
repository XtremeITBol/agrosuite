package bo.agrosuite.campo;

import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.TextUtils;
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
import android.widget.TextView;

import androidx.appcompat.app.AppCompatActivity;

import java.io.IOException;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * Configuración de la dirección del servidor de la finca.
 *
 * El servidor corre en una PC de la finca con una IP de red local, así que no
 * hay una dirección fija que se pueda compilar dentro de la app. Esta pantalla
 * la pide una vez y la verifica de verdad contra /api/health antes de guardar:
 * es preferible fallar acá, con un mensaje claro, que dejar al usuario en una
 * pantalla en blanco en medio de un lote.
 */
public class SetupActivity extends AppCompatActivity {

    private EditText input;
    private TextView status;
    private Button test, save;
    private final ExecutorService pool = Executors.newSingleThreadExecutor();
    private final Handler ui = new Handler(Looper.getMainLooper());
    private boolean verified = false;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_setup);

        input = findViewById(R.id.serverInput);
        status = findViewById(R.id.status);
        test = findViewById(R.id.testBtn);
        save = findViewById(R.id.saveBtn);

        SharedPreferences prefs = getSharedPreferences(MainActivity.PREFS, MODE_PRIVATE);
        String current = prefs.getString(MainActivity.KEY_SERVER, "");
        input.setText(TextUtils.isEmpty(current) ? "https://192.168.1.100:8443" : current);

        test.setOnClickListener(v -> check(false));
        save.setOnClickListener(v -> check(true));
    }

    /** Normaliza lo que tipeó el usuario: la gente escribe "192.168.1.5:8443". */
    private String normalize(String raw) {
        String url = raw.trim();
        if (url.isEmpty()) return null;
        if (!url.startsWith("http://") && !url.startsWith("https://")) {
            url = "https://" + url;
        }
        while (url.endsWith("/")) {
            url = url.substring(0, url.length() - 1);
        }
        return url;
    }

    private void check(final boolean saveOnSuccess) {
        final String url = normalize(input.getText().toString());
        if (url == null) {
            status.setText(R.string.setup_empty);
            return;
        }
        test.setEnabled(false);
        save.setEnabled(false);
        status.setText(R.string.setup_checking);

        pool.execute(() -> {
            String error = null;
            int code = -1;
            HttpURLConnection c = null;
            try {
                c = (HttpURLConnection) new URL(url + "/api/health").openConnection();
                c.setConnectTimeout(6000);
                c.setReadTimeout(6000);
                c.setRequestMethod("GET");
                code = c.getResponseCode();
            } catch (javax.net.ssl.SSLHandshakeException e) {
                // El caso más frecuente: la CA de la finca todavía no está
                // instalada en este teléfono.
                error = getString(R.string.setup_ssl);
            } catch (IOException e) {
                error = getString(R.string.setup_unreachable, String.valueOf(e.getMessage()));
            } catch (Exception e) {
                error = getString(R.string.setup_bad_url);
            } finally {
                if (c != null) c.disconnect();
            }

            final String err = error;
            final int status_code = code;
            ui.post(() -> {
                test.setEnabled(true);
                save.setEnabled(true);
                if (err != null) {
                    verified = false;
                    status.setText(err);
                    return;
                }
                if (status_code != 200) {
                    verified = false;
                    status.setText(getString(R.string.setup_bad_response, status_code));
                    return;
                }
                verified = true;
                status.setText(R.string.setup_ok);
                if (saveOnSuccess) {
                    getSharedPreferences(MainActivity.PREFS, MODE_PRIVATE)
                            .edit().putString(MainActivity.KEY_SERVER, url).apply();
                    startActivity(new Intent(SetupActivity.this, MainActivity.class));
                    finish();
                }
            });
        });
    }

    @Override
    protected void onDestroy() {
        pool.shutdownNow();
        super.onDestroy();
    }
}
