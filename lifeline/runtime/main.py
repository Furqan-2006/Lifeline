"""Node entrypoint: ``python -m lifeline``.

One process per Mininet-WiFi station. Exit codes:
  0  clean stop (SIGINT or SIGTERM)
  2  configuration or argument error (message on stderr)
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lifeline.clock import SystemClock
from lifeline.config import ConfigError, NodeConfig, load_node_config
from lifeline.eventlog import EventLogger
from lifeline.runtime.node import Node
from lifeline.runtime.rng import Rng
from lifeline.transport.udp import TransportBindError, UdpTransport

EXIT_OK = 0
EXIT_CONFIG = 2

# A factory returns (codec, identity). Injected so tests and the smoke run can
# supply the TEMP stand-in; production uses _default_factory (see below).
CodecFactory = Callable[[str, int], tuple[Any, Any]]


class UsageError(ValueError):
    """Invalid command-line combination. Reported with exit code 2."""


@dataclass(frozen=True)
class Args:
    name: str
    config: Path | None
    seed: int
    log_dir: Path
    state_dir: Path | None
    bind: str
    port: int
    broadcast: str | None
    peers: tuple[tuple[str, int], ...]
    own_addrs: tuple[str, ...]
    receiver_loss: float


def parse_peers(text: str) -> tuple[tuple[str, int], ...]:
    """Parse 'host:port,host:port' into tuples. Raises UsageError."""
    out: list[tuple[str, int]] = []
    for item in text.split(","):
        item = item.strip()
        if not item:
            continue
        host, sep, port_text = item.rpartition(":")
        if not sep or not host:
            raise UsageError(f"peer {item!r} must look like host:port")
        try:
            port = int(port_text)
        except ValueError as exc:
            raise UsageError(f"peer {item!r} has a non-numeric port") from exc
        if not 1 <= port <= 65535:
            raise UsageError(f"peer {item!r} port must be in [1, 65535]")
        out.append((host, port))
    if not out:
        raise UsageError("--peers must list at least one host:port")
    return tuple(out)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m lifeline", description="Run one Lifeline node.")
    p.add_argument("--name", required=True, help="node name, for example n07")
    p.add_argument("--config", type=Path, default=None, help="node TOML (C4 4.1); defaults if omitted")
    p.add_argument("--seed", type=int, required=True, help="run seed, 0 to 2**64-1")
    p.add_argument("--log-dir", type=Path, required=True, help="writes LOG_DIR/NAME.jsonl")
    p.add_argument("--state-dir", type=Path, default=None, help="sequence state; keep it across restarts")
    p.add_argument("--bind", default="0.0.0.0", help="local address to bind")
    p.add_argument("--port", type=int, required=True, help="UDP port, same on every station")
    p.add_argument("--broadcast", default=None, help="subnet broadcast address, for example 10.255.255.255")
    p.add_argument("--peers", default=None, help="comma-separated host:port list (tests and loopback)")
    p.add_argument("--own-addr", action="append", default=[], dest="own_addrs",
                   help="this station's IP; repeat if the station has several")
    p.add_argument("--receiver-loss", type=float, default=0.0,
                   help="per-receiver drop probability in [0, 1]")
    return p


def validate(ns: argparse.Namespace) -> Args:
    """Turn parsed arguments into Args, or raise UsageError with a clear message."""
    if bool(ns.broadcast) == bool(ns.peers):
        raise UsageError("give exactly one of --broadcast or --peers")
    if not ns.own_addrs:
        raise UsageError("--own-addr is required so the node ignores its own broadcasts")
    if not ns.name or any(c in ns.name for c in "/\\ \t\n"):
        raise UsageError("--name must be non-empty with no slashes or whitespace")
    if not 0 <= ns.seed < 2**64:
        raise UsageError("--seed must be in [0, 2**64)")
    if not 0 <= ns.port <= 65535:
        raise UsageError("--port must be in [0, 65535]")
    if not 0.0 <= ns.receiver_loss <= 1.0:
        raise UsageError("--receiver-loss must be in [0, 1]")
    peers = parse_peers(ns.peers) if ns.peers else ()
    return Args(
        name=ns.name,
        config=ns.config,
        seed=ns.seed,
        log_dir=ns.log_dir,
        state_dir=ns.state_dir,
        bind=ns.bind,
        port=ns.port,
        broadcast=ns.broadcast,
        peers=peers,
        own_addrs=tuple(ns.own_addrs),
        receiver_loss=ns.receiver_loss,
    )


def load_config(path: Path | None) -> NodeConfig:
    if path is None:
        return NodeConfig()
    return load_node_config(path)


def _default_factory(name: str, seed: int) -> tuple[Any, Any]:
    """Contract question 1. Replace this body when lifeline/envelope.py and crypto.py merge.

    Until then the production CLI cannot build a signing node, and says so.
    """
    raise ConfigError(
        "no envelope codec or identity is wired in yet (M2's envelope.py and crypto.py "
        "are not merged); run the harness entrypoint instead"
    )


def build_node(args: Args, codec_factory: CodecFactory = _default_factory,
               config: NodeConfig | None = None) -> tuple[Node, UdpTransport, EventLogger]:
    """Construct the node. This is the single plug-in point for the modules.

    ---- PLUG IN MODULES HERE -------------------------------------------------
    After ``node`` is built and before it is returned, register handlers:
      M1: neighbor manager (HELLO, WP5) and flood engine (WP6)
          node.add_handler(flood.on_envelope)
      M2: text data plane (WP7)
          node.add_handler(text.on_envelope)
      M3: SOS and key services (WP8)
          node.add_handler(sos.on_envelope); node.add_handler(keys.on_envelope)
    Until those modules exist, the node runs silent and logs only RX, TX
    and DROP MALFORMED.
    ---------------------------------------------------------------------------
    """
    cfg = config if config is not None else load_config(args.config)
    clock = SystemClock()
    rng = Rng(args.seed).child(args.name)
    args.log_dir.mkdir(parents=True, exist_ok=True)
    logger = EventLogger(args.name, clock, path=args.log_dir / f"{args.name}.jsonl")
    codec, identity = codec_factory(args.name, args.seed)
    transport = UdpTransport(
        args.bind,
        args.port,
        clock,
        rng,
        broadcast_addr=args.broadcast,
        peers=args.peers or None,
        own_addrs=args.own_addrs,
        receiver_loss=args.receiver_loss,
    )
    node = Node(
        args.name, cfg, transport, clock, rng, logger, codec,
        identity=identity, state_dir=args.state_dir,
    )
    # ---- register handlers here (see docstring) ----
    return node, transport, logger


async def run(args: Args, codec_factory: CodecFactory = _default_factory) -> int:
    """Run until SIGINT or SIGTERM. Returns the exit code."""
    try:
        node, transport, logger = build_node(args, codec_factory)
    except ConfigError as exc:
        print(f"lifeline: configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except (TransportBindError, OSError) as exc:
        print(f"lifeline: cannot start: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass  # Windows, or not the main thread: tests stop the node directly

    try:
        await transport.start()
    except TransportBindError as exc:
        await node.stop()
        print(f"lifeline: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    await node.start()
    await stop.wait()
    await node.stop()  # closes transport, flushes and closes the log
    return EXIT_OK


def main(argv: Sequence[str] | None = None, codec_factory: CodecFactory = _default_factory) -> int:
    parser = build_parser()
    try:
        ns = parser.parse_args(argv)
        args = validate(ns)
        if args.config is not None and not args.config.exists():
            raise UsageError(f"config file not found: {args.config}")
    except UsageError as exc:
        print(f"lifeline: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except ConfigError as exc:
        print(f"lifeline: configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    return asyncio.run(run(args, codec_factory))


if __name__ == "__main__":
    sys.exit(main())