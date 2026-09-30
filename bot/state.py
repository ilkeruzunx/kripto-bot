"""Botun durumunu (açık pozisyonlar, kar/zarar) diske yazar; yeniden başlatmada kaldığı yerden devam eder."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .strategy import CoinState


class StateStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> dict[str, CoinState]:
        if not self.path.exists():
            return {}
        with open(self.path, encoding="utf-8") as f:
            raw = json.load(f)
        return {coin: CoinState.from_dict(d) for coin, d in raw.get("coins", {}).items()}

    def save(self, states: dict[str, CoinState]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"coins": {coin: s.to_dict() for coin, s in states.items()}}
        # Önce geçici dosyaya yaz, sonra yer değiştir: yarım kalan yazma durumu bozmasın.
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".state-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
