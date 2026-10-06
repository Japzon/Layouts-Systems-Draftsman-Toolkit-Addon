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
from . import ui_common

class LSD_PT_SDF_Booleans:
    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return context.scene.lsd_panel_enabled_sdf_booleans

    @staticmethod
    def draw(layout: bpy.types.UILayout, context: bpy.types.Context) -> None:
        box_main, is_expanded = ui_common.draw_panel_header(
            layout, context, "Booleans", 
            "lsd_show_panel_sdf_booleans", "lsd_panel_enabled_sdf_booleans"
        )
        
        if not is_expanded:
            return

        col = box_main.column(align=True)
        obj = context.active_object
        if not obj or obj.type != 'MESH':
            col.label(text="Please select a mesh object", icon='INFO')
            return
            
        props = getattr(obj, "lsd_pg_sdf_props", None)
        if not props:
            col.label(text="No Boolean Pro properties found", icon='ERROR')
            return

        # 1. Boolean Pro Main Section
        box = col.box()
        box.label(text="Boolean Pro:", icon='MOD_BOOLEAN')
        
        # Cutter object selection with pipette / pick helper
        row_cut = box.row(align=True)
        row_cut.prop(props, "target_object", text="Cutter")
        row_cut.operator("lsd.nm_pick_cutter_from_selection", text="", icon='EYEDROPPER')

        # Direct 1-Click Quick Operations
        row_quick = box.row(align=True)
        op_diff = row_quick.operator("lsd.nm_quick_boolean", text="Difference", icon='SELECT_SUBTRACT')
        op_diff.operation = 'DIFFERENCE'
        op_union = row_quick.operator("lsd.nm_quick_boolean", text="Union", icon='SELECT_EXTEND')
        op_union.operation = 'UNION'
        op_inter = row_quick.operator("lsd.nm_quick_boolean", text="Intersect", icon='SELECT_INTERSECT')
        op_inter.operation = 'INTERSECT'
        op_slice = row_quick.operator("lsd.nm_quick_boolean", text="Slice", icon='MOD_BOOLEAN')
        op_slice.operation = 'SLICE'

        # Stack Configuration
        box.prop(props, "boolean_operation")
        box.prop(props, "boolean_solver")
        
        if props.boolean_operation == 'SLICE':
            box.prop(props, "inset_thickness")
        box.prop(props, "outset_thickness")
        box.prop(props, "bevel_weld_radius")
        box.prop(props, "texture_blur")

        # Weld Settings
        weld_box = box.box()
        weld_row = weld_box.row(align=True)
        weld_row.prop(props, "weld_enabled")
        if props.weld_enabled:
            weld_row.prop(props, "weld_distance")

        # Shading & Materials
        mat_box = box.box()
        mat_box.prop(props, "transfer_normals")
        mat_box.prop(props, "materials_mode")
        if props.materials_mode == 'SPECIFY':
            mat_box.prop(props, "materials_slot_index")
        
        has_bool = "NM_Boolean" in obj.modifiers
        box.operator(
            "lsd.nm_boolean_pro",
            text="Remove Boolean" if has_bool else "Apply Boolean",
            icon='CANCEL' if has_bool else 'ADD'
        )

        # 2. Repair Tools
        box_rep = col.box()
        box_rep.label(text="Repair Tools:", icon='TOOL_SETTINGS')
        row_rep = box_rep.row(align=True)
        row_rep.operator("lsd.nm_repair_boolean_normals", text="Repair Boolean Normals", icon='RECOVER_LAST')
        row_rep.operator("lsd.nm_repair_bevel_normals", text="Repair Bevel Normals", icon='MOD_BEVEL')

        # 3. Advanced Boolean Operations
        box_adv = col.box()
        box_adv.label(text="Advanced Operations:", icon='MODIFIER')
        row_adv = box_adv.row(align=True)
        row_adv.operator("lsd.nm_boolean_extrude", text="Extrude", icon='FACESEL')
        row_adv.operator("lsd.nm_cut_groove", text="Cut Groove", icon='MOD_BEVEL')
        row_adv.operator("lsd.nm_boolean_trim", text="Trim", icon='MOD_BOOLEAN')

        # 4. Surface Integration
        box_surf = col.box()
        box_surf.label(text="Surface Integration:", icon='MOD_SHRINKWRAP')
        row_surf = box_surf.row(align=True)
        has_proj = "NM_Surface_Project" in obj.modifiers
        row_surf.operator(
            "lsd.nm_surface_project",
            text="Remove Project" if has_proj else "Surface Project",
            icon='CANCEL' if has_proj else 'ADD'
        )
        has_ins = "NM_Surface_Insert" in obj.modifiers
        row_surf.operator(
            "lsd.nm_surface_insert",
            text="Remove Insert" if has_ins else "Surface Insert",
            icon='CANCEL' if has_ins else 'ADD'
        )

        # 5. Normal Control
        box_norm = col.box()
        box_norm.label(text="Normal Control:", icon='MOD_NORMALEDIT')
        row_norm1 = box_norm.row(align=True)
        has_wn = "NM_Weighted_Normal" in obj.modifiers
        row_norm1.operator(
            "lsd.nm_normal_weighted",
            text="Remove Weighted" if has_wn else "Weighted Normals",
            icon='CANCEL' if has_wn else 'ADD'
        )
        has_sm = "NM_Smooth_Normals" in obj.modifiers
        row_norm1.operator(
            "lsd.nm_smooth_normals",
            text="Remove Smooth" if has_sm else "Smooth Normals",
            icon='CANCEL' if has_sm else 'ADD'
        )
        
        row_norm2 = box_norm.row(align=True)
        has_nt = "NM_Normal_Transfer" in obj.modifiers
        row_norm2.operator(
            "lsd.nm_normal_transfer",
            text="Remove Transfer" if has_nt else "Normal Transfer",
            icon='CANCEL' if has_nt else 'ADD'
        )

        # 6. Visualization
        box_vis = col.box()
        box_vis.label(text="Visualization:", icon='HIDE_OFF')
        row_vis = box_vis.row(align=True)
        row_vis.operator("lsd.nm_view_normals", text="View Normals", icon='NORMALS_FACE')
        row_vis.operator("lsd.nm_view_sharp", text="View Sharp", icon='EDGESEL')
        row_vis.operator("lsd.nm_mark_sharp", text="Mark Sharp", icon='STICKY_UVS_LOC')

        # 7. Workflow
        box_wf = col.box()
        box_wf.label(text="Workflow:", icon='CHECKMARK')
        box_wf.operator("lsd.nm_apply_modifiers", text="Apply All Modifiers", icon='CHECKMARK')

def register():
    pass

def unregister():
    pass
