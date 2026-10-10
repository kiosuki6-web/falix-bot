
import os
import time
import logging
import requests

SERVER_ID = "3504152"
API_BASE = "https://client.falixnodes.net/api/v2"

API_KEYS = list(dict.fromkeys(
    key for key in (
        os.getenv("FALIX_API_KEY", "").strip(),
        os.getenv("FALIX_API_KEY_2", "").strip(),
    ) if key
))

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
    def __init__(self, status_code, code="unknown", message=""):
        self.status_code = status_code
        self.code = code
        self.message = message
        super().__init__(
            f"HTTP {status_code} | {code} | {message}"
        )


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
                try:
                    body = response.json()
                    error = body.get("error", {})
                    code = error.get("code", "unknown")
                    message = error.get(
                        "message", "Sin mensaje adicional"
                    )
                except (ValueError, AttributeError):
                    code = "unknown"
                    message = "Respuesta de error no JSON"

                # No registrar nunca las claves API.
                logging.error(
                    "Falix rechazó la solicitud: HTTP %s; "
                    "código=%s; motivo=%s",
                    response.status_code,
                    code,
                    message,
                )

                raise ApiAuthError(
                    response.status_code, code, message
                )

            if response.status_code == 429:
                retry_after = response.headers.get(
                    "Retry-After", "no indicado"
                )
                raise RateLimitError(
                    f"HTTP 429; Retry-After={retry_after}"
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
            "Faltan FALIX_API_KEY y FALIX_API_KEY_2 "
            "en GitHub Secrets."
        )

    ultimo_error = None

    for numero, api_key in enumerate(API_KEYS, start=1):
        try:
            resultado = funcion(api_key)

            logging.info(
                "Operación autorizada: %s (clave %s).",
                descripcion,
                numero,
            )
            return resultado, api_key

        except ApiAuthError as error:
            ultimo_error = error
            logging.warning(
                "%s: clave %s rechazada con HTTP %s.",
                descripcion,
                numero,
                error.status_code,
            )

        except RateLimitError:
            # No se cambia de clave para eludir límites.
            raise

    if ultimo_error:
        raise ultimo_error

    raise RuntimeError(
        f"No se pudo completar la operación: {descripcion}"
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
    data, _ = probar_claves(
        consultar_estado_con_clave,
        "consulta del estado",
    )
    return data


def iniciar_con_clave(api_key):
    url = f"{API_BASE}/servers/{SERVER_ID}/power"

    response = request_api(
        "POST",
        url,
        api_key,
        headers={"Content-Type": "application/json"},
        json={"signal": "start"},
    )

    try:
        resultado = response.json()
    except ValueError:
        resultado = {}

    data = resultado.get("data", resultado)
    state = (
        data.get("state", "desconocido")
        if isinstance(data, dict)
        else "desconocido"
    )

    logging.info(
        "Orden de inicio aceptada: HTTP %s; estado=%s.",
        response.status_code,
        state,
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

    busy_words = (
        "restor", "backup", "queue", "queued", "storage",
        "starting", "start_pending", "provision", "install",
        "migrat", "stopping", "restarting",
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

    # Mientras una orden está pendiente, no se envían
    # órdenes repetidas en cada comprobación.
    inicio_solicitado = False

    while True:
        espera = ERROR_INTERVAL

        try:
            data = consultar_estado()
            estado = clasificar_estado(data)

            if estado == "activo":
                logging.info("Servidor activo.")
                inicio_solicitado = False
                espera = ONLINE_INTERVAL

            elif estado == "ocupado":
                logging.info(
                    "Servidor ocupado o en transición; "
                    "no se enviará otra orden."
                )
                espera = BUSY_INTERVAL

            elif estado == "apagado":
                if inicio_solicitado:
                    logging.info(
                        "Ya se solicitó el inicio. "
                        "Se comprobará de nuevo el estado."
                    )
                    espera = OFFLINE_INTERVAL

                else:
                    logging.warning(
                        "Servidor apagado. Intentando encender."
                    )

                    try:
                        iniciar_servidor()
                        inicio_solicitado = True
                        espera = OFFLINE_INTERVAL

                    except ApiAuthError as error:
                        logging.error(
                            "No se autorizó el encendido: "
                            "HTTP %s; código=%s; motivo=%s",
                            error.status_code,
                            error.code,
                            error.message,
                        )
                        espera = ERROR_INTERVAL

                    except RateLimitError as error:
                        logging.error("%s", error)
                        espera = ERROR_INTERVAL

                    except requests.RequestException as error:
                        logging.error(
                            "Error de conexión al encender: %s",
                            type(error).__name__,
                        )
                        # No repetir inmediatamente: la orden
                        # podría haber llegado a Falix.
                        inicio_solicitado = True
                        espera = ERROR_INTERVAL

            else:
                logging.warning(
                    "Estado desconocido. No se enviará "
                    "ninguna orden de encendido."
                )
                espera = ERROR_INTERVAL

        except RateLimitError as error:
            logging.error("%s", error)
            espera = ERROR_INTERVAL

        except Exception as error:
            logging.error(
                "Error al consultar o procesar estado: %s: %s",
                type(error).__name__,
                error,
            )
            espera = ERROR_INTERVAL

        logging.info(
            "Próxima comprobación en %s segundos.", espera
        )
        time.sleep(espera)


if __name__ == "__main__":
    ejecutar()
            
