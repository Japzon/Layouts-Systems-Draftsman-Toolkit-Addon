# --------------------------------------------------------------------------------

# Copyright (c) 2026 Greenlex Systems Services Incorporated. All rights reserved.

#

# Licensed under the GNU General Public License (GPL).

# Original Architecture & Logic by Greenlex Systems Services Incorporated.

#

# No person or organization is authorized to misrepresent this work or claim

# original authorship for themselves. Proper attribution is mandatory.

# --------------------------------------------------------------------------------

import bpy
import bmesh

import math
import mathutils

import re
import os

import json
import xml.etree.ElementTree as ET

import gpu
from bpy.app.handlers import persistent

from operator import itemgetter
from bpy_extras.io_utils import ExportHelper, ImportHelper

from bpy_extras import view3d_utils
from gpu_extras.batch import batch_for_shader

from typing import List, Tuple, Optional, Set, Any, Dict
from .. import config

from ..config import *
from .. import core

from .. import properties
from .. import operators

from .ui_dimensions import LSD_PT_Dimensions_And_Precision_Transforms
from .ui_sdf_booleans import LSD_PT_SDF_Booleans
from .ui_animation import LSD_PT_Animation_System_Main
from .ui_camera import LSD_PT_Camera_Cinematography
from .ui_preferences import LSD_PT_Preferences

class LSD_PT_FabricationConstructionDraftsmanTools(bpy.types.Panel):
    """
    The Master Panel that acts as a container for all other panels.
    It dynamically draws the sub-panels in the order defined by the user.
    """
    bl_label = "Layouts & Systems Draftsman Toolkit"
    bl_idname = "VIEW3D_PT_lsd_main"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Layouts & Systems Toolkit'
    bl_order = 0
    def draw(self, context):
        # Define the mapping of panels to their order properties
        panel_map = [
            (LSD_PT_Dimensions_And_Precision_Transforms, "lsd_order_dimensions"),
            (LSD_PT_SDF_Booleans, "lsd_order_sdf_booleans"),
            (LSD_PT_Animation_System_Main, "lsd_order_animation"),
            (LSD_PT_Camera_Cinematography, "lsd_order_camera"),
            (LSD_PT_Preferences, "lsd_order_preferences"),
        ]
        # Sort panels based on user-defined order
        panels_with_order = []
        for i, (panel_cls, prop_name) in enumerate(panel_map):
            user_order = getattr(context.scene, prop_name, i)
            panels_with_order.append((user_order, i, panel_cls))
        panels_with_order.sort(key=lambda x: (x[0], x[1]))
        # Draw each panel
        for _, _, panel_cls in panels_with_order:
            if hasattr(panel_cls, 'poll') and not panel_cls.poll(context):
                continue
            try:
                panel_cls.draw(self.layout, context)
            except Exception as e:
                import traceback
                err_box = self.layout.box()
                err_box.label(text=f"Panel error: {type(e).__name__}", icon='ERROR')
                print(f"[LSD Addon] Panel draw error in {panel_cls.__name__}: {e}")
                traceback.print_exc()
# ------------------------------------------------------------------------

def register():
    for cls in [LSD_PT_FabricationConstructionDraftsmanTools]:
        if hasattr(cls, 'bl_rna'):
            try:
                bpy.utils.register_class(cls)
            except Exception:
                pass
def unregister():
    for cls in reversed([LSD_PT_FabricationConstructionDraftsmanTools]):
        if hasattr(cls, 'bl_rna'):
            bpy.utils.unregister_class(cls)
