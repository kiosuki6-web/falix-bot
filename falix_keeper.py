import os
import time
import logging
import requests

# ==========================================
# CONFIGURACIÓN DEL SERVIDOR
# ==========================================

SERVER_ID = "3504152"
API_BASE = "https://client.falixnodes.net/api/v2"

API_KEYS = [
    os.getenv("FALIX_API_KEY", "").strip(),
    os.getenv("FALIX_API_KEY_2", "").strip(),
]

API_KEYS = [key for key in API_KEYS if key]

# Intervalos de comprobación
ONLINE_INTERVAL = 300       # 5 minutos
OFFLINE_INTERVAL = 120      # 2 minutos
RESTORING_INTERVAL = 300    # 5 minutos
ERROR_INTERVAL = 600        # 10 minutos

REQUEST_TIMEOUT = 20

# ==========================================
# REGISTRO DE ACTIVIDAD
# ==========================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

session = requests.Session()


# ==========================================
# CONSULTAR ESTADO
# ==========================================

def consultar_estado(api_key):
    url = f"{API_BASE}/servers/{SERVER_ID}/status"

    response = session.get(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        },
        timeout=REQUEST_TIMEOUT,
    )

    if response.status_code in (401, 403):
        raise PermissionError(
            f"Falix rechazó la clave o sus permisos: HTTP {response.status_code}"
        )

    if response.status_code == 429:
        raise RuntimeError(
            "Falix ha limitado las solicitudes (HTTP 429)."
        )

    response.raise_for_status()

    payload = response.json()
    data = payload.get("data", payload)

    if not isinstance(data, dict):
        raise ValueError("La respuesta de estado no tiene el formato esperado.")

    return data


# ==========================================
# ENVIAR ORDEN DE INICIO
# ==========================================

def iniciar_servidor(api_key):
    url = f"{API_BASE}/servers/{SERVER_ID}/power"

    response = session.post(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        json={"signal": "start"},
        timeout=REQUEST_TIMEOUT,
    )

    if response.status_code in (401, 403):
        raise PermissionError(
            f"Falix rechazó la orden o sus permisos: HTTP {response.status_code}"
        )

    if response.status_code == 429:
        raise RuntimeError(
            "Falix ha limitado las solicitudes (HTTP 429)."
        )

    response.raise_for_status()

    logging.info("Falix aceptó la orden de inicio.")
    logging.info("Respuesta: %s", response.text[:500])


# ==========================================
# OBTENER ESTADO CON LAS CLAVES CONFIGURADAS
# ==========================================

def consultar_con_claves():
    if not API_KEYS:
        raise RuntimeError(
            "No se encontraron FALIX_API_KEY ni FALIX_API_KEY_2."
        )

    ultimo_error = None

    for indice, api_key in enumerate(API_KEYS):
        try:
            return consultar_estado(api_key), api_key

        except PermissionError as error:
            ultimo_error = error
            logging.error("%s", error)

            # Probar la segunda clave solo ante 401/403.
            # Nunca cambiar de clave para evadir límites de solicitudes.
            if indice < len(API_KEYS) - 1:
                continue

            raise

    raise ultimo_error or RuntimeError("No se pudo consultar el estado.")


# ==========================================
# CLASIFICAR EL ESTADO DEL SERVIDOR
# ==========================================

def clasificar_estado(data):
    state = str(data.get("state", "")).lower().strip()
    lifecycle = str(data.get("lifecycle_status", "")).lower().strip()

    logging.info(
        "Estado: state=%s | lifecycle_status=%s",
        state or "desconocido",
        lifecycle or "desconocido",
    )

    # Si la API indica restauración, cola o almacenamiento,
    # no se debe mandar otra orden de inicio.
    estados_ocupados = (
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
        "suspend",
        "stopping",
        "restarting",
    )

    for valor in (state, lifecycle):
        if any(palabra in valor for palabra in estados_ocupados):
            return "ocupado"

    # Un servidor en ejecución no necesita otra orden de inicio.
    estados_activos = ("running", "online", "started")

    if state in estados_activos:
        return "activo"

    # Solo enviar inicio si la API confirma un estado apagado.
    estados_apagados = (
        "offline",
        "stopped",
        "shutdown",
    )

    if state in estados_apagados:
        return "apagado"

    # Si el estado no es claro, no arriesgar un inicio.
    return "desconocido"


# ==========================================
# CICLO PRINCIPAL
# ==========================================

def ejecutar():
    logging.info("Iniciando Falix Keeper.")
    logging.info("Servidor configurado: %s", SERVER_ID)

    while True:
        try:
            data, api_key = consultar_con_claves()
            estado = clasificar_estado(data)

            if estado == "activo":
                logging.info(
                    "Servidor activo. Próxima comprobación en 5 minutos."
                )
                espera = ONLINE_INTERVAL

            elif estado == "apagado":
                logging.warning(
                    "El servidor está apagado. Enviando orden de inicio."
                )

                iniciar_servidor(api_key)

                # No enviar otra orden inmediatamente.
                # Volver a consultar el estado después de 2 minutos.
                espera = OFFLINE_INTERVAL

            elif estado == "ocupado":
                logging.info(
                    "El servidor está ocupado, en cola o en transición. "
                    "No se enviará otra orden de inicio."
                )
                espera = RESTORING_INTERVAL

            else:
                logging.warning(
                    "Estado desconocido. No se enviará ninguna orden."
                )
                espera = ERROR_INTERVAL

        except PermissionError as error:
            logging.error("Error de autenticación/permisos: %s", error)
            espera = ERROR_INTERVAL

        except requests.RequestException as error:
            logging.error("Error de conexión con Falix: %s", error)
            espera = ERROR_INTERVAL

        except Exception as error:
            logging.error("Error inesperado: %s", error)
            espera = ERROR_INTERVAL

        logging.info("Esperando %s segundos.", espera)
        time.sleep(espera)


if __name__ == "__main__":
    ejecutar()
