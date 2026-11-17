#!/usr/bin/env python3
"""Sinal (antes "Placa de Captura").

Mostra a imagem de uma webcam ou placa de captura (ex.: PS5 -> placa HDMI USB)
e devolve o áudio de uma entrada — normalmente o "microfone" da própria placa —
para a caixa de som escolhida.

Dependências (Arch Linux): python-gobject gtk4 libadwaita gstreamer
gst-plugins-base gst-plugins-good e um servidor de som compatível com
PulseAudio (pipewire-pulse). Opcional, mas recomendado: gst-plugin-gtk4
(o vídeo passa a ser desenhado direto pela GPU).

Uso:
    ./sinal.py            abre a janela
    ./sinal.py --listar   lista os dispositivos encontrados e sai
"""

import json
import os
import re
import signal
import sys
import threading
from dataclasses import dataclass

import gi

gi.require_version("Gst", "1.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gst, Gtk, Pango  # noqa: E402

APP_ID = "io.github.sinal.Sinal"
APP_NAME = "Sinal"
APP_VERSION = "1.1"
CONFIG_FILE = os.path.join(GLib.get_user_config_dir(), "sinal", "config.json")
# Configurações da época em que o app se chamava "Placa de Captura":
# lidas uma vez se o arquivo novo ainda não existir.
OLD_CONFIG_FILE = os.path.join(GLib.get_user_config_dir(), "placa-de-captura", "config.json")
ICONS_DIR = os.path.join(os.path.dirname(os.path.realpath(__file__)), "icones")
# Identidade visual: azul "sinal" (o libadwaita deriva dele as outras cores de
# destaque), Noite (fundo escuro) e Névoa (fundo claro). Logotipo e títulos em
# Space Grotesk; o resto da interface em Inter (cada uma cai para a próxima
# fonte da lista se não estiver instalada).
BRAND_ACCENT = "#2F6BFF"
BRAND_NIGHT = "#0F1526"
BRAND_MIST = "#F4F5F8"
APP_TAGLINE = "Seu console na tela do PC. Liga a placa e joga."

# Os dois lados do retorno de áudio (captura e reprodução) entram no mesmo grupo
# do PipeWire. Assim ele usa um único relógio para a placa e para a caixa de som
# (com reamostragem adaptativa) e o atraso não cresce com o tempo. Em um
# PulseAudio "de verdade" essa propriedade é simplesmente ignorada.
AUDIO_NODE_GROUP = "sinal-retorno"

# (rótulo, tamanho do buffer em microssegundos)
AUDIO_LATENCIES = [
    ("Baixo (20 ms)", 20_000),
    ("Normal (40 ms)", 40_000),
    ("Alto (100 ms) — mais estável", 100_000),
]
DEFAULT_LATENCY = 1
MAX_VOLUME_PERCENT = 150
LEVEL_FLOOR_DB = -60.0

RAW_FORMAT_NAMES = {"YUY2": "YUYV", "UYVY": "UYVY", "NV12": "NV12", "I420": "I420", "GRAY8": "P&B"}

# Placas que anunciam formatos que não conseguem entregar de verdade:
# (vendor id, product id) USB -> (largura, altura, fps) preferidos.
# MacroSilicon MS2109 (USB 2.0): em 1080p entrega no máximo 30 quadros reais por
# segundo, mesmo anunciando 60; em 720p entrega 60 de verdade — melhor para jogar.
PREFERRED_FORMATS = {("534d", "2109"): (1280, 720, 60)}

try:
    gi.require_version("GLibUnix", "2.0")
    from gi.repository import GLibUnix  # noqa: E402

    unix_signal_add = GLibUnix.signal_add
except (ImportError, ValueError):
    unix_signal_add = GLib.unix_signal_add


def log(*args):
    print("[sinal]", *args, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# Configurações
# --------------------------------------------------------------------------- #
class Settings:
    """Preferências salvas em ~/.config/sinal/config.json."""

    DEFAULTS = {
        "video_device": None,  # chave do dispositivo de vídeo preferido
        "formats": {},  # chave do dispositivo -> formato escolhido
        "audio_source": None,  # nome interno da entrada de áudio (None = nenhuma)
        "audio_sink": None,  # nome interno da saída (None = padrão do sistema)
        "auto_audio": True,  # seguir a câmera: usar o áudio do mesmo aparelho
        "volume": 100,
        "muted": False,
        "latency": DEFAULT_LATENCY,
    }

    def __init__(self, path=CONFIG_FILE):
        self.path = path
        self.data = json.loads(json.dumps(self.DEFAULTS))
        self._save_id = 0
        source = path
        if path == CONFIG_FILE and not os.path.exists(path) and os.path.exists(OLD_CONFIG_FILE):
            source = OLD_CONFIG_FILE
        try:
            with open(source, encoding="utf-8") as f:
                loaded = json.load(f)
        except (OSError, ValueError):
            loaded = {}
        if isinstance(loaded, dict):
            for key, default in self.DEFAULTS.items():
                value = loaded.get(key, default)
                if default is None:
                    ok = value is None or isinstance(value, str)
                elif isinstance(default, bool):
                    ok = isinstance(value, bool)
                elif isinstance(default, int):
                    # bool é subclasse de int em Python: "volume": true não pode virar 1 %.
                    ok = isinstance(value, (int, float)) and not isinstance(value, bool)
                    value = round(value) if ok else value
                else:
                    ok = isinstance(value, type(default))
                if ok:
                    self.data[key] = value
        self.data["volume"] = max(0, min(MAX_VOLUME_PERCENT, int(self.data["volume"])))
        if not 0 <= self.data["latency"] < len(AUDIO_LATENCIES):
            self.data["latency"] = DEFAULT_LATENCY

    def __getitem__(self, key):
        return self.data[key]

    def __setitem__(self, key, value):
        if self.data.get(key) != value:
            self.data[key] = value
            self._schedule_save()

    def set_format(self, device_key, format_key):
        if self.data["formats"].get(device_key) != format_key:
            self.data["formats"][device_key] = format_key
            self._schedule_save()

    def _schedule_save(self):
        if self._save_id:
            GLib.source_remove(self._save_id)
        self._save_id = GLib.timeout_add(500, self._on_save_timeout)

    def _on_save_timeout(self):
        self._save_id = 0
        self.save()
        return GLib.SOURCE_REMOVE

    def save(self):
        if self._save_id:
            GLib.source_remove(self._save_id)
            self._save_id = 0
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError as e:
            log("não foi possível salvar as configurações:", e)


# --------------------------------------------------------------------------- #
# Dispositivos
# --------------------------------------------------------------------------- #
@dataclass
class Camera:
    key: str  # estável entre reinicializações (nome + porta USB)
    name: str
    path: str  # /dev/videoN
    usb: str | None  # caminho sysfs do aparelho USB
    bus_info: str | None  # ex.: usb-0000:0d:00.3-1
    serial: str | None
    usb_id: tuple[str, str] | None  # (vendor, product) em hexadecimal
    caps: Gst.Caps | None


@dataclass
class AudioDevice:
    key: str  # nome interno usado por pulsesrc/pulsesink
    name: str
    usb: str | None
    bus_info: str | None
    serial: str | None
    is_default: bool


_USB_INTERFACE = re.compile(r"^\d+-[\d.]+:\d+\.\d+$")
_ALSA_BUS_INFO = re.compile(r" at (usb-\S+?),")


def usb_device_path(sysfs_path):
    """Caminho sysfs do aparelho USB, sem a interface.

    O vídeo e o áudio de uma mesma placa de captura são interfaces diferentes
    do mesmo aparelho USB, então compartilham esse caminho.
    """
    if not sysfs_path:
        return None
    if sysfs_path.startswith("/sys/"):
        sysfs_path = sysfs_path[4:]
    parts = sysfs_path.strip("/").split("/")
    for i, part in enumerate(parts):
        if _USB_INTERFACE.match(part):
            return "/".join(parts[:i])
    return None


def device_props(device):
    props = {}
    s = device.get_properties()
    if s is None:
        return props
    for i in range(s.n_fields()):
        name = s.nth_field_name(i)
        try:
            props[name] = s.get_value(name)
        except TypeError:
            pass
    return props


def find_matching_source(camera, sources):
    """Entrada de áudio que pertence ao mesmo aparelho USB da câmera."""
    if camera is None:
        return None
    for attr in ("usb", "bus_info"):
        value = getattr(camera, attr)
        if value:
            for source in sources:
                if getattr(source, attr) == value:
                    return source
    if camera.serial:
        hits = [s for s in sources if s.serial == camera.serial]
        if len(hits) == 1:
            return hits[0]
    return None


class DeviceManager(GObject.Object):
    """Lista câmeras (V4L2) e entradas/saídas de áudio (PulseAudio/PipeWire),
    acompanhando quando algo é conectado ou desconectado."""

    __gsignals__ = {"changed": (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self):
        super().__init__()
        self.cameras, self.sources, self.sinks = [], [], []
        self._providers = []
        self._refresh_id = 0
        wanted = [n for n in ("v4l2deviceprovider", "pulsedeviceprovider") if Gst.DeviceProviderFactory.find(n)]
        if len(wanted) < 2 and Gst.DeviceProviderFactory.find("pipewiredeviceprovider"):
            wanted.append("pipewiredeviceprovider")
        self._has_v4l2 = "v4l2deviceprovider" in wanted
        self._has_pulse = "pulsedeviceprovider" in wanted
        for name in wanted:
            provider = Gst.DeviceProviderFactory.get_by_name(name)
            if provider is None:
                continue
            bus = provider.get_bus()
            bus.add_signal_watch()
            handler = bus.connect("message", self._on_bus_message)
            if not provider.start():
                log("não foi possível monitorar", name)
            self._providers.append((provider, bus, handler))
        self.refresh()

    def shutdown(self):
        if self._refresh_id:
            GLib.source_remove(self._refresh_id)
            self._refresh_id = 0
        for provider, bus, handler in self._providers:
            bus.disconnect(handler)
            bus.remove_signal_watch()
            provider.stop()
        self._providers = []

    def rescan(self):
        """Reinicia os provedores, que voltam a procurar os dispositivos do zero."""
        for provider, _bus, _handler in self._providers:
            provider.stop()
            provider.start()
        self.refresh()

    def _on_bus_message(self, _bus, msg):
        if msg.type in (Gst.MessageType.DEVICE_ADDED, Gst.MessageType.DEVICE_REMOVED, Gst.MessageType.DEVICE_CHANGED):
            if self._refresh_id:
                GLib.source_remove(self._refresh_id)
            self._refresh_id = GLib.timeout_add(300, self._on_refresh_timeout)

    def _on_refresh_timeout(self):
        self._refresh_id = 0
        self.refresh()
        return GLib.SOURCE_REMOVE

    def refresh(self):
        cameras, sources, sinks = {}, {}, {}
        for provider, _bus, _handler in self._providers:
            pname = provider.get_factory().get_name()
            for dev in provider.get_devices():
                props = device_props(dev)
                if dev.has_classes("Video/Source"):
                    if pname == "v4l2deviceprovider":
                        cam = self._camera_from(dev, props, props.get("device.path"), "v4l2.device.")
                    elif pname == "pipewiredeviceprovider" and not self._has_v4l2:
                        cam = self._camera_from(dev, props, props.get("api.v4l2.path"), "api.v4l2.cap.")
                    else:
                        cam = None
                    if cam and cam.path not in cameras:
                        cameras[cam.path] = cam
                elif dev.has_classes("Audio/Source") or dev.has_classes("Audio/Sink"):
                    if pname == "pulsedeviceprovider":
                        key = dev.get_property("internal-name")
                    elif pname == "pipewiredeviceprovider" and not self._has_pulse:
                        key = props.get("node.name")
                    else:
                        key = None
                    if not key:
                        continue
                    is_source = dev.has_classes("Audio/Source")
                    if is_source and (props.get("device.class") == "monitor" or key.endswith(".monitor")):
                        continue  # "monitor" de uma saída: criaria eco
                    long_name = props.get("alsa.long_card_name") or props.get("api.alsa.card.longname") or ""
                    m = _ALSA_BUS_INFO.search(long_name + ",")
                    audio = AudioDevice(
                        key=key,
                        name=dev.get_display_name(),
                        usb=usb_device_path(props.get("sysfs.path")),
                        bus_info=m.group(1) if m else None,
                        serial=props.get("device.serial"),
                        is_default=bool(props.get("is-default", False)),
                    )
                    (sources if is_source else sinks).setdefault(key, audio)

        def by_name(items):
            return sorted(items.values(), key=lambda d: d.name.lower())

        new = (by_name(cameras), by_name(sources), by_name(sinks))
        old_sig = self._signature(self.cameras, self.sources, self.sinks)
        self.cameras, self.sources, self.sinks = new
        if self._signature(*new) != old_sig:
            self.emit("changed")

    @staticmethod
    def _camera_from(dev, props, path, prefix):
        if not path:
            dev_path = dev.find_property("device-path") and dev.get_property("device-path")
            path = dev_path or None
        if not path:
            return None
        bus_info = props.get(prefix + "bus_info")
        name = dev.get_display_name()
        vendor, product = props.get("device.vendor.id"), props.get("device.product.id")
        usb_id = None
        if vendor is not None and product is not None:
            usb_id = tuple(str(v).lower().removeprefix("0x").zfill(4) for v in (vendor, product))
        return Camera(
            key=f"{name}|{bus_info or path}",
            name=name,
            path=path,
            usb=usb_device_path(props.get("sysfs.path")),
            bus_info=bus_info,
            serial=props.get("device.serial"),
            usb_id=usb_id,
            caps=dev.get_caps(),
        )

    @staticmethod
    def _signature(cameras, sources, sinks):
        return (
            tuple((c.key, c.path, c.caps.to_string() if c.caps else "") for c in cameras),
            tuple((s.key, s.name) for s in sources),
            tuple((s.key, s.name, s.is_default) for s in sinks),
        )


# --------------------------------------------------------------------------- #
# Formatos de vídeo
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class VideoFormat:
    media: str  # "image/jpeg" ou "video/x-raw"
    raw_format: str | None
    width: int
    height: int
    fps_n: int
    fps_d: int

    @property
    def fps(self):
        return self.fps_n / self.fps_d if self.fps_d else 0.0

    @property
    def key(self):
        return self.caps_string()

    def caps_string(self):
        fmt = f",format={self.raw_format}" if self.raw_format else ""
        return f"{self.media}{fmt},width={self.width},height={self.height},framerate={self.fps_n}/{self.fps_d}"

    @property
    def label(self):
        if self.media == "image/jpeg":
            kind = "MJPEG"
        else:
            kind = RAW_FORMAT_NAMES.get(self.raw_format, self.raw_format or "RAW")
        return f"{self.width}×{self.height} · {self.fps:.4g} fps · {kind}"


def parse_formats(caps):
    if caps is None or caps.is_empty() or caps.is_any():
        return []
    found = set()
    caps = caps.normalize()
    for i in range(caps.get_size()):
        s = caps.get_structure(i).copy()
        media = s.get_name()
        if media not in ("image/jpeg", "video/x-raw"):
            continue
        features = caps.get_features(i)
        if features is not None and not features.is_any() and not features.contains("memory:SystemMemory"):
            continue
        s.fixate_field_nearest_int("width", 1920)
        s.fixate_field_nearest_int("height", 1080)
        s.fixate_field_nearest_fraction("framerate", 60, 1)
        ok_w, width = s.get_int("width")
        ok_h, height = s.get_int("height")
        ok_f, fps_n, fps_d = s.get_fraction("framerate")
        if not (ok_w and ok_h and ok_f) or fps_n <= 0 or fps_d <= 0:
            continue
        raw = None
        if media == "video/x-raw":
            raw = s.get_string("format")
            if not raw:
                continue
        found.add(VideoFormat(media, raw, width, height, fps_n, fps_d))
    return sorted(found, key=lambda f: (-(f.width * f.height), -f.fps, f.media != "image/jpeg", f.raw_format or ""))


def best_format(formats, camera=None):
    """Maior resolução com movimento fluido (≥ 50 fps em 720p+, senão ≥ 25 fps)."""
    preferred = PREFERRED_FORMATS.get(camera.usb_id) if camera else None
    if preferred:
        for f in formats:  # já ordenados: MJPEG antes de formatos sem compressão
            if (f.width, f.height, round(f.fps)) == preferred:
                return f
    tiers = (
        [f for f in formats if f.fps >= 50 and f.width * f.height >= 1280 * 720],
        [f for f in formats if f.fps >= 25],
        formats,
    )
    pool = next((t for t in tiers if t), None)
    if not pool:
        return None
    return min(pool, key=lambda f: (-(f.width * f.height), -f.fps, f.media != "image/jpeg"))


# --------------------------------------------------------------------------- #
# GStreamer
# --------------------------------------------------------------------------- #
def make_element(factory, name=None, **props):
    element = Gst.ElementFactory.make(factory, name)
    if element is None:
        raise RuntimeError(f"O elemento GStreamer “{factory}” não está instalado.")
    for key, value in props.items():
        key = key.replace("_", "-")
        if isinstance(value, str) and element.find_property(key).value_type.is_a(GObject.TYPE_ENUM):
            Gst.util_set_object_arg(element, key, value)
        else:
            element.set_property(key, value)
    return element


def build_pipeline(name, elements):
    pipeline = Gst.Pipeline.new(name)
    for element in elements:
        pipeline.add(element)
    for a, b in zip(elements, elements[1:]):
        if not a.link(b):
            raise RuntimeError(f"Não foi possível ligar {a.get_name()} → {b.get_name()}.")
    return pipeline


ERROR_MESSAGES = {
    "video": {
        "busy": "A câmera está em uso por outro programa (OBS, navegador, Discord…). Feche-o e tente de novo.",
        "gone": "A câmera não foi encontrada. Ela foi desconectada?",
        "denied": "Sem permissão para abrir a câmera.",
        "prefix": "A câmera",
    },
    "audio": {
        "busy": "O dispositivo de áudio está em uso por outro programa.",
        "gone": "O dispositivo de áudio não foi encontrado. Ele foi desconectado?",
        "denied": "Sem permissão para abrir o dispositivo de áudio.",
        "prefix": "O áudio",
    },
}


def friendly_error(err, debug, kind):
    messages = ERROR_MESSAGES[kind]
    domain, R = Gst.resource_error_quark(), Gst.ResourceError
    text = f"{err.message}\n{debug or ''}".lower()
    if err.matches(domain, R.BUSY) or "busy" in text:
        return messages["busy"]
    if err.matches(domain, R.NOT_FOUND) or any(s in text for s in ("no such", "not found", "no entity", "inexistente")):
        return messages["gone"]
    if err.matches(domain, R.NOT_AUTHORIZED) or "permission" in text or "permiss" in text:
        return messages["denied"]
    if "connection refused" in text or "failed to connect" in text:
        return "Não foi possível falar com o servidor de som (PipeWire/PulseAudio)."
    return f"{messages['prefix']}: {err.message}"


class VideoPlayer(GObject.Object):
    """v4l2src → (jpegdec) → tela.  Usa gtk4paintablesink quando instalado;
    caso contrário, copia os quadros para texturas do GTK (funciona em
    qualquer lugar, mas usa um pouco mais de CPU)."""

    __gsignals__ = {"state-changed": (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self, picture):
        super().__init__()
        self.picture = picture
        self.gpu_sink = Gst.ElementFactory.find("gtk4paintablesink") is not None
        self.pipeline = None
        self.key = None
        self.state = "stopped"  # stopped | starting | playing | waiting | error
        self.error = ""
        self.fps = 0.0
        self.size = None
        self._bus = None
        self._bus_handler = 0
        self._tick_id = 0
        self._frames = 0
        self._last_frames = 0
        self._idle_ticks = 0
        self._last_tick_us = 0
        self._last_start = None  # (câmera, formato) para religar se a imagem travar
        self._stall_restarts = 0
        self._generation = 0
        self._lock = threading.Lock()
        self._pending = None
        self._present_scheduled = False
        self._sink = None

    def start(self, camera, fmt):
        self.stop(emit=False)
        self._generation += 1
        self.key = (camera.key, fmt.key)
        if self._last_start is None or (self._last_start[0].key, self._last_start[1].key) != self.key:
            self._stall_restarts = 0
        self._last_start = (camera, fmt)
        try:
            elements = [
                make_element("v4l2src", "src", device=camera.path),
                make_element("capsfilter", caps=Gst.Caps.from_string(fmt.caps_string())),
                # Se a decodificação atrasar, descarta quadros velhos em vez de acumular atraso.
                make_element("queue", max_size_buffers=2, max_size_bytes=0, max_size_time=0, leaky="downstream"),
            ]
            if fmt.media == "image/jpeg":
                elements.append(make_element("jpegdec"))
            elements.append(make_element("videoconvert", n_threads=min(4, os.cpu_count() or 1)))
            if self.gpu_sink:
                sink = make_element("gtk4paintablesink", "sink", sync=False)
            else:
                elements.append(make_element("capsfilter", caps=Gst.Caps.from_string("video/x-raw,format=RGBA")))
                sink = make_element("appsink", "sink", sync=False, max_buffers=1, drop=True, emit_signals=True)
                sink.connect("new-sample", self._on_new_sample, self._generation)
            elements.append(sink)
            pipeline = build_pipeline("video", elements)
        except (RuntimeError, GLib.Error) as e:
            self._fail(str(e))
            return

        self.pipeline = pipeline
        self._sink = sink
        if self.gpu_sink:
            self.picture.set_paintable(sink.get_property("paintable"))
        sink.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, self._count_frame)
        # A MS2109 em 1080p manda cada quadro duas vezes (mesmo PTS): descarta a
        # cópia antes de decodificar, economizando metade do trabalho.
        last_pts = [Gst.CLOCK_TIME_NONE]

        def drop_repeated(_pad, info):
            pts = info.get_buffer().pts
            if pts != Gst.CLOCK_TIME_NONE and pts == last_pts[0]:
                return Gst.PadProbeReturn.DROP
            last_pts[0] = pts
            return Gst.PadProbeReturn.OK

        pipeline.get_by_name("src").get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, drop_repeated)
        self._bus = pipeline.get_bus()
        self._bus.add_signal_watch()
        self._bus_handler = self._bus.connect("message", self._on_bus_message)
        self._frames = self._last_frames = self._idle_ticks = 0
        self._last_tick_us = GLib.get_monotonic_time()
        self._set_state("starting")
        self._tick_id = GLib.timeout_add(1000, self._on_tick)
        log(f"vídeo: {camera.path} {fmt.caps_string()} ({'gtk4paintablesink' if self.gpu_sink else 'appsink'})")
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            # A mensagem de erro detalhada chega pelo bus; esta é só uma garantia.
            GLib.idle_add(self._fail_if_not_running, self._generation)

    def stop(self, emit=True):
        self._generation += 1
        if self._tick_id:
            GLib.source_remove(self._tick_id)
            self._tick_id = 0
        if self.pipeline is not None:
            self.pipeline.set_state(Gst.State.NULL)
            self._bus.disconnect(self._bus_handler)
            self._bus.remove_signal_watch()
            self.pipeline = self._bus = self._sink = None
        with self._lock:
            self._pending = None
        self.picture.set_paintable(None)
        self.key = None
        self.fps = 0.0
        self.size = None
        self.error = ""
        changed = self.state != "stopped"
        self.state = "stopped"
        if emit and changed:
            self.emit("state-changed")

    def _set_state(self, state):
        self.state = state
        self.emit("state-changed")

    def _fail(self, message):
        log("vídeo: erro:", message)
        key = self.key
        self.stop(emit=False)
        self.key = key  # lembra o que falhou para não ficar tentando à toa
        self.error = message
        self._set_state("error")

    def _fail_if_not_running(self, generation):
        if generation == self._generation and self.state == "starting":
            ok, state, _pending = self.pipeline.get_state(0)
            if state != Gst.State.PLAYING:
                self._fail("Não foi possível iniciar a câmera.")
        return GLib.SOURCE_REMOVE

    def _on_bus_message(self, _bus, msg):
        if msg.type == Gst.MessageType.ERROR:
            err, debug = msg.parse_error()
            log("vídeo:", err.message, debug)
            self._fail(friendly_error(err, debug, "video"))
        elif msg.type == Gst.MessageType.WARNING:
            err, debug = msg.parse_warning()
            log("vídeo (aviso):", err.message)

    def _count_frame(self, _pad, _info):
        self._frames += 1
        return Gst.PadProbeReturn.OK

    def _on_tick(self):
        frames, now = self._frames, GLib.get_monotonic_time()
        elapsed = max(now - self._last_tick_us, 1)
        # Mede pelo relógio: o laço principal pode atrasar o tique de 1 s.
        self.fps = (frames - self._last_frames) * 1_000_000 / elapsed
        self._last_frames, self._last_tick_us = frames, now
        if self.fps > 0:
            self._idle_ticks = 0
            self._stall_restarts = 0
            if self.size is None and self._sink is not None:
                caps = self._sink.get_static_pad("sink").get_current_caps()
                if caps:
                    s = caps.get_structure(0)
                    self.size = (s.get_int("width")[1], s.get_int("height")[1])
            self._set_state("playing")
        else:
            self._idle_ticks += 1
            # Sem quadros (ex.: link USB travou depois de suspender o PC): religa,
            # esperando cada vez mais entre as tentativas (5 s, 10 s, 20 s… até 60 s).
            if self._idle_ticks >= min(60, 5 * 2 ** self._stall_restarts) and self._last_start:
                self._stall_restarts += 1
                log("vídeo: sem imagem há", self._idle_ticks, "s; reabrindo a câmera")
                self.start(*self._last_start)
                return GLib.SOURCE_REMOVE  # start() criou um novo tique
            if self.state == "playing" or self._idle_ticks >= 3:
                self._set_state("waiting")
            else:
                self.emit("state-changed")
        return GLib.SOURCE_CONTINUE

    # ---- caminho sem gtk4paintablesink ------------------------------------ #
    def _on_new_sample(self, sink, generation):
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        s = sample.get_caps().get_structure(0)
        width, height = s.get_int("width")[1], s.get_int("height")[1]
        size = buf.get_size()
        if width <= 0 or height <= 0 or size < width * height * 4:
            return Gst.FlowReturn.OK
        data = buf.extract_dup(0, size)
        with self._lock:
            self._pending = (generation, width, height, size // height, data)
            schedule = not self._present_scheduled
            self._present_scheduled = True
        if schedule:
            GLib.idle_add(self._present_frame, priority=GLib.PRIORITY_HIGH_IDLE)
        return Gst.FlowReturn.OK

    def _present_frame(self):
        with self._lock:
            frame, self._pending = self._pending, None
            self._present_scheduled = False
        if frame is None or frame[0] != self._generation:
            return GLib.SOURCE_REMOVE
        _gen, width, height, stride, data = frame
        texture = Gdk.MemoryTexture.new(width, height, Gdk.MemoryFormat.R8G8B8A8, GLib.Bytes.new(data), stride)
        self.picture.set_paintable(texture)
        return GLib.SOURCE_REMOVE


class AudioPlayer(GObject.Object):
    """pulsesrc (entrada) → nível → volume → pulsesink (saída)."""

    __gsignals__ = {
        "state-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "level": (GObject.SignalFlags.RUN_FIRST, None, (float,)),
    }

    def __init__(self):
        super().__init__()
        self.pipeline = None
        self.key = None
        self.state = "stopped"  # stopped | playing | error
        self.error = ""
        self._bus = None
        self._bus_handler = 0
        self._volume = None
        self._volume_value = 1.0
        self._muted = False

    def start(self, source, sink, latency_us):
        self.stop(emit=False)
        self.key = (source, sink, latency_us)
        latency_time = max(5_000, latency_us // 4)
        try:
            stream_props = Gst.Structure.new_empty("props")
            stream_props.set_value("node.group", AUDIO_NODE_GROUP)
            src = make_element(
                "pulsesrc", "src",
                device=source, client_name=APP_NAME,
                buffer_time=latency_us, latency_time=latency_time,
                stream_properties=stream_props,
            )
            out = make_element(
                "pulsesink", "sink",
                client_name=APP_NAME,
                buffer_time=latency_us, latency_time=latency_time,
                sync=False,  # toca assim que chega: menor atraso possível
                stream_properties=stream_props,
            )
            if sink:
                out.set_property("device", sink)
            self._volume = make_element("volume", volume=self._volume_value, mute=self._muted)
            pipeline = build_pipeline("audio", [
                src,
                make_element("capsfilter", caps=Gst.Caps.from_string("audio/x-raw,rate=48000")),
                make_element("audioconvert"),
                make_element("level", interval=50 * Gst.MSECOND, post_messages=True),
                self._volume,
                make_element("audioconvert"),
                out,
            ])
        except (RuntimeError, GLib.Error) as e:
            self._fail(str(e))
            return
        self.pipeline = pipeline
        self._bus = pipeline.get_bus()
        self._bus.add_signal_watch()
        self._bus_handler = self._bus.connect("message", self._on_bus_message)
        log(f"áudio: {source} → {sink or 'saída padrão'} (buffer {latency_us // 1000} ms)")
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            GLib.idle_add(self._fail_if_failed, pipeline)
        self.state = "playing"
        self.error = ""
        self.emit("state-changed")

    def stop(self, emit=True):
        if self.pipeline is not None:
            self.pipeline.set_state(Gst.State.NULL)
            self._bus.disconnect(self._bus_handler)
            self._bus.remove_signal_watch()
            self.pipeline = self._bus = self._volume = None
            self.emit("level", LEVEL_FLOOR_DB)
        self.key = None
        self.error = ""
        changed = self.state != "stopped"
        self.state = "stopped"
        if emit and changed:
            self.emit("state-changed")

    def set_volume(self, value):
        self._volume_value = value
        if self._volume is not None:
            self._volume.set_property("volume", value)

    def set_muted(self, muted):
        self._muted = muted
        if self._volume is not None:
            self._volume.set_property("mute", muted)

    def _fail(self, message):
        log("áudio: erro:", message)
        key = self.key
        self.stop(emit=False)
        self.key = key
        self.state = "error"
        self.error = message
        self.emit("state-changed")

    def _fail_if_failed(self, pipeline):
        if pipeline is self.pipeline and self.state == "playing":
            _ok, state, _pending = pipeline.get_state(0)
            if state != Gst.State.PLAYING:
                self._fail("Não foi possível iniciar o retorno de áudio.")
        return GLib.SOURCE_REMOVE

    def _on_bus_message(self, _bus, msg):
        if msg.type == Gst.MessageType.ELEMENT:
            s = msg.get_structure()
            if s is not None and s.get_name() == "level":
                peaks = s.get_value("peak")
                if peaks:
                    self.emit("level", max(peaks))
        elif msg.type == Gst.MessageType.ERROR:
            err, debug = msg.parse_error()
            log("áudio:", err.message, debug)
            self._fail(friendly_error(err, debug, "audio"))
        elif msg.type == Gst.MessageType.WARNING:
            err, _debug = msg.parse_warning()
            log("áudio (aviso):", err.message)


# --------------------------------------------------------------------------- #
# Interface
# --------------------------------------------------------------------------- #
CSS = """
@define-color accent_bg_color %(accent)s;
:root { --accent-bg-color: %(accent)s; }
window.sinal { font-family: "Inter", sans-serif; }
.sinal-wordmark {
  font-family: "Space Grotesk", "Inter", sans-serif;
  font-weight: 700;
  letter-spacing: -0.045em;
}
.sinal-wordmark.header { font-size: 17px; }
window.sinal statuspage .title,
window.sinal preferencesgroup .heading { font-family: "Space Grotesk", "Inter", sans-serif; }
.sinal-stage { background-color: %(mist)s; }
.sinal-stage .title { color: %(night)s; }
window.sinal.dark .sinal-stage { background-color: %(night)s; }
window.sinal.dark .sinal-stage .title { color: white; }
.video-area { background-color: black; }
.level-meter block.filled { background-color: @success_color; }
"""


def wrapping_factory():
    factory = Gtk.SignalListItemFactory()

    def setup(_factory, item):
        item.set_child(Gtk.Label(xalign=0, wrap=True, max_width_chars=40))

    def bind(_factory, item):
        item.get_child().set_label(item.get_item().get_string())

    factory.connect("setup", setup)
    factory.connect("bind", bind)
    return factory


def combo_row(title):
    # O valor escolhido aparece inteiro como subtítulo (nomes de dispositivos são longos).
    row = Adw.ComboRow(title=title, use_subtitle=True)
    row.set_list_factory(wrapping_factory())
    return row


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app, settings, devices):
        super().__init__(application=app, title=APP_NAME)
        self.settings = settings
        self.devices = devices
        self._updating = 0
        self._cameras, self._formats, self._sources, self._sinks = [], [], [], []
        self._inhibit_cookie = 0
        self._cursor_timeout = 0
        self._sidebar_before_fullscreen = True
        self._last_audio_error = ""

        self.set_default_size(1280, 760)
        self._build_ui()

        self.video = VideoPlayer(self.picture)
        self.video.connect("state-changed", self._on_video_state)
        self.audio = AudioPlayer()
        self.audio.set_volume(settings["volume"] / 100)
        self.audio.set_muted(settings["muted"])
        self.audio.connect("state-changed", self._on_audio_state)
        self.audio.connect("level", self._on_audio_level)

        self.devices.connect("changed", self._on_devices_changed)
        self.connect("close-request", self._on_close_request)
        self.connect("notify::fullscreened", self._on_fullscreen_changed)

        self._on_devices_changed()
        if not self.video.gpu_sink:
            log("gtk4paintablesink não encontrado; usando cópia de quadros (instale gst-plugin-gtk4 para usar a GPU)")

    # ---- construção da janela ------------------------------------------- #
    def _build_ui(self):
        css = Gtk.CssProvider()
        css.load_from_string(CSS % {"accent": BRAND_ACCENT, "night": BRAND_NIGHT, "mist": BRAND_MIST})
        self.add_css_class("sinal")
        # Fundo das telas de estado: Névoa no tema claro, Noite no escuro.
        style = Adw.StyleManager.get_default()
        style.connect("notify::dark", self._on_dark_changed)
        self._on_dark_changed(style)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        # Área do vídeo
        self.picture = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, can_shrink=True, hexpand=True, vexpand=True)
        self.spinner = Adw.Spinner(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER, width_request=48, height_request=48)
        self.video_area = Gtk.Overlay(child=self.picture)
        self.video_area.add_css_class("video-area")
        self.video_area.add_overlay(self.spinner)

        click = Gtk.GestureClick()
        click.connect("pressed", self._on_video_clicked)
        self.video_area.add_controller(click)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_video_motion)
        self.video_area.add_controller(motion)

        self.empty_page = Adw.StatusPage(
            icon_name=APP_ID,
            title="Nenhuma câmera encontrada",
            description="Conecte a placa de captura (ou uma webcam) no USB. Ela aparece aqui sozinha.",
            css_classes=["sinal-stage"],
        )
        retry = Gtk.Button(label="Tentar de novo", halign=Gtk.Align.CENTER, css_classes=["pill", "suggested-action"])
        retry.connect("clicked", lambda *_: self._apply_video(force=True))
        self.error_page = Adw.StatusPage(
            icon_name="dialog-warning-symbolic", title="Não foi possível abrir a câmera", css_classes=["sinal-stage"]
        )
        self.error_page.set_child(retry)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_named(self.video_area, "video")
        self.stack.add_named(self.empty_page, "empty")
        self.stack.add_named(self.error_page, "error")
        self.toasts = Adw.ToastOverlay(child=self.stack)

        # Painel lateral
        page = Adw.PreferencesPage()

        video_group = Adw.PreferencesGroup(title="Vídeo")
        self.camera_row = combo_row("Câmera / placa de captura")
        self.camera_row.connect("notify::selected", self._on_camera_selected)
        self.format_row = combo_row("Resolução e quadros por segundo")
        self.format_row.connect("notify::selected", self._on_format_selected)
        video_group.add(self.camera_row)
        video_group.add(self.format_row)
        page.add(video_group)

        audio_group = Adw.PreferencesGroup(
            title="Áudio",
            description="O som da entrada escolhida sai pela caixa de som escolhida.",
        )
        self.source_row = combo_row("Entrada (microfone)")
        self.source_row.connect("notify::selected", self._on_source_selected)
        self.auto_row = Adw.SwitchRow(
            title="Usar o áudio da própria placa",
            subtitle="Ao trocar de câmera, escolhe a entrada do mesmo aparelho",
            active=self.settings["auto_audio"],
        )
        self.auto_row.connect("notify::active", self._on_auto_toggled)
        self.sink_row = combo_row("Saída (caixa de som)")
        self.sink_row.connect("notify::selected", self._on_sink_selected)

        volume_row = Adw.ActionRow(title="Volume")
        self.volume_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, MAX_VOLUME_PERCENT, 1)
        self.volume_scale.set_hexpand(True)
        self.volume_scale.set_valign(Gtk.Align.CENTER)
        self.volume_scale.set_size_request(130, -1)
        self.volume_scale.add_mark(100, Gtk.PositionType.BOTTOM, None)
        self.volume_scale.set_value(self.settings["volume"])
        self.volume_scale.connect("value-changed", self._on_volume_changed)
        self.mute_button = Gtk.ToggleButton(valign=Gtk.Align.CENTER, css_classes=["flat"], tooltip_text="Silenciar (Ctrl+M)")
        self.mute_button.set_active(self.settings["muted"])
        self.mute_button.connect("toggled", self._on_mute_toggled)
        self._update_mute_icon()
        volume_row.add_suffix(self.volume_scale)
        volume_row.add_suffix(self.mute_button)

        level_row = Adw.ActionRow(title="Nível", tooltip_text="Nível do som que chega pela entrada escolhida")
        self.level_bar = Gtk.LevelBar(min_value=0, max_value=1, valign=Gtk.Align.CENTER, hexpand=True)
        for name in ("low", "high", "full"):
            self.level_bar.remove_offset_value(name)
        self.level_bar.add_css_class("level-meter")
        self.level_bar.set_size_request(160, -1)
        level_row.add_suffix(self.level_bar)

        self.latency_row = Adw.ComboRow(
            title="Buffer do áudio",
            use_subtitle=True,
            tooltip_text="Aumente se o som falhar ou estalar; diminua para menos atraso",
            model=Gtk.StringList.new([label for label, _ in AUDIO_LATENCIES]),
            selected=self.settings["latency"],
        )
        self.latency_row.connect("notify::selected", self._on_latency_selected)

        for row in (self.source_row, self.auto_row, self.sink_row, volume_row, level_row, self.latency_row):
            audio_group.add(row)
        page.add(audio_group)

        tips_group = Adw.PreferencesGroup()
        tips = Adw.ExpanderRow(title="Sem imagem do PS5?", subtitle="Dicas rápidas")
        for title, subtitle in (
            ("Desative o HDCP no PS5", "Configurações › Sistema › HDMI › Ativar HDCP: desligado"),
            ("Barras coloridas = sem sinal", "Confira o cabo HDMI e se o PS5 está ligado"),
            ("Imagem travando em 1080p?", "Placas USB 2.0 (MS2109) só entregam 30 fps em 1080p; use 1280×720 a 60 fps"),
            ("Tela cheia", "F11 ou clique duas vezes na imagem; Esc para sair"),
        ):
            tips.add_row(Adw.ActionRow(title=title, subtitle=subtitle, subtitle_selectable=True))
        tips_group.add(tips)
        page.add(tips_group)

        self.split = Adw.OverlaySplitView(
            sidebar=page,
            content=self.toasts,
            sidebar_position=Gtk.PackType.END,
            min_sidebar_width=320,
            max_sidebar_width=400,
        )
        self.split.connect("notify::collapsed", self._on_split_collapsed)

        # Barra de título
        # Assinatura (ícone + "sinal") com o estado da câmera embaixo.
        brand = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER)
        brand.append(Gtk.Image(icon_name=APP_ID, pixel_size=18))
        brand.append(Gtk.Label(label="sinal", css_classes=["sinal-wordmark", "header"]))
        self.status_label = Gtk.Label(css_classes=["caption", "dim-label"], ellipsize=Pango.EllipsizeMode.END, visible=False)
        title = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
        title.append(brand)
        title.append(self.status_label)
        header = Adw.HeaderBar(title_widget=title)
        sidebar_button = Gtk.ToggleButton(icon_name="sidebar-show-right-symbolic", tooltip_text="Configurações (F9)")
        self.split.bind_property(
            "show-sidebar", sidebar_button, "active",
            GObject.BindingFlags.SYNC_CREATE | GObject.BindingFlags.BIDIRECTIONAL,
        )
        fullscreen_button = Gtk.Button(icon_name="view-fullscreen-symbolic", tooltip_text="Tela cheia (F11)")
        fullscreen_button.set_action_name("win.fullscreen")
        menu = Gio.Menu()
        menu.append("Procurar dispositivos de novo", "win.refresh")
        menu.append(f"Sobre o {APP_NAME}", "app.about")
        menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, primary=True)
        header.pack_end(menu_button)
        header.pack_end(sidebar_button)
        header.pack_end(fullscreen_button)

        self.toolbar = Adw.ToolbarView(content=self.split)
        self.toolbar.add_top_bar(header)
        self.set_content(self.toolbar)

        breakpoint = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 760sp"))
        breakpoint.add_setter(self.split, "collapsed", True)
        self.add_breakpoint(breakpoint)

        # Ações e atalhos
        for name, callback in (
            ("fullscreen", lambda *_: self._toggle_fullscreen()),
            ("sidebar", lambda *_: self.split.set_show_sidebar(not self.split.get_show_sidebar())),
            ("mute", lambda *_: self.mute_button.set_active(not self.mute_button.get_active())),
            ("refresh", lambda *_: self._refresh_devices()),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key_pressed)
        self.add_controller(keys)

    def _on_dark_changed(self, style, *_):
        if style.get_dark():
            self.add_css_class("dark")
        else:
            self.remove_css_class("dark")

    # ---- dispositivos ---------------------------------------------------- #
    def _refresh_devices(self):
        self.devices.rescan()
        self._apply_video(force=self.video.state in ("error", "waiting"))
        self._apply_audio(force=self.audio.state == "error")
        self.toasts.add_toast(Adw.Toast(title="Lista de dispositivos atualizada", timeout=2))

    def _on_devices_changed(self, *_):
        self._updating += 1
        try:
            self._sync_camera_row()
            self._sync_format_row()
            self._sync_source_row()
            self._sync_sink_row()
        finally:
            self._updating -= 1
        # Um dispositivo que voltou pode resolver um erro anterior.
        self._apply_video(force=self.video.state == "error")
        self._apply_audio(force=self.audio.state == "error")

    def _set_model(self, row, labels, selected, placeholder):
        row.set_model(Gtk.StringList.new(labels or [placeholder]))
        row.set_sensitive(bool(labels))
        row.set_selected(selected if labels else 0)

    def _find_camera(self, key):
        if not key:
            return None
        index = next((i for i, c in enumerate(self._cameras) if c.key == key), None)
        if index is None:
            # Mesmo aparelho em outra porta USB?
            name = key.split("|", 1)[0]
            index = next((i for i, c in enumerate(self._cameras) if c.name == name), None)
        return index

    def _sync_camera_row(self):
        self._cameras = list(self.devices.cameras)
        preferred = self.settings["video_device"]
        index = self._find_camera(preferred)
        if index is None and self.video.key:
            # Continua na câmera que já está aberta (ex.: uma webcam acabou de ser
            # conectada e ficou antes na ordem alfabética).
            index = next((i for i, c in enumerate(self._cameras) if c.key == self.video.key[0]), None)
        if index is None:
            # Prefere uma placa de captura conhecida a uma webcam qualquer.
            index = next((i for i, c in enumerate(self._cameras) if c.usb_id in PREFERRED_FORMATS), 0)
        if preferred is None and self._cameras:
            # Primeira vez: a escolha automática vira a preferida, para não trocar
            # sozinha quando outra câmera for conectada depois.
            self.settings["video_device"] = self._cameras[index].key
        self._set_model(self.camera_row, [c.name for c in self._cameras], index, "Nenhuma câmera encontrada")

    def _sync_format_row(self):
        camera = self._selected_camera()
        self._formats = parse_formats(camera.caps) if camera else []
        index = 0
        if camera and self._formats:
            saved = self.settings["formats"].get(camera.key)
            best = best_format(self._formats, camera)
            keys = [f.key for f in self._formats]
            index = keys.index(saved) if saved in keys else self._formats.index(best)
        self._set_model(self.format_row, [f.label for f in self._formats], index, "—")

    def _sync_source_row(self):
        self._sources = list(self.devices.sources)
        key = self.settings["audio_source"]
        # Só segue a câmera preferida. Se ela sumiu e outra entrou no lugar (ex.:
        # uma webcam), não liga o microfone dessa outra na caixa de som (eco) nem
        # troca a entrada salva.
        if self.settings["auto_audio"] and self._find_camera(self.settings["video_device"]) is not None:
            match = find_matching_source(self._selected_camera(), self._sources)
            if match:
                key = match.key
                self.settings["audio_source"] = key
        keys = [s.key for s in self._sources]
        index = keys.index(key) + 1 if key in keys else 0
        self.source_row.set_model(Gtk.StringList.new(["Nenhuma (sem retorno de áudio)"] + [s.name for s in self._sources]))
        self.source_row.set_selected(index)

    def _sync_sink_row(self):
        self._sinks = list(self.devices.sinks)
        default = next((s.name for s in self._sinks if s.is_default), None)
        first = f"Padrão do sistema ({default})" if default else "Padrão do sistema"
        keys = [s.key for s in self._sinks]
        key = self.settings["audio_sink"]
        index = keys.index(key) + 1 if key in keys else 0
        self.sink_row.set_model(Gtk.StringList.new([first] + [s.name for s in self._sinks]))
        self.sink_row.set_selected(index)

    def _selected_camera(self):
        i = self.camera_row.get_selected()
        return self._cameras[i] if 0 <= i < len(self._cameras) else None

    def _selected_format(self):
        i = self.format_row.get_selected()
        return self._formats[i] if 0 <= i < len(self._formats) else None

    def _selected_source(self):
        i = self.source_row.get_selected() - 1
        return self._sources[i].key if 0 <= i < len(self._sources) else None

    def _selected_sink(self):
        i = self.sink_row.get_selected() - 1
        return self._sinks[i].key if 0 <= i < len(self._sinks) else None

    # ---- respostas às escolhas do usuário -------------------------------- #
    def _on_camera_selected(self, *_):
        if self._updating:
            return
        camera = self._selected_camera()
        if camera is None:
            return
        self.settings["video_device"] = camera.key
        self._updating += 1
        try:
            self._sync_format_row()
            if self.settings["auto_audio"]:
                self._sync_source_row()
        finally:
            self._updating -= 1
        self._apply_video()
        self._apply_audio()

    def _on_format_selected(self, *_):
        if self._updating:
            return
        camera, fmt = self._selected_camera(), self._selected_format()
        if camera and fmt:
            self.settings.set_format(camera.key, fmt.key)
            self._apply_video()

    def _on_source_selected(self, *_):
        if self._updating:
            return
        key = self._selected_source()
        self.settings["audio_source"] = key
        if self.settings["auto_audio"]:
            match = find_matching_source(self._selected_camera(), self._sources)
            if match is None or match.key != key:
                # Escolha manual diferente da automática: desliga o modo automático.
                self.auto_row.set_active(False)
        self._apply_audio()

    def _on_auto_toggled(self, *_):
        self.settings["auto_audio"] = self.auto_row.get_active()
        if self._updating or not self.settings["auto_audio"]:
            return
        self._updating += 1
        try:
            self._sync_source_row()
        finally:
            self._updating -= 1
        self._apply_audio()

    def _on_sink_selected(self, *_):
        if self._updating:
            return
        self.settings["audio_sink"] = self._selected_sink()
        self._apply_audio()

    def _on_latency_selected(self, *_):
        self.settings["latency"] = self.latency_row.get_selected()
        self._apply_audio()

    def _on_volume_changed(self, scale):
        value = round(scale.get_value())
        self.settings["volume"] = value
        self.audio.set_volume(value / 100)

    def _on_mute_toggled(self, button):
        self.settings["muted"] = button.get_active()
        self.audio.set_muted(button.get_active())
        self._update_mute_icon()

    def _update_mute_icon(self):
        muted = self.mute_button.get_active()
        self.mute_button.set_icon_name("audio-volume-muted-symbolic" if muted else "audio-volume-high-symbolic")

    # ---- liga/desliga os pipelines --------------------------------------- #
    def _apply_video(self, force=False):
        camera, fmt = self._selected_camera(), self._selected_format()
        if camera is None or fmt is None:
            self.video.stop()
            self._on_video_state()  # a página "nenhuma câmera" depende da seleção
            return
        if force or self.video.key != (camera.key, fmt.key) or self.video.state == "stopped":
            self.video.start(camera, fmt)

    def _apply_audio(self, force=False):
        source = self._selected_source()
        if source is None:
            self.audio.stop()
            self._last_audio_error = ""
            return
        latency = AUDIO_LATENCIES[self.latency_row.get_selected()][1]
        key = (source, self._selected_sink(), latency)
        if force or self.audio.key != key or self.audio.state == "stopped":
            self.audio.start(*key)

    def _on_video_state(self, *_):
        state = self.video.state
        camera = self._selected_camera()
        if camera is None:
            self.stack.set_visible_child_name("empty")
        elif state == "error":
            self.error_page.set_description(GLib.markup_escape_text(self.video.error))
            self.stack.set_visible_child_name("error")
        else:
            self.stack.set_visible_child_name("video")
        self.spinner.set_visible(state == "starting")

        if state == "playing" and camera:
            details = [camera.name]
            if self.video.size:
                details.append(f"{self.video.size[0]}×{self.video.size[1]}")
            details.append(f"{self.video.fps:.0f} fps")
            subtitle = " · ".join(details)
        elif state == "starting":
            subtitle = "Abrindo a câmera…"
        elif state == "waiting":
            subtitle = "Aguardando imagem…"
        elif state == "error":
            subtitle = "Erro na câmera"
        else:
            subtitle = ""
        self.status_label.set_label(subtitle)
        self.status_label.set_visible(bool(subtitle))
        self._update_inhibit(state in ("playing", "waiting", "starting"))

    def _on_audio_state(self, *_):
        if self.audio.state == "error":
            if self.audio.error != self._last_audio_error:
                self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(self.audio.error), timeout=6))
            self._last_audio_error = self.audio.error
        elif self.audio.state == "playing":
            self._last_audio_error = ""

    def _on_audio_level(self, _player, peak_db):
        fraction = (peak_db - LEVEL_FLOOR_DB) / -LEVEL_FLOOR_DB
        self.level_bar.set_value(max(0.0, min(1.0, fraction)))

    def _update_inhibit(self, active):
        app = self.get_application()
        if active and not self._inhibit_cookie and app is not None:
            # Jogando com o controle o PC não recebe teclado/mouse: impede a tela de apagar.
            self._inhibit_cookie = app.inhibit(
                self, Gtk.ApplicationInhibitFlags.IDLE | Gtk.ApplicationInhibitFlags.SUSPEND, "Exibindo a imagem da câmera"
            )
        elif not active and self._inhibit_cookie and app is not None:
            app.uninhibit(self._inhibit_cookie)
            self._inhibit_cookie = 0

    # ---- tela cheia e cursor --------------------------------------------- #
    def _toggle_fullscreen(self):
        if self.is_fullscreen():
            self.unfullscreen()
        else:
            self.fullscreen()

    def _on_fullscreen_changed(self, *_):
        fullscreen = self.is_fullscreen()
        self.toolbar.set_reveal_top_bars(not fullscreen)
        if fullscreen:
            self._sidebar_before_fullscreen = self.split.get_show_sidebar()
            self.split.set_show_sidebar(False)
            self._schedule_cursor_hide()
        else:
            self.split.set_show_sidebar(self._sidebar_before_fullscreen)
            self._show_cursor()

    def _on_split_collapsed(self, *_):
        # Ao entrar em tela cheia a janela alarga, o painel deixa de estar
        # recolhido e o libadwaita o mostra de novo sozinho: esconde outra vez.
        if self.is_fullscreen():
            self.split.set_show_sidebar(False)

    def _on_key_pressed(self, _controller, keyval, _keycode, state):
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        if keyval == Gdk.KEY_F11:
            self._toggle_fullscreen()
        elif keyval == Gdk.KEY_Escape and self.is_fullscreen():
            self.unfullscreen()
        elif keyval == Gdk.KEY_F9:
            self.split.set_show_sidebar(not self.split.get_show_sidebar())
        elif ctrl and keyval in (Gdk.KEY_m, Gdk.KEY_M):
            self.mute_button.set_active(not self.mute_button.get_active())
        else:
            return False
        return True

    def _on_video_clicked(self, gesture, n_press, _x, _y):
        if n_press == 2 and gesture.get_current_button() == Gdk.BUTTON_PRIMARY:
            self._toggle_fullscreen()

    def _on_video_motion(self, *_):
        self._show_cursor()
        if self.is_fullscreen():
            self._schedule_cursor_hide()

    def _schedule_cursor_hide(self):
        if self._cursor_timeout:
            GLib.source_remove(self._cursor_timeout)
        self._cursor_timeout = GLib.timeout_add(2000, self._hide_cursor)

    def _hide_cursor(self):
        self._cursor_timeout = 0
        if self.is_fullscreen():
            self.video_area.set_cursor_from_name("none")
        return GLib.SOURCE_REMOVE

    def _show_cursor(self):
        if self._cursor_timeout and not self.is_fullscreen():
            GLib.source_remove(self._cursor_timeout)
            self._cursor_timeout = 0
        self.video_area.set_cursor(None)

    # ---- encerramento ---------------------------------------------------- #
    def _on_close_request(self, *_):
        self.shutdown()
        return False

    def shutdown(self):
        if self._cursor_timeout:
            GLib.source_remove(self._cursor_timeout)
            self._cursor_timeout = 0
        self.video.stop()
        self.audio.stop()
        self._update_inhibit(False)
        self.settings.save()


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.window = None
        self.settings = None
        self.devices = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        GLib.set_application_name(APP_NAME)
        if os.path.isdir(ICONS_DIR):
            Gtk.IconTheme.get_for_display(Gdk.Display.get_default()).add_search_path(ICONS_DIR)
        Gtk.Window.set_default_icon_name(APP_ID)
        for name, callback in (("quit", lambda *_: self.quit()), ("about", self._on_about)):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)
        self.set_accels_for_action("app.quit", ["<Control>q"])
        self.set_accels_for_action("win.fullscreen", ["F11"])
        self.set_accels_for_action("win.sidebar", ["F9"])
        self.set_accels_for_action("win.mute", ["<Control>m"])
        self.settings = Settings()
        self.devices = DeviceManager()

    def do_activate(self):
        if self.window is None:
            self.window = MainWindow(self, self.settings, self.devices)
        self.window.present()

    def do_shutdown(self):
        if self.window is not None:
            self.window.shutdown()
        if self.devices is not None:
            self.devices.shutdown()
        Adw.Application.do_shutdown(self)

    def _on_about(self, *_):
        about = Adw.AboutDialog(
            application_name=APP_NAME,
            application_icon=APP_ID,
            version=APP_VERSION,
            comments=f"{APP_TAGLINE}\n\nMostra a imagem da webcam ou placa de captura e devolve o áudio dela para a caixa de som.",
            license_type=Gtk.License.MIT_X11,
        )
        about.present(self.window)


