# bots/__main__.py
import sys
from pathlib import Path
import runpy


def _ensure_package_context():
    """
    python bots/__main__.py 로 직접 실행하면 패키지 컨텍스트가 없어서 상대 import가 깨진다.
    이때는 같은 프로세스에서 runpy로 `python -m bots`를 흉내내서 실행한다.
    (execv를 쓰지 않아서 IDE 콘솔 출력이 사라지는 문제도 방지)
    """
    if __package__ not in (None, ""):
        return  # 이미 패키지 컨텍스트 OK

    this_file = Path(__file__).resolve()
    project_root = this_file.parent.parent  # .../tradingBot
    sys.path.insert(0, str(project_root))

    # 현재 프로세스에서 모듈 실행(-m bots)처럼 동작
    runpy.run_module("bots", run_name="__main__")
    raise SystemExit  # run_module 끝나면 여기로 돌아오므로 종료


_ensure_package_context()

from .trade_bot import _quick_import_test

if __name__ == "__main__":
    _quick_import_test()
