# /// script
# requires-python = ">=3.11"
# dependencies = ["bleak>=0.22"]
# ///
import argparse
import asyncio
import os
import sys
import time
import tomllib
from datetime import datetime

from bleak import BleakClient, BleakScanner

SERVICE_UUID = "0000fff0-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000fff4-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "0000fff3-0000-1000-8000-00805f9b34fb"

MODEL_BY_SERVICE_UUID = {
    f"0000{code:04x}-0000-1000-8000-00805f9b34fb": model
    for code, model in {0xB717: 1, 0xB817: 2, 0xB917: 3, 0xBA17: 4}.items()
}

TX_HEADER = 0xBC
RX_HEADER = 0xCC

CMD_SYNC_TIME = 0x01
CMD_TIMER = 0x02
CMD_VENT = 0x03
CMD_POWER = 0x04
CMD_TEST_MODE = 0x05
CMD_FACTORY_RESET = 0x06
CMD_RESET_OIL = 0x07
CMD_STATUS = 0xAA
CMD_TIMERS = 0xBB
CMD_LOGIN = 0xF1
CMD_CHANGE_PASSWORD = 0xF2
CMD_RENAME = 0xF3

WEEKDAYS = "1234567"

DEFAULT_PIN = 9999

COMMON_PINS = [
    1234, 0, 1111, 1212, 7777, 1004, 2000, 4444, 2222, 6969, 9999, 3333, 5555,
    6666, 1122, 1313, 8888, 4321, 2001, 1010, 1000, 2020, 2021, 2022, 2023,
    2024, 2025, 2580, 7, 1231, 1230, 123456, 111111, 121212, 654321, 666666,
    112233, 159753, 101010, 12345, 54321, 88888, 66666,
]

CONFIG_PATHS = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "aroma.toml"),
    os.path.expanduser("~/.config/aft-aroma/aroma.toml"),
]


def load_config() -> dict:
    for path in CONFIG_PATHS:
        if os.path.exists(path):
            with open(path, "rb") as f:
                return tomllib.load(f)
    return {}


def checksum(data: bytes) -> int:
    return sum(data) & 0xFF


def frame(cmd: int, payload: bytes) -> bytes:
    body = bytes([TX_HEADER, cmd, len(payload)]) + payload
    return body + bytes([checksum(body)])


def password_bytes(password: int) -> bytes:
    return password.to_bytes(4, "big")[1:]


def parse_status(data: bytes) -> dict:
    return {
        "oil_percent": data[3],
        "spraying": data[4] == 1,
        "power": data[5] == 1,
        "vent": data[6] == 1,
        "connected": data[7] == 1,
        "work_window": f"{data[9]:02d}:{data[10]:02d}-{data[11]:02d}:{data[12]:02d}",
        "spray_seconds": data[13] * 5,
        "pause_seconds": data[14] * 5,
    }


def parse_timer(data: bytes) -> dict:
    mask = data[10]
    return {
        "index": data[3],
        "start": f"{data[4]:02d}:{data[5]:02d}",
        "stop": f"{data[6]:02d}:{data[7]:02d}",
        "spray_seconds": data[8] * 5,
        "pause_seconds": data[9] * 5,
        "days": "".join(d for i, d in enumerate(WEEKDAYS) if mask & (1 << i)),
        "enabled": data[11] == 1,
    }


def days_mask(days: str) -> int:
    mask = 0
    for d in days:
        if d not in WEEKDAYS:
            raise argparse.ArgumentTypeError(f"dia invalido: {d} (use 1=seg ... 7=dom)")
        mask |= 1 << (int(d) - 1)
    return mask


def hhmm(value: str) -> tuple[int, int]:
    hour, minute = value.split(":")
    return int(hour), int(minute)


def diffuser_model(adv) -> int | None:
    for uuid in adv.service_uuids:
        model = MODEL_BY_SERVICE_UUID.get(uuid.lower())
        if model:
            return model
    return None


def pin_sequence(start: int, maximum: int):
    seen = set()
    for pin in COMMON_PINS:
        if pin <= maximum and pin not in seen:
            seen.add(pin)
            yield pin
    for pin in range(start, maximum + 1):
        if pin not in seen:
            yield pin


