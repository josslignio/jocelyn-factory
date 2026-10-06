"""factory_v3/juge_isole.py — F3 juge isolé : le code jugé ne s'exécute JAMAIS dans le processus pytest qui décide.
Usage : python juge_isole.py <fd> <livrables ':'-séparés> -- <arguments pytest>.
Le verdict part dans un tube non hérité (fd), pas dans le JUnit (forgeable) ; le crochet d'audit (sys.addaudithook) est irrévocable."""
import json, os, sys

PIRE = ("pass", "skip", "xfail", "fail").index   # B:6 : fusion du pire au meilleur, jamais « le dernier rapport gagne »


class Plugin:
    def __init__(self):
        self.cas = {}

    def pytest_runtest_logreport(self, report):
        st = "xfail" if getattr(report, "wasxfail", False) else {"passed": "pass", "skipped": "skip"}.get(report.outcome, "fail")
        if report.when == "call" or report.outcome != "passed":
            self.cas[report.nodeid] = max(st, self.cas.get(report.nodeid, st), key=PIRE)   # B:6 : nodeid brut, sans réécriture


def main():
    fd, livs = int(sys.argv[1]), {os.path.realpath(p) for p in sys.argv[2].split(":")}

    def garde(event, args):
        i = {"open": 0, "compile": 1, "import": 1}.get(event, -1)
        c = getattr(args[0], "co_filename", None) if event == "exec" else args[i] if 0 <= i < len(args) else None
        if isinstance(c, (str, bytes, os.PathLike)) and os.path.realpath(os.fsdecode(c)) in livs:
            raise PermissionError(f"juge isolé : {event} d'un livrable interdit dans le processus du juge : {os.fsdecode(c)}")

    os.set_inheritable(fd, False)
    sys.addaudithook(garde)
    import pytest  # noqa: E402
    rc = pytest.main(sys.argv[sys.argv.index("--") + 1:], plugins=[p := Plugin()])
    os.write(fd, json.dumps({"rc": int(rc), "cas": p.cas}).encode())
    sys.exit(int(rc))


if __name__ == "__main__":
    main()
