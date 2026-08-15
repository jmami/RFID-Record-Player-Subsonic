import json
import os
import RPi.GPIO as GPIO
import threading
import sys
import time
import libsonic
import vlc
import hashlib
import string
import random
from urllib.parse import quote
from dotenv import load_dotenv
from gpiozero import DigitalInputDevice, DigitalOutputDevice
from gpiozero.pins.lgpio import LGPIOFactory
from mfrc522 import SimpleMFRC522

# --- CONFIGURACIÓN DE HARDWARE ---
HALL_SENSOR_PIN = 17
STEPPER_PINS = [14, 15, 18, 23]

# --- GESTIÓN DE RUTAS ---
ENV_FILE = ".env"
RFID_FILE = "rfid.json"

def ensure_pulseaudio():
    """Asegura que PulseAudio está corriendo"""
    print("🔊 Verificando PulseAudio...")
    try:
        subprocess.run(["pulseaudio", "--check"], check=True, capture_output=True)
        print("✅ PulseAudio ya está corriendo")
        return True
    except subprocess.CalledProcessError:
        print("🔄 Iniciando PulseAudio...")
        try:
            subprocess.Popen(
                ["pulseaudio", "--start", "--log-target=syslog"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            time.sleep(2)
            return True
        except Exception as e:
            print(f"⚠️  Error iniciando PulseAudio: {e}")
            return False

def set_bluetooth_as_default_sink():
    """Intenta configurar el dispositivo Bluetooth como sink por defecto"""
    try:
        # Buscar el sink de Bluetooth
        result = subprocess.run(
            ["pactl", "list", "short", "sinks"],
            capture_output=True,
            text=True
        )

        for line in result.stdout.split('\n'):
            if 'bluez_sink' in line:
                sink_name = line.split()[1]
                print(f"🎧 Configurando {sink_name} como salida predeterminada...")
                subprocess.run(["pactl", "set-default-sink", sink_name], check=True)

                # Asegurar que el sink no está muteado y a volumen audible
                print(f"🔊 Desmuteando y ajustando volumen de {sink_name}...")
                subprocess.run(["pactl", "set-sink-mute", sink_name, "0"], check=False)
                subprocess.run(["pactl", "set-sink-volume", sink_name, "100%"], check=False)

                return True

        return False
    except Exception as e:
        print(f"⚠️  Error configurando sink: {e}")
        return False

def wait_for_bt_sink(max_retries=30, delay=2):
    """Espera a que el dispositivo Bluetooth esté disponible"""
    print("🔊 Esperando altavoz Bluetooth...")

    for i in range(max_retries):
        try:
            sinks = subprocess.check_output("pactl list short sinks", shell=True).decode()
            if "bluez_sink" in sinks:
                print("✅ Altavoz Bluetooth detectado.")
                set_bluetooth_as_default_sink()
                return True
        except Exception as e:
            print(f"⏳ Esperando... ({i+1}/{max_retries}) - {e}")

        time.sleep(delay)

    print("⚠️ No se detectó altavoz Bluetooth después de esperar.")
    return False

class SubsonicController:
    def __init__(self):
        self.load_config()
        self.init_subsonic()
        self.init_vlc()
        self.rfid_map = self.load_rfid_map()
        self.current_uri = None

    def load_config(self):
        load_dotenv(ENV_FILE)
        self.server = os.getenv("SUBSONIC_URL")
        self.user = os.getenv("SUBSONIC_USER")
        self.password = os.getenv("SUBSONIC_PASS")
        self.port = os.getenv("SUBSONIC_PORT")

        if not all([self.server, self.user, self.password]):
            print("❌ Error: Faltan credenciales en el archivo .env")
            sys.exit(1)

    def init_subsonic(self):
        try:
            print(f"📡 Conectando a Subsonic: {self.server}")
            self.conn = libsonic.Connection(
                self.server,
                self.user,
                self.password,
                port = self.port,
                appName="JukePi"
            )
            # Pequeño ping para verificar
            if not self.conn.ping():
                print("⚠️ Advertencia: El servidor Subsonic no responde al ping.")
        except Exception as e:
            print(f"❌ Error conectando a Subsonic: {e}")

    def init_vlc(self):
        # Usamos '--aout=alsa' si es necesario forzar, pero pipewire suele manejarlo bien
        # Inicializamos el reproductor de LISTAS (MediaListPlayer)
        self.vlc_instance = vlc.Instance()
        self.list_player = self.vlc_instance.media_list_player_new()
        self.player = self.list_player.get_media_player() # Acceso al reproductor subyacente

    def load_rfid_map(self):
        try:
            with open(RFID_FILE, 'r') as file:
                return json.load(file)
        except (FileNotFoundError, json.JSONDecodeError):
            print("⚠️ No se pudo leer rfid.json o está vacío.")
            return {}

    def _get_auth_params(self):
        """Genera los parámetros de autenticación para la URL de streaming"""
        salt = ''.join(random.choice(string.ascii_letters + string.digits) for _ in range(6))
        token = hashlib.md5((self.password + salt).encode('utf-8')).hexdigest()
        return f"u={quote(self.user)}&t={token}&s={salt}&v=1.16.1&c=RPiPlayer"

    def fetch_songs(self, uri):
        """Devuelve una lista de diccionarios de canciones basada en la URI"""
        # uri formato: subsonic:tipo:id
        try:
            parts = uri.split(":")
            if len(parts) != 3: return []

            otype, oid = parts[1], parts[2]
            songs = []

            if otype == "album":
                print(f"📥 Obteniendo álbum ID {oid}...")
                album = self.conn.getAlbum(oid)
                if 'album' in album and 'song' in album['album']:
                    songs = album['album']['song']

            elif otype == "playlist":
                print(f"📥 Obteniendo playlist ID {oid}...")
                pl = self.conn.getPlaylist(oid)
                if 'playlist' in pl and 'entry' in pl['playlist']:
                    songs = pl['playlist']['entry']

            elif otype == "artist":
                artist = self.conn.getArtist(oid)
                artist_name = artist['artist']['name']
                print(f"📥 Obteniendo canciones del artista {artist_name} ...")

                results = self.conn.search3(artist_name)
                search_result = results.get('searchResult3', {})

                # 🔹 Obtener solo canciones
                just_songs = search_result.get('song', [])

                # 🔹 Filtrar por artista exacto
                songs = [
                    song for song in just_songs
                        if song.get('artist') == artist_name
                ]
                random.shuffle(songs)

            return songs
        except Exception as e:
            print(f"❌ Error obteniendo canciones: {e}")
            return []

    def play(self, rfid_id):
        uri = self.rfid_map.get(str(rfid_id))

        if not uri:
            print(f"⚠️ Etiqueta {rfid_id} no configurada.")
            return

        if uri == self.current_uri:
            print("🔄 Misma etiqueta, ignorando...")
            return

        print(f"▶️ Nueva etiqueta detectada: {uri}")
        # 🔥 PARAR completamente la lista anterior
        self.list_player.stop()
        self.player.stop()  # doble seguro

        self.current_uri = uri

        # 1. Obtener canciones
        songs = self.fetch_songs(uri)
        if not songs:
            print("❌ No se encontraron canciones para reproducir.")
            return

        # 2. Crear lista de reproducción VLC
        media_list = self.vlc_instance.media_list_new()
        auth_params = self._get_auth_params()

        print(f"🎵 Cargando {len(songs)} canciones en cola...")

        for song in songs:
            # Construir URL completa con autenticación
            stream_url = f"{self.server}/rest/stream?id={song['id']}&{auth_params}"
            media = self.vlc_instance.media_new(stream_url)
            media_list.add_media(media)

        # 3. Asignar y reproducir
        self.list_player.set_media_list(media_list)
        self.list_player.play()
        print("🔊 Reproduciendo...")

    def pause(self):
        """Pausa o detiene la reproducción (simulando levantar la aguja)"""
        if self.list_player.is_playing():
            print("⏸️ Pausando reproducción...")
            self.list_player.pause() # O usar .stop() si quieres reiniciar al poner la aguja

    def resume(self):
        """Reanuda si estaba pausado"""
        if not self.list_player.is_playing() and self.current_uri:
             print("▶️ Reanudando...")
             self.list_player.play()

    def stop(self):
        self.list_player.stop()
        self.current_uri = None

class StepperMotor:
    STEP_SEQUENCE = [
        [1,0,0,1], [1,0,0,0], [1,1,0,0], [0,1,0,0],
        [0,1,1,0], [0,0,1,0], [0,0,1,1], [0,0,0,1],
    ]
    STEP_DELAY = 0.002 # Ajustar velocidad aquí

    def __init__(self):
        self.pins = [DigitalOutputDevice(pin) for pin in STEPPER_PINS]
        self._running = False
        self._thread = None

    def _run(self):
        print("⚙️ Motor: Iniciando giro")
        while self._running:
            for step in self.STEP_SEQUENCE:
                for pin, value in zip(self.pins, step):
                    pin.value = value
                time.sleep(self.STEP_DELAY)
        self._stop_pins()
        print("⚙️ Motor: Detenido")

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def _stop_pins(self):
        for pin in self.pins:
            pin.off()

class FakeHallSensor:
    def __init__(self):
        self.value = False

    def activate(self):
        if not self.value: # Solo imprimir si cambia el estado
            print("\n🧪 [MOCK] Brazo bajado (Imán detectado)")
            self.value = True

    def deactivate(self):
        if self.value:
            print("\n🧪 [MOCK] Brazo levantado (Sin imán)")
            self.value = False

class FakeRFID:
    def __init__(self):
        self.fake_id = None
        self._last_read_id = None

    def read_id_no_block(self):
        # Simula comportamiento real: devuelve el ID mientras la tarjeta esté cerca
        return self.fake_id

    def set_id(self, new_id):
        if self.fake_id != new_id:
            print(f"🧪 [MOCK] Acercando etiqueta RFID: {new_id}")
            self.fake_id = new_id

    def remove_card(self):
        if self.fake_id is not None:
            print("🧪 [MOCK] Retirando etiqueta RFID")
            self.fake_id = None

class FakeMotor:
    def start(self):
        print("⚙️ [MOCK] Motor: GIRANDO")

    def stop(self):
        print("⚙️ [MOCK] Motor: DETENIDO")



class RecordPlayer:
    def __init__(self, audio_controller, motor, rfid, hall_sensor):
        self.audio = audio_controller
        self.motor = motor
        self.rfid = rfid
        self.hall_sensor = hall_sensor

        self.current_rfid = None
        self.spinning = False

    def update(self):
        # Leemos el sensor Hall (Brazo del tocadiscos)
        # Nota: pull_up=True significa que detecta imán cuando va a tierra (0) o viceversa
        # Ajusta lógica según tu montaje físico del sensor
        magnet_detected = self.hall_sensor.value

        # ESTADO: COMIENZA A GIRAR (Brazo se mueve hacia el disco)
        if magnet_detected and not self.spinning:
            print("🧲 Brazo activado -> Arrancando motor")
            self.spinning = True
            self.motor.start()
            # Si había música pausada, intentamos reanudar
            self.audio.resume()

        # ESTADO: PARA DE GIRAR (Brazo vuelve al reposo)
        elif not magnet_detected and self.spinning:
            print("🧲 Brazo desactivado -> Deteniendo")
            self.spinning = False
            self.motor.stop()
            self.audio.pause() # O self.audio.stop() para resetear totalmente

        # MIENTRAS GIRA: Escanear etiquetas
        if self.spinning:
            rfid_id = self.rfid.read_id_no_block()
            if rfid_id and rfid_id != self.current_rfid:
                print(f"🏷️ Etiqueta detectada: {rfid_id}")
                self.current_rfid = rfid_id
                self.audio.play(rfid_id)

def main():
    print("=========================================")
    print("   RPi Subsonic Record Player v2.0      ")
    print("=========================================")

    # Inicializar controladores
    try:
        subsonic = SubsonicController()
        motor = StepperMotor()
        rfid = SimpleMFRC522()
        # Ajustar pin_factory si da problemas en Pi Zero 2, LGPIO es el estándar moderno
        hall_sensor = DigitalInputDevice(HALL_SENSOR_PIN, pull_up=True, pin_factory=LGPIOFactory())

        player = RecordPlayer(
            audio_controller=subsonic,
            motor=motor,
            rfid=rfid,
            hall_sensor=hall_sensor,
        )

        print("✅ Sistema listo. Esperando acción del brazo...")

        while True:
            player.update()
            time.sleep(0.1)

    except KeyboardInterrupt:
        print("\n👋 Apagando sistema...")
    except Exception as e:
        print(f"❌ Error inesperado: {e}")
    finally:
        if 'motor' in locals(): motor.stop()
        # GPIO cleanup a veces es redundante con gpiozero, pero por seguridad si usas librerías mixtas
        try:
            import RPi.GPIO as GPIO
            GPIO.cleanup()
        except:
            pass

def main_test():
    print("=========================================")
    print("   MODO TEST: SIMULACIÓN DE HARDWARE     ")
    print("=========================================")

    subsonic = SubsonicController()
    motor = FakeMotor()        # o StepperMotor si quieres
    rfid = FakeRFID() # ID existente en rfid.json
    hall_sensor = FakeHallSensor()

    player = RecordPlayer(
            audio_controller=subsonic,
            motor=motor,
            rfid=rfid,
            hall_sensor=hall_sensor,
        )
# Variables para controlar la línea de tiempo
    start_time = time.time()

    # Flags para que los eventos del test ocurran solo una vez
    events_triggered = {
        "arm_down": False,
        "card_scan": False,
        "arm_up": False,
        "arm_down2": False
    }

    print("⏱️ Iniciando línea de tiempo...")

    try:
        while True:
            # 1. Ejecutar la lógica del reproductor (lo que haría la RPi)
            player.update()

            # 2. Calcular tiempo transcurrido
            elapsed = time.time() - start_time

            # --- GUIÓN DE LA PRUEBA (TIMELINE) ---

            # T+2s: Bajar el brazo (Activar sensor)
            if elapsed > 2 and not events_triggered["arm_down"]:
                hall_sensor.activate()
                events_triggered["arm_down"] = True

            # T+4s: Colocar tarjeta RFID (ID de prueba que tengas en rfid.json)
            # Asegúrate de poner aquí un ID que exista en tu archivo
            if elapsed > 4 and not events_triggered["card_scan"]:
                rfid.set_id(856425748622)
                events_triggered["card_scan"] = True

            # T+20s: Levantar el brazo (Canción termina o usuario para)
            if elapsed > 20 and not events_triggered["arm_up"]:
                hall_sensor.deactivate()
                events_triggered["arm_up"] = True
                print("\n✅ Test finalizado. Saliendo en 3 segundos...")

            if elapsed > 30 and not events_triggered["arm_down2"]:
                hall_sensor.activate()
                events_triggered["arm_down2"] = True

            # Salir del script poco después de terminar
            if elapsed > 40:
                break

            # Pequeña pausa para no saturar la CPU
            time.sleep(0.1)

    except KeyboardInterrupt:
        print("\nTest cancelado por usuario.")

if __name__ == "__main__":
    if os.getenv("MODE") == "test":
        main_test()
    else:
        main()
