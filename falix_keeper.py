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
API_KEYS = [key for key in API_KEYS if key]

ONLINE_INTERVAL = 300
OFFLINE_INTERVAL = 120
BUSY_INTERVAL = 120
ERROR_INTERVAL = 300
REQUEST_TIMEOUT = 30
MAX_RETRIES = 2

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

session = requests.Session()


def request_api(method, url, api_key, **kwargs):
    headers = kwargs.pop("headers", {})
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

            if response.status_code == 403:
                raise PermissionError(
                    "HTTP 403: Falix rechazó la petición. "
                    "No se repetirá automáticamente."
                )

            if response.status_code == 401:
                raise PermissionError(
                    "HTTP 401: la clave API no fue aceptada."
                )

            if response.status_code == 429:
                raise RuntimeError(
                    "HTTP 429: Falix limitó las solicitudes. "
                    "Se respetará el límite."
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


def consultar_estado(api_key):
    url = f"{API_BASE}/servers/{SERVER_ID}/status"
    response = request_api("GET", url, api_key)

    payload = response.json()
    data = payload.get("data", payload)

    if not isinstance(data, dict):
        raise ValueError("Respuesta de estado inesperada.")

    return data


def iniciar_servidor(api_key):
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
    return response.json()


def obtener_estado():
    if not API_KEYS:
        raise RuntimeError(
            "Faltan los secretos FALIX_API_KEY y FALIX_API_KEY_2."
        )

    ultimo_error = None

    for api_key in API_KEYS:
        try:
            return consultar_estado(api_key), api_key
        except PermissionError as error:
            ultimo_error = error
            logging.warning("%s", error)

    if ultimo_error:
        raise ultimo_error

    raise RuntimeError("No se pudo consultar el estado.")


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

    # Evita enviar otra orden en el siguiente ciclo
    # si la orden anterior fue aceptada.
    inicio_solicitado = False

    while True:
        espera = ERROR_INTERVAL

        try:
            data, api_key = obtener_estado()
            estado = clasificar_estado(data)

            if estado == "activo":
                logging.info("El servidor está activo.")
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
                        "Ya se solicitó el inicio. "
                        "Se comprobará el estado antes de repetir."
                    )
                    espera = OFFLINE_INTERVAL
                else:
                    logging.warning(
                        "Servidor apagado. Solicitando inicio."
                    )

                    try:
                        iniciar_servidor(api_key)
                        inicio_solicitado = True
                        espera = OFFLINE_INTERVAL

                    except PermissionError as error:
                        logging.error("%s", error)
                        espera = ERROR_INTERVAL

                    except requests.RequestException as error:
                        logging.error(
                            "Fallo de conexión al iniciar: %s",
                            type(error).__name__,
                        )
                        # La orden podría haber llegado a Falix
                        # aunque la respuesta se haya perdido.
                        inicio_solicitado = True
                        espera = ERROR_INTERVAL

            else:
                logging.warning(
                    "Estado desconocido. No se enviará ninguna orden."
                )
                espera = ERROR_INTERVAL

        except Exception as error:
            logging.error(
                "Error: %s: %s",
                type(error).__name__,
                error,
            )
            espera = ERROR_INTERVAL

        logging.info("Próxima comprobación en %s segundos.", espera)
        time.sleep(espera)


if __name__ == "__main__":
    ejecutar()
