"""Command line entry point: ``ajt serve`` / ``ajt ocr`` / ``ajt doctor``."""
from __future__ import annotations

import argparse
import asyncio
import json
import socket
import sys
from pathlib import Path

from . import config as config_mod
from .config import Config


def _urls(port: int, token: str, tailscale_url: str, path: str) -> str:
    from . import pairing

    return pairing.best_pair_url(port, token, path=path, tailscale_url=tailscale_url)


def best_camera_url(port: int, token: str, tailscale_url: str = "") -> str:
    return _urls(port, token, tailscale_url, "/camera")


def best_gui_url(port: int, token: str, tailscale_url: str = "") -> str:
    return _urls(port, token, tailscale_url, "/")


def _print_doctor(cfg: Config) -> int:
    from .pairing import lan_ip

    checks: list[tuple[str, bool, str]] = []

    try:
        import rapidocr_onnxruntime  # noqa: F401

        checks.append(("OCR (rapidocr-onnxruntime)", True, "ready"))
    except Exception as exc:  # noqa: BLE001
        checks.append(("OCR (rapidocr-onnxruntime)", False, str(exc)))

    checks.append(
        (
            "翻訳バックエンド",
            True,
            f"{cfg.resolved_provider()} (設定: {cfg.provider})",
        )
    )
    if not (cfg.has_openai or cfg.has_gemini):
        checks.append(
            (
                "LLM APIキー",
                False,
                "未設定 — オフラインのルールベース訳で動作します（精度は限定的）",
            )
        )

    ip = lan_ip()
    try:
        with socket.socket() as sock:
            sock.bind((cfg.host, cfg.port))
        port_ok = True
        port_msg = f"{cfg.host}:{cfg.port} は空いています"
    except OSError as exc:
        port_ok = False
        port_msg = f"{cfg.host}:{cfg.port} を使用できません: {exc}"
    checks.append(("ポート", port_ok, port_msg))

    checks.append(("LAN IP", ip != "127.0.0.1", ip))
    if cfg.source_url:
        checks.append(("入力ソース", True, cfg.source_url))

    from . import tailscale

    ts = tailscale.probe(cfg.port)
    if not ts.available:
        checks.append(
            ("Tailscale", False, "未使用 — iPhone のカメラは --https か IPカメラアプリで")
        )
    elif ts.https_serving:
        checks.append(("Tailscale", True, f"HTTPS 有効: {ts.https_url}"))
    else:
        checks.append(
            ("Tailscale", False, "検出済みだが HTTPS 未設定 — ajt tailscale --enable で有効化")
        )

    width = max(len(name) for name, _, _ in checks)
    failed = 0
    for name, ok, detail in checks:
        mark = "✓" if ok else "!"
        if not ok:
            failed += 1
        print(f" {mark} {name.ljust(width)}  {detail}")

    camera_url = best_camera_url(cfg.port, cfg.token, ts.https_url)
    gui_url = best_gui_url(cfg.port, cfg.token, ts.https_url)
    print()
    print(f"ペアリングURL: {camera_url}")
    print(f"GUI:          {gui_url}")
    return 0 if failed <= 1 else 1


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .server import create_app

    config_mod.load_dotenv()
    cfg = Config()
    if args.port:
        cfg.port = args.port
    if args.host:
        cfg.host = args.host

    app = create_app(cfg, tls=args.https)

    # Print the pairing details before uvicorn takes over the terminal: the
    # pairing endpoint only answers on loopback, so this is the reliable way to
    # get the phone connected.
    from . import tailscale
    from .pairing import lan_ip

    ts = tailscale.probe(cfg.port)
    scheme = "https" if args.https else "http"
    ip = lan_ip()
    camera_url = best_camera_url(cfg.port, cfg.token, ts.https_url)
    hint = ""
    if not ts.https_url and not args.https:
        hint = (
            "  ※ iPhone のカメラを使うには Tailscale HTTPS か --https が必要です\n"
            "    有効化: ajt tailscale --enable\n"
        )
    print(
        "\n  古文 → 現代口語 ライブ翻訳\n"
        f"  GUI (このPC):   {scheme}://127.0.0.1:{cfg.port}/?t={cfg.token}\n"
        f"  GUI (LAN):      {scheme}://{ip}:{cfg.port}/?t={cfg.token}\n"
        f"  iPhone カメラ:  {camera_url}\n"
        f"{hint}",
        flush=True,
    )

    if args.https:
        from .pairing import ensure_cert

        cert, key = ensure_cert()
        uvicorn.run(app, host=cfg.host, port=cfg.port, ssl_certfile=str(cert), ssl_keyfile=str(key))
    else:
        uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="info")
    return 0


