"""
Eight trading school backtest modules.
Each school provides a generate_signals() function that takes
OHLCV DataFrame and returns buy/sell signal DataFrames.
"""
from .base import BaseSchool
from .chan_theory import ChanTheorySchool
from .ict import ICTSchool
from .price_action import PriceActionSchool
from .wyckoff import WyckoffSchool
from .morphology import MorphologySchool
from .gann import GannSchool
from .wave_theory import WaveTheorySchool
from .dow_theory import DowTheorySchool

SCHOOLS = {
    "chan_theory": ChanTheorySchool,
    "ict": ICTSchool,
    "price_action": PriceActionSchool,
    "wyckoff": WyckoffSchool,
    "morphology": MorphologySchool,
    "gann": GannSchool,
    "wave_theory": WaveTheorySchool,
    "dow_theory": DowTheorySchool,
}

def list_schools():
    return list(SCHOOLS.keys())

def get_school(name: str):
    return SCHOOLS.get(name)
