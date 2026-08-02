# -*- coding: utf-8 -*-
"""params_local.py 구조 예시 — 복사해서 실제 값 채우기: cp params_local.example.py params_local.py
값은 더미이며 실제 전략 파라미터는 비공개."""

_H = 3600
_D = 86400
_H4 = 4 * _H
MC = 200

REV_BYBIT = {"BTCUSDT": {"long": {"win": 1440, "k1": 4.0, "b": -2.0, "cooldown_sec": 3 * _H, "max_concurrent": MC}}}
REV_MT5 = {"BTCUSDT": {"long": {"win": 1440, "k1": 4.0, "b": -2.0, "cooldown_sec": 3 * _H, "max_concurrent": MC}}}
S11_TREND = {"BTCUSDT": {"long": {"win": 1440, "k1": 4.0, "b": -2.0, "cooldown_sec": 3 * _H, "max_concurrent": MC}}}
S11_REV = {"BTCUSDT": {"long": {"win": 1440, "k1": 4.0, "b": -2.0, "cooldown_sec": 3 * _H, "max_concurrent": MC}}}
S11_FADE = {"BTCUSDT": {"long": {"win": 1440, "k1": 4.0, "b": -2.0, "cooldown_sec": 3 * _H, "max_concurrent": MC}}}
S11M_TREND = {"BTCUSDT": {"long": {"win": 1440, "k1": 4.0, "b": -2.0, "cooldown_sec": 3 * _H, "max_concurrent": MC}}}
S11M_REV = {"BTCUSDT": {"long": {"win": 1440, "k1": 4.0, "b": -2.0, "cooldown_sec": 3 * _H, "max_concurrent": MC}}}
S11M_FADE = {"BTCUSDT": {"long": {"win": 1440, "k1": 4.0, "b": -2.0, "cooldown_sec": 3 * _H, "max_concurrent": MC}}}
FXD_TREND = {"BTCUSDT": {"long": {"win": 1440, "k1": 4.0, "b": -2.0, "cooldown_sec": 3 * _H, "max_concurrent": MC}}}
FXD_REV = {"BTCUSDT": {"long": {"win": 1440, "k1": 4.0, "b": -2.0, "cooldown_sec": 3 * _H, "max_concurrent": MC}}}
CRYPTOD_TREND = {"BTCUSDT": {"long": {"win": 1440, "k1": 4.0, "b": -2.0, "cooldown_sec": 3 * _H, "max_concurrent": MC}}}
CRYPTOD_REV = {"BTCUSDT": {"long": {"win": 1440, "k1": 4.0, "b": -2.0, "cooldown_sec": 3 * _H, "max_concurrent": MC}}}
MT5D_TREND = {"BTCUSDT": {"long": {"win": 1440, "k1": 4.0, "b": -2.0, "cooldown_sec": 3 * _H, "max_concurrent": MC}}}
MT5D_REV = {"BTCUSDT": {"long": {"win": 1440, "k1": 4.0, "b": -2.0, "cooldown_sec": 3 * _H, "max_concurrent": MC}}}
S22_TREND = {"BTCUSDT": {"long": {"win": 1440, "k1": 4.0, "b": -2.0, "cooldown_sec": 3 * _H, "max_concurrent": MC}}}
S22_REV = {"BTCUSDT": {"long": {"win": 1440, "k1": 4.0, "b": -2.0, "cooldown_sec": 3 * _H, "max_concurrent": MC}}}
S22_EWZ = {"BTCUSDT": {"long": {"win": 1440, "k1": 4.0, "b": -2.0, "cooldown_sec": 3 * _H, "max_concurrent": MC}}}
S22_FADE = {"BTCUSDT": {"long": {"win": 1440, "k1": 4.0, "b": -2.0, "cooldown_sec": 3 * _H, "max_concurrent": MC}}}
S22M_TREND = {"BTCUSDT": {"long": {"win": 1440, "k1": 4.0, "b": -2.0, "cooldown_sec": 3 * _H, "max_concurrent": MC}}}
S22M_REV = {"BTCUSDT": {"long": {"win": 1440, "k1": 4.0, "b": -2.0, "cooldown_sec": 3 * _H, "max_concurrent": MC}}}
S22M_EWZ = {"BTCUSDT": {"long": {"win": 1440, "k1": 4.0, "b": -2.0, "cooldown_sec": 3 * _H, "max_concurrent": MC}}}
S22M_FADE = {"BTCUSDT": {"long": {"win": 1440, "k1": 4.0, "b": -2.0, "cooldown_sec": 3 * _H, "max_concurrent": MC}}}
S22M_SWEEP = {"BTCUSDT": {"long": {"win": 1440, "k1": 4.0, "b": -2.0, "cooldown_sec": 3 * _H, "max_concurrent": MC}}}
SIZING_BYBIT_BY_STRATEGY = {"_default": 0.02}
SIZING_MT5_BY_STRATEGY = {"_default": 0.02}