class Diffuser:
    def __init__(self, client: BleakClient, verbose: bool):
        self.client = client
        self.verbose = verbose
        self.queue: asyncio.Queue[bytes] = asyncio.Queue()

    async def start(self):
        service = self.client.services.get_service(SERVICE_UUID)
        if service is None:
            raise RuntimeError("servico FFF0 nao encontrado no aparelho")
        notify = service.get_characteristic(NOTIFY_UUID) or service.get_characteristic(WRITE_UUID)
        await self.client.start_notify(notify, self._on_notify)

    def _on_notify(self, _sender, data: bytearray):
        data = bytes(data)
        if self.verbose:
            print(f"<- {data.hex(' ')}", file=sys.stderr)
        if len(data) > 4 and data[0] == RX_HEADER and data[-1] == checksum(data[:-1]):
            self.queue.put_nowait(data)

    async def send(self, cmd: int, payload: bytes = b""):
        packet = frame(cmd, payload)
        if self.verbose:
            print(f"-> {packet.hex(' ')}", file=sys.stderr)
        await self.client.write_gatt_char(WRITE_UUID, packet, response=True)

    async def wait_for(self, cmd: int, timeout: float = 3.0, count: int = 1) -> list[bytes]:
        found: list[bytes] = []
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while len(found) < count:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                msg = await asyncio.wait_for(self.queue.get(), remaining)
            except asyncio.TimeoutError:
                break
            if msg[1] == cmd:
                found.append(msg)
        return found

    async def login(self, password: int, timeout: float = 2.0) -> bool:
        await self.send(CMD_LOGIN, password_bytes(password))
        replies = await self.wait_for(CMD_LOGIN, timeout=timeout)
        return bool(replies) and replies[0][3] == 1

    async def sync_time(self):
        now = datetime.now()
        await self.send(
            CMD_SYNC_TIME,
            bytes([now.year >> 8 & 0xFF, now.year & 0xFF, now.month, now.day,
                   now.hour, now.minute, now.second, now.isoweekday()]),
        )

    async def status(self) -> dict | None:
        await self.send(CMD_STATUS, b"\x01")
        replies = await self.wait_for(CMD_STATUS)
        return parse_status(replies[0]) if replies else None

    async def timers(self) -> list[dict]:
        await self.send(CMD_TIMERS, b"\x01")
        return [parse_timer(m) for m in await self.wait_for(CMD_TIMER, timeout=3.0, count=16)]


async def discover_diffusers(timeout: float):
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    rows = []
    for device, adv in found.values():
        model = diffuser_model(adv)
        if model:
            rows.append((device.address, adv.local_name or device.name or "?", model, adv.rssi))
    return sorted(rows, key=lambda r: -r[3])


async def scan(timeout: float):
    rows = await discover_diffusers(timeout)
    if not rows:
        print("nenhum difusor encontrado (feche o app no celular: o aparelho so aceita uma conexao)")
        return
    for address, name, model, rssi in rows:
        print(f"{address}  {name:<20} modelo={model}  rssi={rssi}")


async def resolve_address(address: str | None, timeout: float) -> str:
    if address:
        return address
    rows = await discover_diffusers(timeout)
    if not rows:
        raise SystemExit("nenhum difusor encontrado; rode `scan` e passe --address ou configure o aroma.toml")
    print(f"usando {rows[0][0]} ({rows[0][1]})", file=sys.stderr)
    return rows[0][0]


async def recover_pin(args):
    address = await resolve_address(args.address, args.timeout)
    sequence = pin_sequence(args.start, args.max)
    started = time.time()
    tried = 0
    while True:
        try:
            async with BleakClient(address, timeout=20.0) as client:
                dev = Diffuser(client, args.verbose)
                await dev.start()
                print("conectado, testando PINs...", file=sys.stderr)
                for pin in sequence:
                    if await dev.login(pin):
                        print(f"\nPIN encontrado: {pin}")
                        print(f"adicione ao aroma.toml:  password = {pin}")
                        return
                    tried += 1
                    if tried % 200 == 0:
                        rate = tried / (time.time() - started)
                        print(f"testados {tried} (atual {pin}, {rate:.1f}/s)", file=sys.stderr)
                print("PIN nao encontrado no intervalo testado (tente --max maior)")
                return
        except asyncio.CancelledError:
            raise
        except Exception as error:
            print(f"conexao caiu ({error}); reconectando...", file=sys.stderr)
            await asyncio.sleep(2)


