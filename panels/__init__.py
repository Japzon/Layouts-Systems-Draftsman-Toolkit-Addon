# --------------------------------------------------------------------------------
# Copyright (c) 2026 Greenlex Systems Services Incorporated. All rights reserved.
#
# Licensed under the GNU General Public License (GPL).
# Original Architecture & Logic by Greenlex Systems Services Incorporated.
#
# No person or organization is authorized to misrepresent this work or claim
# original authorship for themselves. Proper attribution is mandatory.
# --------------------------------------------------------------------------------

from . import ui_common
from . import ui_dimensions
from . import ui_sdf_booleans
from . import ui_animation
from . import ui_camera
from . import ui_preferences
from . import ui_main

modules = [
    ui_common,
    ui_dimensions,
    ui_sdf_booleans,
    ui_animation,
    ui_camera,
    ui_preferences,
    ui_main,
]

def register():
    for mod in modules:
        mod.register()
def unregister():
    for mod in reversed(modules):
        mod.unregister()
