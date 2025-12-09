from __future__ import annotations

import collections
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Deque, Dict, Iterable, List, Tuple

import snap7
from flask import Flask, jsonify, render_template_string, request


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


@dataclass(frozen=True)
class Tag:
    name: str
    db_number: int
    start: int
    size: int
    data_type: str = "real"  # Supported: real, int, dint, bool
    parse: Callable[[bytes], Any] | None = None


class DataBuffer:
    """Thread-safe buffer that stores the last N samples per signal."""

    def __init__(self, maxlen: int = 500):
        self._data: Dict[str, Deque[Tuple[float, Any]]] = collections.defaultdict(
            lambda: collections.deque(maxlen=maxlen)
        )
        self._lock = threading.Lock()

    def append(self, key: str, timestamp: float, value: Any) -> None:
        with self._lock:
            self._data[key].append((timestamp, value))

    def snapshot(self, keys: Iterable[str]) -> Dict[str, List[Tuple[float, Any]]]:
        with self._lock:
            return {key: list(self._data.get(key, [])) for key in keys}


class PLCManager:
    def __init__(self, name: str, ip: str, rack: int, slot: int, tags: List[Tag]):
        self.name = name
        self.ip = ip
        self.rack = rack
        self.slot = slot
        self.tags = tags
        self.client = snap7.client.Client()
        self._lock = threading.Lock()
        self.last_error: str | None = None
        self.connected = False

    def connect(self) -> None:
        with self._lock:
            if self.connected:
                return
            try:
                self.client.connect(self.ip, self.rack, self.slot)
                self.connected = self.client.get_connected()
                self.last_error = None if self.connected else "Unknown connection issue"
                if self.connected:
                    logging.info("Connected to %s (%s)", self.name, self.ip)
                else:
                    logging.warning("Failed to connect to %s (%s)", self.name, self.ip)
            except Exception as exc:  # snap7 raises generic exceptions
                self.connected = False
                self.last_error = f"Connection error: {exc}"
                logging.warning("%s connection error: %s", self.name, exc)

    def _ensure_connection(self) -> bool:
        if not self.connected or not self.client.get_connected():
            self.connected = False
            self.connect()
        return self.connected

    def _parse_value(self, tag: Tag, raw: bytes) -> Any:
        if tag.parse:
            return tag.parse(raw)
        data_type = tag.data_type.lower()
        if data_type == "real":
            return snap7.util.get_real(raw, 0)
        if data_type == "int":
            return snap7.util.get_int(raw, 0)
        if data_type == "dint":
            return snap7.util.get_dint(raw, 0)
        if data_type == "bool":
            return snap7.util.get_bool(raw, 0, 0)
        raise ValueError(f"Unsupported data type: {tag.data_type}")

    def read_tags(self) -> Dict[str, Any]:
        values: Dict[str, Any] = {}
        if not self._ensure_connection():
            return values
        for tag in self.tags:
            try:
                raw = self.client.db_read(tag.db_number, tag.start, tag.size)
                values[tag.name] = self._parse_value(tag, raw)
            except Exception as exc:
                self.last_error = f"Read error for {tag.name}: {exc}"
                logging.warning("%s read failed (%s): %s", self.name, tag.name, exc)
                self.connected = False
                break
        return values


class PLCReader(threading.Thread):
    def __init__(self, manager: PLCManager, buffer: DataBuffer, interval: float = 1.0):
        super().__init__(daemon=True)
        self.manager = manager
        self.buffer = buffer
        self.interval = interval
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        while not self._stop_event.is_set():
            start_time = time.time()
            values = self.manager.read_tags()
            for name, value in values.items():
                key = f"{self.manager.name}.{name}"
                self.buffer.append(key, start_time, value)
            time.sleep(self.interval)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

plc_config = [
    {
        "name": "plc_1500",
        "ip": "192.168.0.10",  # TODO: replace with actual PLC IP
        "rack": 0,
        "slot": 1,
        "tags": [
            Tag("thermo_1", db_number=1, start=0, size=4, data_type="real"),
            Tag("thermo_2", db_number=1, start=4, size=4, data_type="real"),
            Tag("pid_kp", db_number=2, start=0, size=4, data_type="real"),
            Tag("pid_ki", db_number=2, start=4, size=4, data_type="real"),
            Tag("pid_kd", db_number=2, start=8, size=4, data_type="real"),
            Tag("pid_output", db_number=2, start=12, size=4, data_type="real"),
        ],
    },
    {
        "name": "plc_et200sp",
        "ip": "192.168.0.11",  # TODO: replace with actual PLC IP
        "rack": 0,
        "slot": 1,
        "tags": [
            Tag("thermo_1", db_number=3, start=0, size=4, data_type="real"),
            Tag("thermo_2", db_number=3, start=4, size=4, data_type="real"),
            Tag("pid_kp", db_number=4, start=0, size=4, data_type="real"),
            Tag("pid_ki", db_number=4, start=4, size=4, data_type="real"),
            Tag("pid_kd", db_number=4, start=8, size=4, data_type="real"),
            Tag("pid_output", db_number=4, start=12, size=4, data_type="real"),
        ],
    },
]