def list_devices():
    """Modo texto (--listar): útil para conferir o que o programa enxerga."""
    devices = DeviceManager()
    print("Câmeras / placas de captura:")
    for cam in devices.cameras:
        formats = parse_formats(cam.caps)
        best = best_format(formats, cam)
        match = find_matching_source(cam, devices.sources)
        print(f"  • {cam.name}  [{cam.path}]")
        print(f"      formato escolhido: {best.label if best else '—'}  ({len(formats)} disponíveis)")
        print(f"      áudio do mesmo aparelho: {match.name + '  [' + match.key + ']' if match else '—'}")
    if not devices.cameras:
        print("  (nenhuma)")
    print("Entradas de áudio:")
    for s in devices.sources:
        print(f"  • {s.name}  [{s.key}]{'  (padrão)' if s.is_default else ''}")
    print("Saídas de áudio:")
    for s in devices.sinks:
        print(f"  • {s.name}  [{s.key}]{'  (padrão)' if s.is_default else ''}")
    print("Vídeo pela GPU (gtk4paintablesink):", "sim" if Gst.ElementFactory.find("gtk4paintablesink") else "não")
    devices.shutdown()


def main(argv):
    Gst.init(None)
    if "--listar" in argv[1:]:
        list_devices()
        return 0
    app = App()
    # Ctrl+C, kill e fechar o terminal: sai do jeito certo (salva e fecha os streams).
    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        unix_signal_add(GLib.PRIORITY_DEFAULT, signum, lambda: app.quit() or GLib.SOURCE_REMOVE)
    return app.run([a for a in argv if a != "--listar"])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