def cmd_ocr(args: argparse.Namespace) -> int:
    from .ocr import OcrEngine
    from . import normalize

    path = Path(args.image)
    if not path.is_file():
        print(f"ファイルがありません: {path}", file=sys.stderr)
        return 2
    engine = OcrEngine(threads=args.threads)
    result = engine.recognize(path)
    if args.json:
        print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
        return 0
    print("--- OCR ---")
    print(result.text or "(なし)")
    print()
    print("--- 正規化 ---")
    print(normalize.normalize_source(result.text) or "(なし)")
    print()
    print(f"layout={result.layout} mean={result.mean_score:.2f} lines={len(result.lines)}")
    return 0


def cmd_translate(args: argparse.Namespace) -> int:
    from . import translate

    config_mod.load_dotenv()
    cfg = Config()
    translator = translate.build_translator(cfg)
    text = args.text
    if args.file:
        text = Path(args.file).read_text(encoding="utf-8")
    if not text:
        text = sys.stdin.read()
    result = asyncio.run(translator.translate(text))
    if args.json:
        print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(result.modern)
        if result.glosses:
            print()
            for g in result.glosses:
                print(f"  {g.term}: {g.meaning}")
        if result.notes:
            print(f"\n[{result.backend}] {result.notes}")
    return 0


def cmd_tailscale(args: argparse.Namespace) -> int:
    from . import tailscale

    config_mod.load_dotenv()
    cfg = Config()
    port = args.port or cfg.port

    if args.enable:
        ok, output = tailscale.enable_serve(port)
        print(output or ("有効化しました" if ok else "失敗しました"))
        if not ok:
            return 1
    elif args.disable:
        ok, output = tailscale.disable_serve()
        print(output or ("解除しました" if ok else "失敗しました"))
        return 0 if ok else 1

    info = tailscale.probe(port)
    if not info.available:
        print("Tailscale は利用できません（未インストール、または停止中）")
        return 1

    print(f"tailnet IP:   {info.ip}")
    print(f"DNS 名:       {info.dns_name}")
    if info.https_serving:
        print(f"HTTPS:        有効 ({info.https_url})")
        print(f"              -> {info.serving_target}")
        print()
        print("iPhone カメラURL:")
        print(f"  {info.https_url}/camera?t={cfg.token}")
    else:
        print("HTTPS:        未有効")
        print(f"  有効化: ajt tailscale --enable  (または tailscale serve --bg --https=443 http://127.0.0.1:{port})")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    config_mod.load_dotenv()
    return _print_doctor(Config())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ajt", description="古文OCR → 現代口語ライブ翻訳"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="GUIサーバーを起動")
    p_serve.add_argument("--host", help="バインドするアドレス")
    p_serve.add_argument("--port", type=int, help="ポート番号")
    p_serve.add_argument("--https", action="store_true", help="自己署名証明書でHTTPS起動")
    p_serve.add_argument("--tls", action="store_true", help="(互換用)")
    p_serve.set_defaults(func=cmd_serve)

    p_ocr = sub.add_parser("ocr", help="画像1枚をOCRして表示")
    p_ocr.add_argument("image")
    p_ocr.add_argument("--threads", type=int, default=8)
    p_ocr.add_argument("--json", action="store_true")
    p_ocr.set_defaults(func=cmd_ocr)

    p_tr = sub.add_parser("translate", help="テキストを口語訳")
    p_tr.add_argument("text", nargs="?")
    p_tr.add_argument("--file")
    p_tr.add_argument("--json", action="store_true")
    p_tr.set_defaults(func=cmd_translate)

    p_doc = sub.add_parser("doctor", help="環境の診断")
    p_doc.set_defaults(func=cmd_doctor)

    p_ts = sub.add_parser("tailscale", help="Tailscale HTTPS の状態確認・設定")
    p_ts.add_argument("--enable", action="store_true", help="tailscale serve を有効化")
    p_ts.add_argument("--disable", action="store_true", help="tailscale serve を解除")
    p_ts.add_argument("--port", type=int, help="アプリのポート")
    p_ts.set_defaults(func=cmd_tailscale)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
