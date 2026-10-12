import argparse
import os
import sys


def _cli(argv):
    from . import Game
    from .log import describe
    from .og.caller import CONVENTIONS, address, number
    parser = argparse.ArgumentParser(prog="og360", description="OG360 from the command line")
    parser.add_argument("--host", help="xbox ip (default: the saved one)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="show the running og xbox game")
    call = sub.add_parser("call", help="call a function by its hex address (up to 6 stack args, registers like ecx=VALUE)")
    call.add_argument("address", type=address)
    call.add_argument("arguments", nargs="*")
    call.add_argument("--convention", choices=list(CONVENTIONS), default="cdecl",
                      help="like ghidra shows it: thiscall takes this first, fastcall puts the first two in ecx, edx")
    call.add_argument("--when", choices=["auto", "safe", "now"], default="auto",
                      help="auto: at a safe spot if the game has one, otherwise right away")
    call.add_argument("--expect", type=number, help="fail unless eax is this")
    call.add_argument("--string", help="text for @string (ascii) or @wstring (utf-16) args")
    args = parser.parse_args(argv)
    try:
        game = Game(args.host)
        try:
            if args.command == "status":
                print("Game:     %s" % game.title)
                print("Title ID: %s" % game.title_id)
                print("Emulator: %s" % game.guest.emulator)
            else:
                value = game.call(args.address, *args.arguments, convention=args.convention,
                                  expected=args.expect, string=args.string, when=args.when)
                print("EAX = %d (0x%08X)" % (value, value))
                got = game.caller.last_result or {}
                if got.get("edx") is not None:
                    print("EDX = 0x%08X" % got["edx"])
                if got.get("float") is not None:
                    print("float result = %r" % got["float"])
                if got.get("xmm0") is not None:
                    print("XMM0 = %r" % got["xmm0"])
                if got.get("timing"):
                    print("Ran: %s" % got["timing"])
                print("Report: %s" % game.caller.last_report)
        finally:
            game.close()
    except Exception as e:
        print("Stopped: %s" % describe(e), file=sys.stderr)
        return 2
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ("status", "call", "-h", "--help"):
        return _cli(argv)
    try:
        import tkinter
    except ImportError:
        sys.exit("this python has no tkinter. get python from python.org (or python3-tk on linux, python-tk with "
                 "homebrew) then start OG360 again")
    from . import paths, updater
    from .ui.app import App
    code = App(argv[0] if argv else None).run()
    if code == updater.RESTART and not os.environ.get("OG360_LAUNCHER"):
        os.chdir(paths.ROOT)
        os.execv(sys.executable, [sys.executable, "-m", "og360"] + argv)
    return code


if __name__ == "__main__":
    sys.exit(main())
