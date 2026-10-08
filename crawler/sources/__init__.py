from .indeed import Indeed
from .linkedin import LinkedIn
from .reed import Reed

REGISTRY = {"reed": Reed, "indeed": Indeed, "linkedin": LinkedIn}


def get_source(name: str):
    return REGISTRY[name]()
