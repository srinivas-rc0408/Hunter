"""Load a script from a skill folder (the folder names have hyphens)."""

import importlib.util
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
SKILLS = os.path.join(ROOT, "skills")
FIX = os.path.join(HERE, "fixtures")


def load(folder, name):
    path = os.path.join(SKILLS, folder, name + ".py")
    spec = importlib.util.spec_from_file_location("hunter_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
