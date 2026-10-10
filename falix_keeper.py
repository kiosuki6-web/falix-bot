import os
import time
import logging
import requests

SERVER_ID = "3504152"
API_BASE = "https://client.falixnodes.net/api/v2"

API_KEYS = [
    os.getenv("FALIX_API_KEY", "").strip(),
    os.getenv("FALIX_API_KEY_2", "").strip(),
]
API_KEYS = list(dict.fromkeys(key for key in API_KEYS if key))

ONLINE_INTERVAL = 300
OFFLINE_INTERVAL = 120
BUSY_INTERVAL = 120
ERROR_INTERVAL = 300

REQUEST_TIMEOUT = 20
MAX_RETRIES = 2

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

session = requests.Session()


class ApiAuthError(Exception):
    def __init__(self, status_code):
        self.status_code = status_code
        super().__init__(f"HTTP {status_code}: autorización rechazada")


class RateLimitError(Exception):
    pass


def request_api(method, url, api_key, **kwargs):
    headers = dict(kwargs.pop("headers", {}))
    headers.update({
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    })

    for attempt in range(MAX_RETRIES + 1):
        try:
            response = session.request(
                method,
                url,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
                **kwargs,
            )

            if response.status_code in (401, 403):
                raise ApiAuthError(response.status_code)

            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After", "")
                raise RateLimitError(
                    f"Falix respondió HTTP 429. "
                    f"Retry-After={retry_after or 'no indicado'}"
                )

            if response.status_code >= 500:
                if attempt < MAX_RETRIES:
                    time.sleep(5 * (attempt + 1))
                    continue

            response.raise_for_status()
            return response

        except (requests.Timeout, requests.ConnectionError):
            if attempt >= MAX_RETRIES:
                raise

            time.sleep(5 * (attempt + 1))

    raise RuntimeError("No se pudo completar la petición.")


def probar_claves(funcion, descripcion):
    if not API_KEYS:
        raise RuntimeError(
            "Faltan FALIX_API_KEY y FALIX_API_KEY_2 en "
            "GitHub Secrets."
        )

    ultimo_error = None

    for numero, api_key in enumerate(API_KEYS, start=1):
        try:
            resultado = funcion(api_key)

            if numero > 1:
                logging.info(
                    "La segunda clave funcionó para: %s.",
                    descripcion,
                )

            return resultado, api_key

        except ApiAuthError as error:
            ultimo_error = error

            logging.warning(
                "%s: la clave %s fue rechazada con HTTP %s.",
                descripcion,
                numero,
                error.status_code,
            )

        except RateLimitError:
            # No cambiar de clave para eludir límites de solicitudes.
            raise

    if ultimo_error:
        raise ultimo_error

    raise RuntimeError(
        f"No se pudo completar la operación: {descripcion}."
    )


def consultar_estado_con_clave(api_key):
    url = f"{API_BASE}/servers/{SERVER_ID}/status"

    response = request_api("GET", url, api_key)
    payload = response.json()
    data = payload.get("data", payload)

    if not isinstance(data, dict):
        raise ValueError("Respuesta de estado inesperada.")

    return data


def consultar_estado():
    data, api_key = probar_claves(
        consultar_estado_con_clave,
        "consulta del estado",
    )
    return data, api_key


def iniciar_con_clave(api_key):
    url = f"{API_BASE}/servers/{SERVER_ID}/power"

    response = request_api(
        "POST",
        url,
        api_key,
        headers={"Content-Type": "application/json"},
        json={"signal": "start"},
    )

    logging.info(
        "Falix respondió HTTP %s a la orden de inicio.",
        response.status_code,
    )

    try:
        resultado = response.json()
    except ValueError:
        resultado = {}

    logging.info(
        "Respuesta de encendido recibida. "
        "Se comprobará el estado antes de emitir otra orden."
    )

    return resultado


def iniciar_servidor():
    resultado, _ = probar_claves(
        iniciar_con_clave,
        "orden de encendido",
    )
    return resultado


def clasificar_estado(data):
    state = str(data.get("state", "")).lower().strip()
    lifecycle = str(
        data.get("lifecycle_status", "")
    ).lower().strip()

    logging.info(
        "Estado: state=%s | lifecycle_status=%s",
        state or "desconocido",
        lifecycle or "desconocido",
    )

    # Si el ciclo de vida indica una transición, no iniciar de nuevo.
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

    for value in (state, lifecycle):
        if any(word in value for word in busy_words):
            return "ocupado"

    if state in ("running", "online", "started"):
        return "activo"

    if state in ("offline", "stopped", "shutdown"):
        return "apagado"

    return "desconocido"


def ejecutar():
    logging.info("Falix Keeper iniciado.")
    logging.info("Servidor configurado: %s", SERVER_ID)

    # Evita repetir órdenes si ya se solicitó el encendido.
    inicio_solicitado = False

    while True:
        espera = ERROR_INTERVAL

        try:
            data, _ = consultar_estado()
            estado = clasificar_estado(data)

            if estado == "activo":
                logging.info("Servidor activo.")
                inicio_solicitado = False
                espera = ONLINE_INTERVAL

            elif estado == "ocupado":
                logging.info(
                    "Servidor ocupado o en transición. "
                    "No se enviará otra orden."
                )
                espera = BUSY_INTERVAL

            elif estado == "apagado":
                if inicio_solicitado:
                    logging.info(
                        "El encendido ya fue solicitado. "
                        "Se comprobará el estado antes de repetir."
                    )
                    espera = OFFLINE_INTERVAL

                else:
                    logging.warning(
                        "Servidor apagado. "
                        "Probando las claves autorizadas para encender."
                    )

                    try:
                        iniciar_servidor()
                        inicio_solicitado = True
                        espera = OFFLINE_INTERVAL

                    except ApiAuthError as error:
                        logging.error(
                            "Las claves disponibles no pudieron "
                            "autorizar el encendido: HTTP %s.",
                            error.status_code,
                        )
                        espera = ERROR_INTERVAL

                    except RateLimitError as error:
                        logging.error("%s", error)
                        espera = ERROR_INTERVAL

                    except requests.RequestException as error:
                        logging.error(
                            "Error de conexión al enviar el encendido: %s.",
                            type(error).__name__,
                        )

                        # La orden podría haber llegado a Falix.
                        # No se repetirá inmediatamente.
                        inicio_solicitado = True
                        espera = ERROR_INTERVAL

            else:
                logging.warning(
                    "Estado desconocido. No se enviará ninguna orden."
                )
                espera = ERROR_INTERVAL

        except RateLimitError as error:
            logging.error("%s", error)
            espera = ERROR_INTERVAL

        except Exception as error:
            logging.error(
                "Error al consultar o procesar el estado: %s: %s",
                type(error).__name__,
                error,
            )
            espera = ERROR_INTERVAL

        logging.info(
            "Próxima comprobación en %s segundos.",
            espera,
        )
        time.sleep(espera)


if __name__ == "__main__":
    ejecutar()