def build_managers() -> List[PLCManager]:
    managers: List[PLCManager] = []
    for cfg in plc_config:
        managers.append(
            PLCManager(
                name=cfg["name"],
                ip=cfg["ip"],
                rack=cfg["rack"],
                slot=cfg["slot"],
                tags=cfg["tags"],
            )
        )
    return managers


buffer = DataBuffer(maxlen=500)
managers = build_managers()
readers: List[PLCReader] = [PLCReader(manager, buffer, interval=1.0) for manager in managers]

app = Flask(__name__)


def start_readers() -> None:
    for reader in readers:
        if not reader.is_alive():
            reader.start()


def stop_readers() -> None:
    for reader in readers:
        reader.stop()
    for reader in readers:
        reader.join(timeout=1.0)


def _html_page(signals: List[str]) -> str:
    return render_template_string(
        """
        <!doctype html>
        <html lang="de">
        <head>
            <meta charset="utf-8" />
            <title>PLC Dashboard</title>
            <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
            <script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns"></script>
            <style>
                body { font-family: Arial, sans-serif; margin: 20px; }
                .signal-list { max-width: 300px; float: left; margin-right: 20px; }
                .charts { overflow: auto; }
                canvas { max-width: 800px; margin-bottom: 24px; display: block; }
                .status { margin-bottom: 12px; color: #666; }
            </style>
        </head>
        <body>
            <h1>Live-Daten Siemens S7</h1>
            <div class="status" id="status"></div>
            <div class="signal-list">
                <h3>Signale</h3>
                <div id="checkboxes"></div>
            </div>
            <div class="charts">
                <canvas id="chart"></canvas>
            </div>
            <script>
                const signals = {{ signals | tojson }};
                const checkboxes = document.getElementById('checkboxes');
                const statusLabel = document.getElementById('status');
                const ctx = document.getElementById('chart').getContext('2d');

                const datasets = signals.map((signal, idx) => ({
                    label: signal,
                    data: [],
                    borderColor: `hsl(${(idx * 70) % 360}, 70%, 50%)`,
                    tension: 0.2,
                    hidden: true,
                }));

                const chart = new Chart(ctx, {
                    type: 'line',
                    data: { datasets },
                    options: {
                        animation: false,
                        scales: {
                            x: { type: 'time', time: { unit: 'second' } },
                            y: { beginAtZero: false },
                        },
                    },
                });

                function renderCheckboxes() {
                    checkboxes.innerHTML = '';
                    datasets.forEach((ds, idx) => {
                        const wrapper = document.createElement('div');
                        const cb = document.createElement('input');
                        cb.type = 'checkbox';
                        cb.id = `cb-${idx}`;
                        cb.addEventListener('change', () => {
                            ds.hidden = !cb.checked;
                            chart.update();
                        });
                        const label = document.createElement('label');
                        label.htmlFor = cb.id;
                        label.textContent = ds.label;
                        wrapper.appendChild(cb);
                        wrapper.appendChild(label);
                        checkboxes.appendChild(wrapper);
                    });
                }

                function updateStatus(status) {
                    statusLabel.textContent = status;
                }

                async function fetchData() {
                    const selected = datasets.filter(ds => !ds.hidden).map(ds => ds.label);
                    if (selected.length === 0) {
                        updateStatus('Bitte Signale auswählen.');
                        return;
                    }
                    try {
                        const params = new URLSearchParams({ signals: selected.join(',') });
                        const response = await fetch(`/data?${params.toString()}`);
                        const payload = await response.json();
                        updateStatus(payload.status);
                        datasets.forEach(ds => {
                            const points = payload.series[ds.label] || [];
                            ds.data = points.map(([ts, val]) => ({ x: ts * 1000, y: val }));
                        });
                        chart.update();
                    } catch (err) {
                        updateStatus('Fehler beim Abruf der Daten: ' + err);
                    }
                }

                renderCheckboxes();
                setInterval(fetchData, 2000);
            </script>
        </body>
        </html>
        """,
        signals=signals,
    )


@app.route("/")
def index() -> str:
    all_signals = [f"{manager.name}.{tag.name}" for manager in managers for tag in manager.tags]
    return _html_page(all_signals)


@app.route("/data")
def data() -> Any:
    selected_param = request.args.get("signals", "")
    selected = [s for s in selected_param.split(",") if s]
    series = buffer.snapshot(selected)
    status_parts = []
    for manager in managers:
        if manager.connected:
            status_parts.append(f"{manager.name}: verbunden")
        elif manager.last_error:
            status_parts.append(f"{manager.name}: {manager.last_error}")
        else:
            status_parts.append(f"{manager.name}: nicht verbunden")
    return jsonify({"series": series, "status": " | ".join(status_parts)})


@app.route("/health")
def health() -> Any:
    return {"status": "ok", "plcs": {m.name: m.connected for m in managers}}


if __name__ == "__main__":
    try:
        start_readers()
        app.run(host="0.0.0.0", port=5000, debug=False)
    except KeyboardInterrupt:
        logging.info("Stopping readers...")
    finally:
        stop_readers()
