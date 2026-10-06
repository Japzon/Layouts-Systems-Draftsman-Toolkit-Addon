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

from . import ui_common

class LSD_PT_Preferences:
    """
    AI Editor Note:
    This class is a drawing helper for the 'Preferences' panel. It is not a
    registered bpy.types.Panel, but is called by the main LSD_PT_FabricationConstructionDraftsmanToolsAutomated
    to draw its content. This structure allows for dynamic reordering of panels.
    """
    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        # Preferences panel is always available if the main panel is.
        return True
    @staticmethod
    def draw(layout: bpy.types.UILayout, context: bpy.types.Context) -> None:
        """
        Main drawing logic for the Preferences panel.
        """
        scene = context.scene
        # 1. Standardized Header
        box, is_expanded = ui_common.draw_panel_header(
            layout, context,
            "Preferences",
            "lsd_show_panel_preferences",
            "lsd_panel_enabled_preferences"
        )
        if is_expanded:
            col_main = box.column(align=True)
            # --- Viewport Display Settings ---
            display_box = col_main.box()
            display_box.label(text="Viewport Display", icon='VIEW3D')
            row = display_box.row(align=True)
            row.prop(scene, "lsd_viz_gizmos", text="Show Gizmos")
            row.prop(scene, "lsd_show_bones", text="Show Bones")
            # --- Scene Units ---
            units_box = col_main.box()
            units_box.label(text="Scene Units", icon='SCENE_DATA')
            unit_settings = scene.unit_settings
            col = units_box.column(align=True)
            col.prop(unit_settings, "system", text="System")
            if unit_settings.system != 'NONE':
                col.prop(unit_settings, "scale_length", text="Unit Scale")
                col.prop(unit_settings, "length_unit", text="Measurement Unit")
                col.prop(unit_settings, "use_separate", text="Separate Units")
            # --- UI Behavior ---
            behavior_box = col_main.box()
            behavior_box.label(text="UI Behavior", icon='PREFERENCES')
            behavior_box.prop(scene, "lsd_auto_collapse_panels")
            # --- Panel Order & Visibility Data ---
            top_level_names = {
                "lsd_order_dimensions": "Dimensions & Precision Transforms",
                "lsd_order_sdf_booleans": "Booleans",
                "lsd_order_animation": "Animation Layers System",
                "lsd_order_camera": "Camera Studio & Pathing",
                "lsd_order_preferences": "Preferences",
            }
            visibility_mapping = {
                "lsd_order_dimensions": "lsd_panel_enabled_dimensions",
                "lsd_order_sdf_booleans": "lsd_panel_enabled_sdf_booleans",
                "lsd_order_animation": "lsd_panel_enabled_animation",
                "lsd_order_camera": "lsd_panel_enabled_camera",
                "lsd_order_preferences": "lsd_panel_enabled_preferences",
            }
            
            # Sort order keys based on current property values
            order_keys = list(top_level_names.keys())
            order_keys.sort(key=lambda k: getattr(scene, k, 0))
            
            # --- Panel Visibility ---
            visibility_box = col_main.box()
            visibility_box.label(text="Panel Visibility (Main)", icon='HIDE_OFF')
            grid = visibility_box.grid_flow(row_major=True, columns=2, even_columns=True, even_rows=False, align=True)
            for k in order_keys:
                vis_prop = visibility_mapping.get(k)
                if vis_prop:
                    grid.prop(scene, vis_prop, text=top_level_names[k])
            
            
            # --- Panel Order ---
            order_box = col_main.box()
            order_box.label(text="Panel Reordering", icon='SORTSIZE')
            for k in order_keys:
                row = order_box.row(align=True)
                row.label(text=top_level_names[k])
                op_up = row.operator("lsd.move_panel", text="", icon='TRIA_UP')
                op_up.direction = 'UP'
                op_up.prop_name = k
                op_down = row.operator("lsd.move_panel", text="", icon='TRIA_DOWN')
                op_down.direction = 'DOWN'
                op_down.prop_name = k
            row = order_box.row(align=True)
            row.operator("lsd.reset_panel_order", icon='LOOP_BACK', text="Reset")
def register():
    for cls in [LSD_PT_Preferences]:
        if hasattr(cls, 'bl_rna'):
            bpy.utils.register_class(cls)
def unregister():
    for cls in reversed([LSD_PT_Preferences]):
        if hasattr(cls, 'bl_rna'):
            bpy.utils.unregister_class(cls)
