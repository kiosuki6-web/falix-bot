import os
import time
import logging
import requests

# ==========================================
# CONFIGURACIÓN
# ==========================================

SERVER_ID = "3504152"
API_BASE = "https://client.falixnodes.net/api/v2"

API_KEYS = [
    os.getenv("FALIX_API_KEY", "").strip(),
    os.getenv("FALIX_API_KEY_2", "").strip(),
]
API_KEYS = [key for key in API_KEYS if key]

ONLINE_INTERVAL = 300
OFFLINE_INTERVAL = 120
BUSY_INTERVAL = 120
ERROR_INTERVAL = 300

REQUEST_TIMEOUT = 30
MAX_RETRIES = 2

# ==========================================
# REGISTRO
# ==========================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

session = requests.Session()


# ==========================================
# PETICIONES HTTP CON REINTENTOS LIMITADOS
# ==========================================

def request_api(method, url, api_key, **kwargs):
    headers = kwargs.pop("headers", {})
    headers.update({
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    })

    for intento in range(MAX_RETRIES + 1):
        try:
            response = session.request(
                method,
                url,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
                **kwargs,
            )

            # No repetir automáticamente errores de permisos.
            if response.status_code in (401, 403):
                raise PermissionError(
                    f"Falix rechazó la petición: HTTP {response.status_code}. "
                    "Comprueba los permisos de la clave API."
                )

            if response.status_code == 429:
                raise RuntimeError(
                    "Falix limitó las solicitudes (HTTP 429). "
                    "Se pausará la automatización."
                )

            # Reintentar errores temporales del servidor.
            if response.status_code >= 500:
                if intento < MAX_RETRIES:
                    espera = 5 * (intento + 1)
                    logging.warning(
                        "Error temporal HTTP %s. Reintentando en %s segundos.",
                        response.status_code,
                        espera,
                    )
                    time.sleep(espera)
                    continue

            response.raise_for_status()
            return response

        except (requests.Timeout, requests.ConnectionError) as error:
            if intento >= MAX_RETRIES:
                raise

            espera = 5 * (intento + 1)
            logging.warning(
                "Problema de conexión (%s). Reintentando en %s segundos.",
                type(error).__name__,
                espera,
            )
            time.sleep(espera)

    raise RuntimeError("No se pudo completar la petición.")


# ==========================================
# CONSULTAR ESTADO
# ==========================================

def consultar_estado(api_key):
    url = f"{API_BASE}/servers/{SERVER_ID}/status"

    response = request_api("GET", url, api_key)
    payload = response.json()
    data = payload.get("data", payload)

    if not isinstance(data, dict):
        raise ValueError("La API devolvió un estado inesperado.")

    return data


# ==========================================
# INICIAR SERVIDOR
# ==========================================

def iniciar_servidor(api_key):
    url = f"{API_BASE}/servers/{SERVER_ID}/power"

    response = request_api(
        "POST",
        url,
        api_key,
        headers={"Content-Type": "application/json"},
        json={"signal": "start"},
    )

    logging.info("Respuesta de inicio: HTTP %s", response.status_code)
    logging.info("Falix aceptó la petición de inicio.")
    return True


# ==========================================
# CONSULTAR USANDO LAS CLAVES CONFIGURADAS
# ==========================================

def obtener_estado():
    if not API_KEYS:
        raise RuntimeError(
            "Faltan los secretos FALIX_API_KEY y FALIX_API_KEY_2."
        )

    errores = []

    for api_key in API_KEYS:
        try:
            data = consultar_estado(api_key)
            return data, api_key

        except PermissionError as error:
            errores.append(error)
            logging.warning("%s", error)

        except (requests.RequestException, ValueError) as error:
            # Un fallo de conexión no demuestra que la clave sea inválida.
            raise RuntimeError(
                f"No se pudo consultar el estado: {type(error).__name__}"
            ) from error

    raise PermissionError(
        "Ninguna clave configurada pudo consultar el servidor. "
        "Revisa los permisos y los secretos de GitHub."
    )


# ==========================================
# CLASIFICAR ESTADO
# ==========================================

def clasificar_estado(data):
    state = str(data.get("state", "")).lower().strip()
    lifecycle = str(data.get("lifecycle_status", "")).lower().strip()

    logging.info(
        "Estado: state=%s | lifecycle_status=%s",
        state or "desconocido",
        lifecycle or "desconocido",
    )

    busy_words = (
        "restor",
        "backup",
        "queue",
        "queued",
        "storage",
        "starting",
        "start_pending",
        "provision",
        "install",
        "migrat",
        "stopping",
        "restarting",
    )

    if any(word in lifecycle for word in busy_words):
        return "ocupado"

    if any(word in state for word in busy_words):
        return "ocupado"

    if state in ("running", "online", "started"):
        return "activo"

    if state in ("offline", "stopped", "shutdown"):
        return "apagado"

    return "desconocido"


# ==========================================
# CICLO PRINCIPAL
# ==========================================

def ejecutar():
    logging.info("Falix Keeper iniciado.")
    logging.info("Servidor configurado: %s", SERVER_ID)

    while True:
        espera = ERROR_INTERVAL

        try:
            data, api_key = obtener_estado()
            estado = clasificar_estado(data)

            if estado == "activo":
                logging.info("Servidor activo.")
                espera = ONLINE_INTERVAL

            elif estado == "ocupado":
                logging.info(
                    "Servidor en transición, cola o almacenamiento. "
                    "No se enviará ninguna orden."
                )
                espera = BUSY_INTERVAL

            elif estado == "apagado":
                logging.warning(
                    "Servidor apagado. Se intentará iniciar una vez."
                )

                try:
                    iniciar_servidor(api_key)
                    espera = OFFLINE_INTERVAL

                except PermissionError as error:
                    # Un 403 no se arregla repitiendo la misma orden.
                    logging.error("%s", error)
                    espera = ERROR_INTERVAL

                except RuntimeError as error:
                    logging.error("%s", error)
                    espera = ERROR_INTERVAL

            else:
                logging.warning(
                    "Estado desconocido. No se enviará ninguna orden."
                )
                espera = ERROR_INTERVAL

        except PermissionError as error:
            logging.error("Error de permisos: %s", error)
            espera = ERROR_INTERVAL

        except requests.RequestException as error:
            logging.error(
                "Error de conexión tras los reintentos: %s",
                type(error).__name__,
            )
            espera = ERROR_INTERVAL

        except Exception as error:
            logging.error(
                "Error inesperado: %s: %s",
                type(error).__name__,
                error,
            )
            espera = ERROR_INTERVAL

        logging.info("Próxima comprobación en %s segundos.", espera)
        time.sleep(espera)


if __name__ == "__main__":
    ejecutar()