async def run_command(args):
    address = await resolve_address(args.address, args.timeout)
    async with BleakClient(address, timeout=15.0) as client:
        dev = Diffuser(client, args.verbose)
        await dev.start()

        if args.command != "raw":
            if not await dev.login(args.password):
                raise SystemExit("senha recusada pelo aparelho (ajuste o aroma.toml ou use -p; se esqueceu, rode `recover-pin`)")
            await dev.sync_time()
            await asyncio.sleep(0.3)

        if args.command == "status":
            print_dict(await dev.status())
        elif args.command == "power":
            await dev.send(CMD_POWER, bytes([args.state == "on"]))
        elif args.command == "vent":
            await dev.send(CMD_VENT, bytes([args.state == "on"]))
        elif args.command == "timers":
            for timer in await dev.timers():
                print_dict(timer)
                print()
        elif args.command == "timer":
            start_hour, start_min = hhmm(args.start)
            stop_hour, stop_min = hhmm(args.stop)
            await dev.send(CMD_TIMER, bytes([
                args.index, start_hour, start_min, stop_hour, stop_min,
                args.spray // 5, args.pause // 5,
                days_mask(args.days), int(not args.disabled),
            ]))
        elif args.command == "rename":
            await dev.send(CMD_RENAME, args.name.encode("utf-8"))
        elif args.command == "set-password":
            await dev.send(CMD_CHANGE_PASSWORD, password_bytes(args.new_password))
        elif args.command == "reset-oil":
            await dev.send(CMD_RESET_OIL, b"\x01")
        elif args.command in ("spray", "test-mode"):
            await dev.send(CMD_TEST_MODE, b"\x01")
        elif args.command == "factory-reset":
            await dev.send(CMD_FACTORY_RESET, b"\x01")
        elif args.command == "raw":
            await dev.send(int(args.cmd, 16), bytes.fromhex(args.payload))

        await asyncio.sleep(args.listen)


async def run(args):
    if args.command == "scan":
        await scan(args.timeout)
    elif args.command == "recover-pin":
        await recover_pin(args)
    else:
        await run_command(args)


def print_dict(values: dict | None):
    if values is None:
        print("sem resposta do aparelho")
        return
    for key, value in values.items():
        print(f"{key:<14} {value}")


def build_parser(config: dict) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aft-aroma",
        description="Controle BLE de difusores de aroma AFT (AF-11S e compativeis)",
    )
    parser.add_argument("-a", "--address", default=config.get("address"),
                        help="endereco/UUID do aparelho (padrao: aroma.toml)")
    parser.add_argument("-p", "--password", type=int, default=config.get("password", DEFAULT_PIN),
                        help=f"PIN numerico do aparelho (padrao: aroma.toml ou {DEFAULT_PIN})")
    parser.add_argument("-t", "--timeout", type=float, default=8.0,
                        help="tempo de scan em segundos")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="mostra os bytes enviados e recebidos")
    parser.add_argument("--listen", type=float, default=1.0,
                        help="segundos ouvindo respostas apos o comando")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("scan", help="procura difusores por perto")
    sub.add_parser("status", help="le nivel de oleo, power, ventilador e agenda")
    sub.add_parser("timers", help="lista os timers gravados")
    sub.add_parser("spray", help="borrifa agora por ~5s (test mode) e para sozinho")

    for name in ("power", "vent"):
        rotulo = "liga/desliga o difusor" if name == "power" else "liga/desliga o ventilador"
        sub.add_parser(name, help=rotulo).add_argument("state", choices=["on", "off"])

    timer = sub.add_parser("timer", help="grava um ciclo agendado")
    timer.add_argument("index", type=int, help="posicao do timer (1 em diante)")
    timer.add_argument("start", help="inicio HH:MM")
    timer.add_argument("stop", help="fim HH:MM")
    timer.add_argument("--spray", type=int, default=10, help="segundos borrifando (multiplo de 5)")
    timer.add_argument("--pause", type=int, default=120, help="segundos parado (multiplo de 5)")
    timer.add_argument("--days", default="1234567", help="1=seg ... 7=dom, ex: 12345")
    timer.add_argument("--disabled", action="store_true", help="grava o timer desativado")

    sub.add_parser("rename", help="renomeia o aparelho").add_argument("name")
    sub.add_parser("set-password", help="troca o PIN do aparelho").add_argument("new_password", type=int)
    sub.add_parser("reset-oil", help="zera o contador de nivel de oleo")
    sub.add_parser("test-mode", help="dispara o modo de teste (mesmo que spray)")
    sub.add_parser("factory-reset", help="restaura configuracoes de fabrica")

    recover = sub.add_parser("recover-pin", help="recupera o PIN esquecido do SEU aparelho")
    recover.add_argument("--start", type=int, default=0, help="inicio da varredura numerica")
    recover.add_argument("--max", type=int, default=9999, help="fim da varredura numerica")

    raw = sub.add_parser("raw", help="envia um comando cru (sem login)")
    raw.add_argument("cmd", help="byte do comando em hex, ex: aa")
    raw.add_argument("payload", nargs="?", default="01", help="payload em hex, ex: 01")

    return parser


def main():
    parser = build_parser(load_config())
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
