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
import uuid
from bpy.app.handlers import persistent
from operator import itemgetter
from bpy_extras.io_utils import ExportHelper, ImportHelper
from bpy_extras import view3d_utils
from gpu_extras.batch import batch_for_shader
from typing import List, Tuple, Optional, Set, Any, Dict
from . import properties
from . import generators
from . import core
from .config import *
from .core import (
    ensure_default_rig,
    create_parametric_part_object,
    setup_and_update_material,
    get_unique_name,
    clean_conflicting_mechanics,
    update_single_bone_gizmo,
    apply_native_constraints,
    update_all_gizmos,
    update_ik_chain_length,
    create_parametric_chain,
    update_chain_driver_settings,
    setup_native_wrap_gn,
    get_all_children_objects,
    rig_parametric_joint,
    build_generative_robot,
    get_part_catalog_prompt,
)

from . import properties

#   PART 5: OPERATORS

# ------------------------------------------------------------------------

# --- ASSET LIBRARY SYSTEM OPERATORS ---

# --- ASSET LIBRARY SYSTEM OPERATORS ---

class LSD_OT_SelectObjectByName(bpy.types.Operator):
    """Selects a Dimension assembly in the scene by its exact name (LSD Internal Use)."""
    bl_idname = "lsd.select_object_by_name"
    bl_label = "Select Dimension"
    bl_options = {'REGISTER', 'UNDO'}
    target_name: bpy.props.StringProperty()
    def execute(self, context):
        obj = bpy.data.objects.get(self.target_name)
        if obj:
            bpy.ops.object.select_all(action='DESELECT')
            obj.select_set(True)
            context.view_layer.objects.active = obj
            return {'FINISHED'}
        return {'CANCELLED'}
class LSD_OT_AddToDimensionMaster(bpy.types.Operator):
    """Adds the selected dimension objects to the master interface list."""
    bl_idname = "lsd.add_to_dimension_master"
    bl_label = "Track Selected Dimensions"
    bl_description = "Add all currently selected dimension assemblies to the tracking list"
    bl_options = {'REGISTER', 'UNDO'}
    @classmethod
    def poll(cls, context):
        from . import core
        if not context.selected_objects:
            return False
        # Allow only if at least one EXPLICITLY selected object is a dimension component
        return any(core.get_dimension_host(o) for o in context.selected_objects)
    def execute(self, context):
        from . import core
        master = context.scene.lsd_dimensions_master
        existing_hosts = {item.obj for item in master if item.obj}
        added_count = 0
        skipped_count = 0
        # 1. Gather unique dimension hosts from EXPLICIT selection only
        targets = set()
        for obj in context.selected_objects:
            host = core.get_dimension_host(obj)
            if host:
                targets.add(host)
        if not targets:
            self.report({'WARNING'}, "No dimensions found in selection. Please select the dimension lines or labels you wish to track.")
            return {'CANCELLED'}
        # 2. Add them to the list if not already present
        for host in targets:
            if host in existing_hosts:
                skipped_count += 1
                continue
            item = master.add()
            item.obj = host
            # AI Editor Note: Recover existing driver settings to prevent resetting to 1.0 ratio
            if host.animation_data:
                for drv_info in host.animation_data.drivers:
                    if drv_info.data_path == 'lsd_pg_dim_props.length':
                        drv = drv_info.driver
                        # 1. Recover Ratio from Expression (e.g., 'target_len * 1.500')
                        expr = drv.expression
                        match = re.search(r'\*\s*([\d\.]+)', expr)
                        if match:
                            try: item.ratio = float(match.group(1))
                            except: pass
                        # 2. Recover Target from Variables
                        for var in drv.variables:
                            if var.name == 'target_len':
                                for target in var.targets:
                                    if target.id:
                                        item.driver_target = target.id
                                        break
                                if item.driver_target: break
                        break
            added_count += 1
        if added_count > 0:
            self.report({'INFO'}, f"Tracked {added_count} new dimension(s).")
        elif skipped_count > 0:
            self.report({'INFO'}, "Selected dimensions are already tracked.")
        return {'FINISHED'}
class LSD_OT_RemoveFromDimensionMaster(bpy.types.Operator):
    """Removes a dimension from the drafting lists."""
    bl_idname = "lsd.remove_from_dimension_master"
    bl_label = "Remove From List"
    bl_description = "Remove this dimension from the tracking list without deleting the object"
    bl_options = {'REGISTER', 'UNDO'}
    index: bpy.props.IntProperty()
    group_index: bpy.props.IntProperty(default=-1)
    is_grouped: bpy.props.BoolProperty(default=False)
    def execute(self, context):
        scene = context.scene
        if self.is_grouped:
            sets = scene.lsd_dimensions_grouped_sets
            if self.group_index >= 0 and self.group_index < len(sets):
                target_group = sets[self.group_index]
                if self.index >= 0 and self.index < len(target_group.items):
                    item = target_group.items[self.index]
                    # AI Editor Note: Cleanup relationships before removal
                    host = item.obj
                    if host and hasattr(host, "lsd_pg_dim_props"):
                         host.driver_remove('lsd_pg_dim_props.length')
                         host.lsd_pg_dim_props.is_manual = False
                         host.update_tag()
                    target_group.items.remove(self.index)
                    return {'FINISHED'}
        else:
            master = scene.lsd_dimensions_master
            if self.index >= 0 and self.index < len(master):
                master.remove(self.index)
                return {'FINISHED'}
        return {'CANCELLED'}
class LSD_OT_BakeDimensionsMaster(bpy.types.Operator):
    """Consolidates selected dimensions into the Master Interface in the Tool Properties tab."""
    bl_idname = "lsd.bake_dimensions_master"
    bl_label = "Group for Master Control"
    bl_description = "Group all selected dimensions into the master list and expose them in the Tool Properties tab for centralized control"
    bl_options = {'REGISTER', 'UNDO'}
    def execute(self, context):
        from . import core
        # Gather targets from workspace and selection
        master = context.scene.lsd_dimensions_master
        target_list = [item.obj for item in master if item.obj]
        for obj in context.selected_objects:
            host = core.get_dimension_host(obj)
            if host and host not in target_list:
                target_list.append(host)
        # AI Editor Note: Enforce total uniqueness to prevent the "3 instead of 2" bug
        unique_targets = []
        for t in target_list:
            if t not in unique_targets:
                unique_targets.append(t)
        if not unique_targets:
            self.report({'WARNING'}, "No dimensions in workspace or selected.")
            return {'CANCELLED'}
        from . import generators
        generators.group_dimension_master_list(context, unique_targets)
        
        # Auto-navigate to Scene tab
        for area in context.screen.areas:
            if area.type == 'PROPERTIES':
                for space in area.spaces:
                    if space.type == 'PROPERTIES':
                        space.context = 'SCENE'
                        break

        self.report({'INFO'}, f"Grouped {len(unique_targets)} dimensions to the Dimensions Group Manager (Scene Tab).")
        return {'FINISHED'}

class LSD_OT_NavigateToDimensionGroups(bpy.types.Operator):
    """Switch the Properties Editor to the Scene tab to view grouped dimensions."""
    bl_idname = "lsd.navigate_to_dimension_groups"
    bl_label = "Locate Dimension Groups"
    bl_options = {'REGISTER'}

    def execute(self, context):
        found = False
        for area in context.screen.areas:
            if area.type == 'PROPERTIES':
                for space in area.spaces:
                    if space.type == 'PROPERTIES':
                        space.context = 'SCENE'
                        found = True
                        break
        if not found:
            self.report({'WARNING'}, "No Properties editor found on screen to navigate.")
            return {'CANCELLED'}
        return {'FINISHED'}

class LSD_OT_ImportGroupedDimensionsBack(bpy.types.Operator):
    """Moves a specific group of dimensions back into the active Dimensions Master Tracker."""
    bl_idname = "lsd.import_grouped_dimensions_back"
    bl_label = "Import Back"
    bl_description = "Move this specific dimension set back into the main Dimensions Master Tracker list for editing"
    bl_options = {'REGISTER', 'UNDO'}
    group_index: bpy.props.IntProperty()
    def execute(self, context):
        scene = context.scene
        sets = scene.lsd_dimensions_grouped_sets
        master = scene.lsd_dimensions_master
        if self.group_index < 0 or self.group_index >= len(sets):
            return {'CANCELLED'}
        target_group = sets[self.group_index]
        # Restore Group Name to Tracker for Title Continuity
        scene.lsd_dim_tracker_group_name = target_group.name
        
        # AI Editor Note: Protect relationships during batch migration to avoid
        # the 'Group Propagation' overwrite bug in properties.py.
        from . import properties
        properties._lsd_is_batch_updating = True
        
        count = 0
        try:
            for item in target_group.items:
                host = item.obj
                if not host: continue
                # Avoid duplicates in workspace
                if not any(m.obj == host for m in master):
                    new_item = master.add()
                    new_item.obj = host
                    # PERSISTENT LINK SOURCE: Ensure we copy the reference and ratio back
                    new_item.driver_target = item.driver_target
                    new_item.ratio = item.ratio
                    count += 1
        finally:
            properties._lsd_is_batch_updating = False
        sets.remove(self.group_index)
        self.report({'INFO'}, f"Imported {count} dimensions back to Sidebar.")
        return {'FINISHED'}
class LSD_OT_ClearGroupedDimensions(bpy.types.Operator):
    """Deletes a specific persistent dimension set."""
    bl_idname = "lsd.clear_grouped_dimensions"
    bl_label = "Delete Group"
    bl_description = "Delete this specific persistent dimension set. Dimension objects are not deleted."
    bl_options = {'REGISTER', 'UNDO'}
    group_index: bpy.props.IntProperty()
    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)
    def execute(self, context):
        sets = context.scene.lsd_dimensions_grouped_sets
        if self.group_index >= 0 and self.group_index < len(sets):
            group = sets[self.group_index]
            # AI Editor Note: Per User Request - Clear all drivers/links
            # for all items in the group before deleting metadata.
            for item in group.items:
                host = item.obj
                if host and hasattr(host, "lsd_pg_dim_props"):
                     host.driver_remove('lsd_pg_dim_props.length')
                     host.lsd_pg_dim_props.is_manual = False
                     host.update_tag()
            sets.remove(self.group_index)
        return {'FINISHED'}

class LSD_OT_Generate_Collision_Mesh(bpy.types.Operator):
    """
    Duplicates selected mesh objects, renames them with 'COLL_', and hides them.
    These objects are used as simplified collision geometry for physics simulations.
    """
    bl_idname = "lsd.generate_collision_mesh"
    bl_label = "Generate Collision Mesh (Selected)"
    bl_options = {'REGISTER', 'UNDO'}
    @classmethod
    def poll(cls, context):
        # AI Editor Note: Only operate if there are mesh objects selected
        return context.selected_objects and any(obj.type == 'MESH' for obj in context.selected_objects)
    def execute(self, context):
        # 1. Standard mode capture/restore
        initial_mode = context.mode
        if context.mode != 'OBJECT':
             bpy.ops.object.mode_set(mode='OBJECT')
        # 2. Ensure Collision Collection exists
        col_name = COLLISION_COLLECTION_NAME
        coll = bpy.data.collections.get(col_name)
        if not coll:
            coll = bpy.data.collections.new(col_name)
            context.scene.collection.children.link(coll)
        selected = [obj for obj in context.selected_objects if obj.type == 'MESH']
        count = 0
        depsgraph = context.evaluated_depsgraph_get()
        for obj in selected:
            # 3. Identify Source vs Collision Mesh
            source = obj
            is_coll = obj.name.startswith("COLL_")
            if is_coll:
                if obj.parent:
                    source = obj.parent
                else:
                    # Orphaned COLL_ object, skip or handle as source
                    pass
            coll_name = f"COLL_{source.name}"
            new_obj = bpy.data.objects.get(coll_name)
            # --- UPDATED: REFRESH LOGIC ---
            # AI Editor Note: To ensure the collision mesh reflects the latest source mesh
            # (including evaluated modifiers), we always purge the old instance before
            # creating a fresh snapshot. This prevents 'ghost' geometry and z-fighting.
            if new_obj and new_obj.parent == source:
                old_mesh = new_obj.data
                bpy.data.objects.remove(new_obj, do_unlink=True)
                if old_mesh and old_mesh.users == 0:
                    bpy.data.meshes.remove(old_mesh)
            # 4. Create fresh duplication from Evaluated State (Snapshot)
            # AI Editor Note: In Blender 4.x, creating an object directly from an evaluated mesh
            # (via to_mesh) results in a RuntimeError. We instead use new_from_object on an
            # evaluated instance to secure a permanent data-block in the main database.
            eval_source = source.evaluated_get(depsgraph)
            new_mesh = bpy.data.meshes.new_from_object(eval_source, preserve_all_data_layers=True, depsgraph=depsgraph)
            new_obj = bpy.data.objects.new(coll_name, new_mesh)
            # Copy world-space transform from original
            new_obj.matrix_world = source.matrix_world.copy()
            # Link & Parent
            coll.objects.link(new_obj)
            new_obj.parent = source
            new_obj.matrix_parent_inverse = source.matrix_world.inverted()
            # 5. Hide and Configure based on Global State
            show_global = context.scene.lsd_show_collisions
            new_obj.hide_set(not show_global)
            new_obj.hide_render = True
            new_obj.display_type = 'WIRE'
            # AI Editor Note: Enabling 'show_in_front' satisfies the drafting requirement
            # for collision meshes to always be selectable even when 'buried' inside
            # the visual target mesh.
            new_obj.show_in_front = True
            # 6. Add/Update Simplification (Decimate)
            mod_name = "LSD_Collision_Simplify"
            dec_mod = new_obj.modifiers.new(name=mod_name, type='DECIMATE')
            # 7. Add/Update Thickness (Solidify)
            thick_mod_name = "LSD_Collision_Thickness"
            thick_mod = new_obj.modifiers.new(name=thick_mod_name, type='SOLIDIFY')
            # AI Editor Note: Configuring 'Solidify' to act as an external shell
            # as per user preference (Positive Offset, Rim-only fill).
            thick_mod.offset = 1.0
            thick_mod.use_rim = True
            thick_mod.use_rim_only = True
            # Note: We determine the ratio from the source object's properties
            ratio = 1.0
            thickness = 0.0
            if hasattr(source, "lsd_pg_mech_props"):
                coll_props = source.lsd_pg_mech_props.collision
                ratio = coll_props.decimate_ratio
                thickness = coll_props.thickness
            dec_mod.ratio = ratio
            thick_mod.thickness = thickness
            count += 1
        # 7. Finalize Selection (keep original selected for further work)
        # 8. Restore Mode
        if context.mode != initial_mode:
            bpy.ops.object.mode_set(mode=initial_mode)
        self.report({'INFO'}, f"Synchronized {count} collision mesh(es) in '{col_name}'.")
        return {'FINISHED'}
class LSD_OT_Purge_Collision(bpy.types.Operator):
    """Deletes existing collision mesh(es) for selected object(s) to clean up simulation data."""
    bl_idname = "lsd.purge_collision"
    bl_label = "Purge Collision (Selected)"
    bl_description = "Delete the 'COLL_' collision mesh associated with the selected part(s)"
    bl_options = {'REGISTER', 'UNDO'}
    @classmethod
    def poll(cls, context):
        return context.selected_objects
    def execute(self, context):
        count = 0
        # Capture reference set of potential sources first to avoid StructRNA errors
        # when objects are removed from the selection collection mid-loop.
        sources = set()
        for obj in context.selected_objects:
            try:
                source = obj
                if obj.name.startswith("COLL_") and obj.parent:
                    source = obj.parent
                if source:
                    sources.add(source)
            except ReferenceError:
                continue
        for source in sources:
            try:
                coll_name = f"COLL_{source.name}"
                coll_obj = bpy.data.objects.get(coll_name)
                # Only purge if it truly belongs to the selected part
                if coll_obj and coll_obj.parent == source:
                    mesh = coll_obj.data
                    bpy.data.objects.remove(coll_obj, do_unlink=True)
                    if mesh and mesh.users == 0:
                        bpy.data.meshes.remove(mesh)
                    count += 1
            except ReferenceError:
                continue
        self.report({'INFO'}, f"Purged {count} collision mesh(es).")
        return {'FINISHED'}

# --- MAIN OPERATORS ---

class LSD_OT_Execute_AI_Prompt(bpy.types.Operator):
    """
    Executes the AI generation process based on the user's prompt.
    AI Editor Note:
    This is the entry point for the AI generation logic. The `execute` method
    is responsible for interpreting the user's intent and triggering the
    appropriate generative function.
    For this version, it uses simple keyword matching on the prompt to decide
    whether to call the `build_example_rover` function. This demonstrates a
    basic but functional dispatch mechanism.
    In a future, more advanced implementation, this operator would be responsible for:
    1.  Securely retrieving the API key.
    2.  Packaging the prompt and sending it to a remote LLM service.
    3.  Receiving a structured plan (e.g., a JSON list of Python commands or
        operator calls) from the LLM.
    4.  Safely executing that plan.
    """
    bl_idname = "lsd.execute_ai_prompt"
    bl_label = "Start Generating"
    bl_description = "Takes the plain English prompt and generates content using AI"
    bl_options = {'REGISTER', 'UNDO'}
    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        # The operator can only run if there is a prompt.
        ai_props = context.scene.lsd_pg_ai_props
        return ai_props and ai_props.api_prompt != ""
    def execute(self, context: bpy.types.Context) -> Set[str]:
        ai_props = context.scene.lsd_pg_ai_props
        prompt = ai_props.api_prompt.lower()
        # AI Editor Note: Add scaling factor from the generation cage
        scale_factor = 1.0
        if context.scene.lsd_use_generation_cage:
            scale_factor = context.scene.lsd_generation_cage_size
        # --- AI Dispatch Logic (Local vs Cloud API) ---
        if ai_props.ai_source == 'LOCAL':
            # Use the local procedural generator
            try:
                self.report({'INFO'}, f"Executing local generation for: '{prompt}'...")
                # AI Editor Note: Utilizing the 'build_generative_robot' logic in core.py
                # which can be expanded to utilize any addon feature via keywords or direct scripting.
                build_generative_robot(context, prompt, scale_factor=scale_factor)
                self.report({'INFO'}, "Successfully generated content.")
                return {'FINISHED'}
            except Exception as e:
                self.report({'ERROR'}, f"Generation failure: {e}")
                import traceback
                traceback.print_exc()
                return {'CANCELLED'}
        elif ai_props.ai_source == 'API':
            # Placeholder for future API integration (Cloud LLM)
            # The API would receive the prompt + catalog of available operators.
            system_prompt = get_part_catalog_prompt()
            print(f"--- LSD CLOUD AI REQUEST ---\nSystem: {system_prompt}\nPrompt: {prompt}\n-----------------------")
            self.report({'INFO'}, "Cloud API request simulated. Using Local mode for active execution.")
            return {'CANCELLED'}
        else:
            return {'CANCELLED'}
class LSD_OT_SetJointType(bpy.types.Operator):
    """Sets the joint type for all selected pose bones and updates their state"""
    bl_idname = "lsd.set_joint_type"
    bl_label = "Set Joint Type"
    bl_description = "Sets the joint type for all selected pose bones and updates their state"
    bl_options = {'REGISTER', 'UNDO'}
    type: bpy.props.EnumProperty(
        items=[
            ('none', "None", "No joint, bone is a free-floating link"),
            ('base', "Base", "A root joint that can be moved and rotated freely"),
            ('fixed', "Fixed", "A rigid connection with no movement"),
            ('revolute', "Revolute", "A hinge-like joint that rotates around a single axis"),
            ('continuous', "Continuous", "A revolute joint with no limits"),
            ('prismatic', "Linear", "A sliding joint that moves along a single axis")
        ],
        name="Joint Type"
    )
    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        # In Blender 3.2+, use context.selected_pose_bones_from_active_object
        if hasattr(context, 'selected_pose_bones_from_active_object'):
            return context.mode == 'POSE' and len(context.selected_pose_bones_from_active_object) > 0
        return context.mode == 'POSE' and len(context.selected_pose_bones) > 0
    def execute(self, context: bpy.types.Context) -> Set[str]:
        """
        Executes the operator, applying the joint type and all necessary state
        updates to every selected pose bone.
        """
        bones_to_update = []
        if hasattr(context, 'selected_pose_bones_from_active_object'):
            bones_to_update = context.selected_pose_bones_from_active_object
        else:
            # Fallback for older Blender versions
            bones_to_update = context.selected_pose_bones
        # Guard to prevent the update callback from firing redundant operators
        core._prop_update_guard = True
        try:
            # Retrieve style once for efficiency
            show = context.scene.lsd_viz_gizmos
            style = context.scene.lsd_gizmo_style
            for pbone in bones_to_update:
                # 1. Set the property value directly.
                pbone.lsd_pg_kinematic_props.joint_type = self.type
                # 2. Update state.
                clean_conflicting_mechanics(pbone)
                update_single_bone_gizmo(pbone, show, style)
                apply_native_constraints(pbone)
        finally:
            core._prop_update_guard = False
            context.view_layer.update()
        self.report({'INFO'}, f"Set joint type to '{self.type}' for {len(bones_to_update)} bones.")
        return {'FINISHED'}

class LSD_OT_Apply_Smart_Skin_Thickness(bpy.types.Operator):
    """Applies the current skin thickness to all selected vertices"""
    bl_idname = "lsd.apply_smart_skin_thickness"
    bl_label = "Apply Skin Thickness"
    bl_description = "Applies the chosen thickness value to the skin radius of all selected vertices"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return context.mode == 'EDIT_MESH' and context.active_object and context.active_object.type == 'MESH'

    def execute(self, context: bpy.types.Context) -> Set[str]:
        from . import generators
        thickness = context.scene.lsd_pg_smart_skin_props.thickness
        generators.apply_smart_skin_thickness(context, thickness)
        return {'FINISHED'}

class LSD_OT_Apply_Smart_Skin_Transition(bpy.types.Operator):
    """Smooths the skin thickness transition using the defined curve mapping"""
    bl_idname = "lsd.apply_smart_skin_transition"
    bl_label = "Smooth Skin Transition"
    bl_description = "Uses the 'Smooth Skin Profile' curve to distribute thickness along the selected vertex path"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return context.mode == 'EDIT_MESH' and context.active_object and context.active_object.type == 'MESH'

    def execute(self, context: bpy.types.Context) -> Set[str]:
        from . import generators
        from . import core
        curve_node = core.ensure_smart_skin_data()
        generators.apply_smart_skin_transition(context, curve_node.mapping)
        return {'FINISHED'}

class LSD_OT_Init_Smart_Skin_Data(bpy.types.Operator):
    """Initializes the required data-blocks for Smart Skin curves"""
    bl_idname = "lsd.init_smart_skin_data"
    bl_label = "Initialize Smart Skin Data"
    bl_description = "Creates the hidden NodeTree and Curve mapping required for Smart Skin transitions"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        from . import core
        core.ensure_smart_skin_data()
        return {'FINISHED'}

class LSD_OT_Setup_Skin_Modifier(bpy.types.Operator):
    """Adds a Skin modifier and initializes drafting data for the active object"""
    bl_idname = "lsd.setup_skin_modifier"
    bl_label = "Setup Smart Skin"
    bl_description = "Adds a Skin modifier, marks a root vertex, and initializes the profile curve mapping"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.active_object and context.active_object.type == 'MESH'

    def execute(self, context):
        obj = context.active_object
        
        # 1. Add Skin Modifier
        if not any(m.type == 'SKIN' for m in obj.modifiers):
            mod = obj.modifiers.new(name="Smart Skin", type='SKIN')
        
        # 2. Initialize Data
        from . import core
        core.ensure_smart_skin_data()
        
        # 3. Ensure we are in Edit Mode (UX requirement for thickness adjustment)
        if context.mode != 'EDIT_MESH':
            bpy.ops.object.mode_set(mode='EDIT')
            
        self.report({'INFO'}, "Smart Skin Modifier ready. Select vertices to adjust thickness.")
        return {'FINISHED'}
class LSD_OT_CalculateCenterOfMass(bpy.types.Operator):
    """Calculates the center of mass for the link's visual meshes"""
    bl_idname = "lsd.calculate_center_of_mass"
    bl_label = "Calculate from Visuals"
    bl_description = "Calculate the center of mass from the link's visual meshes"
    bl_options = {'REGISTER', 'UNDO'}
    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        obj = context.active_object
        if context.mode == 'POSE':
            return context.active_pose_bone is not None
        return obj is not None and (obj.name.startswith("COLL_") or hasattr(obj, "lsd_pg_mech_props"))
    def execute(self, context: bpy.types.Context) -> Set[str]:
        # Handle Pose Mode (Classic Bone-centric)
        if context.mode == 'POSE':
            return self.execute_pose_mode(context)
        # Handle Object Mode (New Mesh-centric)
        return self.execute_object_mode(context)
    def execute_object_mode(self, context):
        selected = context.selected_objects
        if not selected:
            return {'CANCELLED'}
        count = 0
        depsgraph = context.evaluated_depsgraph_get()
        for obj in selected:
            source = obj
            target_obj = obj
            # 1. Identify Target (measure collision mesh if available)
            if obj.name.startswith("COLL_") and obj.parent:
                source = obj.parent
            else:
                coll_name = f"COLL_{obj.name}"
                coll_obj = bpy.data.objects.get(coll_name)
                if coll_obj and coll_obj.parent == obj:
                    target_obj = coll_obj
            # Identify Property Owner
            if not hasattr(source, "lsd_pg_mech_props"):
                continue
            # 2. Calculate COM & Volume from FULL RESOLUTION
            # AI Editor Note: We temporarily suppress the simplification and thickness
            # modifiers to ensure physics integration uses the actual manifold volume.
            mod_dec = target_obj.modifiers.get("LSD_Collision_Simplify")
            mod_thick = target_obj.modifiers.get("LSD_Collision_Thickness")
            old_dec = mod_dec.show_viewport if mod_dec else True
            old_thick = mod_thick.show_viewport if mod_thick else True
            if mod_dec: mod_dec.show_viewport = False
            if mod_thick: mod_thick.show_viewport = False
            try:
                # Re-evaluate for high-fidelity snapshot
                dg = context.evaluated_depsgraph_get()
                eval_obj = target_obj.evaluated_get(dg)
                temp_mesh = bpy.data.meshes.new_from_object(eval_obj)
                bm = bmesh.new()
                bm.from_mesh(temp_mesh)
                volume = abs(bm.calc_volume())
                if bm.verts:
                    # Use bounding center for simplicity on collision primitives
                    min_v = mathutils.Vector((min(v.co.x for v in bm.verts), min(v.co.y for v in bm.verts), min(v.co.z for v in bm.verts)))
                    max_v = mathutils.Vector((max(v.co.x for v in bm.verts), max(v.co.y for v in bm.verts), max(v.co.z for v in bm.verts)))
                    com_local = (min_v + max_v) / 2
                else:
                    com_local = mathutils.Vector((0, 0, 0))
                bm.free()
                bpy.data.meshes.remove(temp_mesh)
            finally:
                # Restore visual drafting state
                if mod_dec: mod_dec.show_viewport = old_dec
                if mod_thick: mod_thick.show_viewport = old_thick
            # 3. Apply to Source Properties
            inertial = source.lsd_pg_mech_props.inertial
            inertial.volume_m3 = volume
            # Convert g/cm3 to kg/m3 for mass calculation
            inertial.mass = volume * (inertial.custom_mass_gcm3 * 1000.0)
            inertial.center_of_mass = com_local
            count += 1
        self.report({'INFO'}, f"Calculated COM & Mass for {count} object(s)")
        return {'FINISHED'}
    def execute_pose_mode(self, context):
        bones_to_process = context.selected_pose_bones if context.selected_pose_bones else [context.active_pose_bone]
        count = 0
        for pbone in bones_to_process:
            if not pbone: continue
            child_meshes = get_all_children_objects(pbone, context)
            if not child_meshes: continue
            total_volume = 0
            weighted_center = mathutils.Vector((0, 0, 0))
            for obj in child_meshes:
                depsgraph = context.evaluated_depsgraph_get()
                eval_obj = obj.evaluated_get(depsgraph)
                temp_mesh = bpy.data.meshes.new_from_object(eval_obj)
                if not temp_mesh.polygons:
                    bpy.data.meshes.remove(temp_mesh)
                    continue
                bm = bmesh.new()
                bm.from_mesh(temp_mesh)
                volume = bm.calc_volume()
                if bm.verts:
                    min_v = mathutils.Vector((min(v.co.x for v in bm.verts), min(v.co.y for v in bm.verts), min(v.co.z for v in bm.verts)))
                    max_v = mathutils.Vector((max(v.co.x for v in bm.verts), max(v.co.y for v in bm.verts), max(v.co.z for v in bm.verts)))
                    center_local = (min_v + max_v) / 2
                else:
                    center_local = mathutils.Vector((0, 0, 0))
                weighted_center += (obj.matrix_world @ center_local) * volume
                total_volume += volume
                bm.free()
                bpy.data.meshes.remove(temp_mesh)
            if total_volume > 0:
                avg_center_world = weighted_center / total_volume
                link_frame_matrix = pbone.id_data.matrix_world @ pbone.matrix
                com_local = link_frame_matrix.inverted() @ avg_center_world
                pbone.lsd_pg_kinematic_props.inertial.center_of_mass = com_local
                count += 1
        self.report({'INFO'}, f"Calculated COM for {count} bone(s) via visual meshes.")
        return {'FINISHED'}
class LSD_OT_CalculateInertia(bpy.types.Operator):
    """
    Calculates and populates the inertia tensor fields for a link based on its
    mass, dimensions, and collision shape.
    """
    bl_idname = "lsd.calculate_inertia"
    bl_label = "Calculate Inertia from Shape"
    bl_description = "Calculate the inertia tensor based on the mass and collision shape"
    bl_options = {'REGISTER', 'UNDO'}
    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        obj = context.active_object
        if context.mode == 'POSE':
            return context.active_pose_bone is not None
        return obj is not None and (obj.name.startswith("COLL_") or hasattr(obj, "lsd_pg_mech_props"))
    def execute(self, context: bpy.types.Context) -> Set[str]:
        obj = context.active_object
        source = obj
        target_obj = obj
        # 1. Identify Context and Target
        if context.mode == 'POSE' and context.active_pose_bone:
            return self.execute_pose_mode(context)
        if context.mode == 'POSE':
            return self.execute_pose_mode(context)
        return self.execute_object_mode(context)
    def execute_object_mode(self, context):
        selected = context.selected_objects
        if not selected:
            return {'CANCELLED'}
        count = 0
        for obj in selected:
            source = obj
            target_obj = obj
            # Identify Target (measure collision mesh if available)
            if obj.name.startswith("COLL_") and obj.parent:
                source = obj.parent
            else:
                coll_name = f"COLL_{obj.name}"
                coll_obj = bpy.data.objects.get(coll_name)
                if coll_obj and coll_obj.parent == obj:
                    target_obj = coll_obj
            if not hasattr(source, "lsd_pg_mech_props"):
                continue
            props = source.lsd_pg_mech_props
            # --- HIGH FIDELITY DIMENSION CAPTURE ---
            # Temporarily disable modifiers to get 'full resolution' dimensions
            mod_dec = target_obj.modifiers.get("LSD_Collision_Simplify")
            mod_thick = target_obj.modifiers.get("LSD_Collision_Thickness")
            old_dec = mod_dec.show_viewport if mod_dec else True
            old_thick = mod_thick.show_viewport if mod_thick else True
            if mod_dec: mod_dec.show_viewport = False
            if mod_thick: mod_thick.show_viewport = False
            try:
                # obj.dimensions reflects the current evaluated state
                context.view_layer.update()
                dims = target_obj.dimensions.copy()
            finally:
                if mod_dec: mod_dec.show_viewport = old_dec
                if mod_thick: mod_thick.show_viewport = old_thick
            self._calculate_for_props(props, dims)
            count += 1
        self.report({'INFO'}, f"Calculated inertia for {count} object(s)")
        return {'FINISHED'}
    def execute_pose_mode(self, context):
        bones_to_process = context.selected_pose_bones if context.selected_pose_bones else [context.active_pose_bone]
        count = 0
        for pbone in bones_to_process:
            if not pbone: continue
            props = pbone.lsd_pg_kinematic_props
            child_meshes = get_all_children_objects(pbone, context)
            if not child_meshes: continue
            link_frame_matrix = pbone.id_data.matrix_world @ pbone.matrix
            inv_link_matrix = link_frame_matrix.inverted()
            min_v = mathutils.Vector((float('inf'), float('inf'), float('inf')))
            max_v = mathutils.Vector((float('-inf'), float('-inf'), float('-inf')))
            for mesh_obj in child_meshes:
                for v in mesh_obj.bound_box:
                    v_world = mesh_obj.matrix_world @ mathutils.Vector(v)
                    v_local = inv_link_matrix @ v_world
                    for i in range(3):
                        min_v[i] = min(min_v[i], v_local[i])
                        max_v[i] = max(max_v[i], v_local[i])
            dims = max_v - min_v
            self._calculate_for_props(props, dims)
            count += 1
        return {'FINISHED'}
    def _calculate_for_props(self, props, dims):
        inertial = props.inertial
        mass = inertial.mass
        # For parts, collision shape is in lsd_pg_mech_props.collision
        # For bones, context varies. We handle both attribute paths.
        shape = 'BOX'
        if hasattr(props, "collision"):
            shape = props.collision.shape
        # Generic Bounding Box Inertia (Solid Cuboid) - used for BOX and MESH fallback
        if shape in ['BOX', 'MESH']:
            inertial.ixx = (1/12) * mass * (dims.y**2 + dims.z**2)
            inertial.iyy = (1/12) * mass * (dims.x**2 + dims.z**2)
            inertial.izz = (1/12) * mass * (dims.x**2 + dims.y**2)
        elif shape == 'CYLINDER':
            radius = (dims.x + dims.y) / 4
            height = dims.z
            inertial.ixx = (1/12) * mass * (3 * radius**2 + height**2)
            inertial.iyy = (1/12) * mass * (3 * radius**2 + height**2)
            inertial.izz = (1/2) * mass * radius**2
        elif shape == 'SPHERE':
            radius = max(dims.x, dims.y, dims.z) / 2
            val = (2/5) * mass * radius**2
            inertial.ixx = inertial.iyy = inertial.izz = val
class LSD_OT_Calculate_All_Physics(bpy.types.Operator):
    """Calculates both Center of Mass and Inertia Tensor for the active target."""
    bl_idname = "lsd.calculate_all_physics"
    bl_label = "Auto-Calculate Physics"
    bl_description = "Automatically compute COM and Inertia Tensor from collision geometry"
    bl_options = {'REGISTER', 'UNDO'}
    @classmethod
    def poll(cls, context):
        if context.mode == 'POSE':
            return context.active_pose_bone is not None
        obj = context.active_object
        return obj is not None and (obj.name.startswith("COLL_") or hasattr(obj, "lsd_pg_mech_props"))
    def execute(self, context):
        # 1. Run COM
        bpy.ops.lsd.calculate_center_of_mass()
        # 2. Run Inertia
        bpy.ops.lsd.calculate_inertia()
        self.report({'INFO'}, "Physics parameters (COM, Inertia) updated.")
        return {'FINISHED'}
class LSD_OT_BakeMesh(bpy.types.Operator):
    """
    Applies all modifiers and converts a parametric object to a plain static mesh,
    permanently removing its parametric controls and helper objects.
    """
    bl_idname = "lsd.bake_mesh"
    bl_label = "Bake to Static Mesh"
    bl_description = "Convert the parametric part to a static mesh, applying all modifiers and removing parametric controls"
    bl_options = {'REGISTER', 'UNDO'}
    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        obj = context.active_object
        return obj and obj.type in {'MESH', 'CURVE'} and hasattr(obj, "lsd_pg_mech_props") and obj.lsd_pg_mech_props.is_part
    def execute(self, context: bpy.types.Context) -> Set[str]:
        """
        Executes the baking process.
        This operator uses a robust, multi-step process to ensure a clean conversion:
        1.  A temporary duplicate of the object is created.
        2.  Blender's native `convert` operator is used on the duplicate. This is the
            most reliable way to apply all modifiers, including Geometry Nodes that
            generate instances (like chains).
        3.  A new, final baked object is created from the resulting mesh data.
        4.  All original parametric objects and their specific helpers (cutters,
            empties, link meshes) are identified by inspecting properties, not by
            relying on fragile naming conventions.
        5.  The original objects and their now-unused data-blocks are deleted.
        6.  The new baked object is selected for a seamless user experience.
        """
        obj_to_bake = context.active_object
        original_obj_name = obj_to_bake.name
        # --- 1. Ensure Object Mode for safe operations ---
        if context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        # --- 2. Create the final mesh data using the robust convert operator ---
        # This is more reliable than `new_from_object` for complex GN setups.
        # AI Editor Note: Use manual deselection to avoid operator context errors.
        for o in context.view_layer.objects:
            o.select_set(False)
        obj_to_bake.select_set(True)
        context.view_layer.objects.active = obj_to_bake
        # Duplicate the object to work on a temporary copy.
        bpy.ops.object.duplicate(linked=False)
        temp_baked_obj = context.active_object
        try:
            # Convert the duplicate to a mesh, applying all modifiers.
            bpy.ops.object.convert(target='MESH')
            # Apply transforms to "lock in" the geometry.
            # AI Editor Note: Only apply scale to ensure URDF compatibility (uniform scale).
            # We keep location and rotation on the object to preserve the pivot point,
            # preventing the object from "jumping" to the world origin.
            bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
            baked_mesh_data = temp_baked_obj.data.copy()
        except Exception as e:
            self.report({'ERROR'}, f"Failed to convert object to mesh: {e}")
            # Clean up the temporary duplicate on failure.
            if temp_baked_obj:
                bpy.data.objects.remove(temp_baked_obj, do_unlink=True)
            return {'CANCELLED'}
        finally:
            # Always clean up the temporary object.
            if temp_baked_obj and temp_baked_obj.name in bpy.data.objects:
                 bpy.data.objects.remove(temp_baked_obj, do_unlink=True)
        # --- 3. Create the new, final baked object ---
        baked_obj_name = get_unique_name(f"{original_obj_name}_Baked")
        baked_obj = bpy.data.objects.new(baked_obj_name, baked_mesh_data)
        # Copy location and rotation from original, but reset scale to 1.0
        # because we just baked the scale into the mesh.
        loc, rot, _ = obj_to_bake.matrix_world.decompose()
        baked_obj.matrix_world = mathutils.Matrix.LocRotScale(loc, rot, mathutils.Vector((1.0, 1.0, 1.0)))
        context.collection.objects.link(baked_obj)
        # --- 4. Collect all original parametric objects and helpers for deletion ---
        objects_to_delete = {obj_to_bake}
        props = obj_to_bake.lsd_pg_mech_props
        # --- AI Editor Note: Handle Basic Joint Stator Baking ---
        # If this is a basic joint, we must also bake the stator object.
        # We do this by recursively calling the bake operator on the stator,
        # or manually converting it here. Manual conversion is safer to avoid context switching issues.
        if props.category == 'BASIC_JOINT' and props.joint_stator_obj:
            stator = props.joint_stator_obj
            # Unlink the stator from the properties so it doesn't get deleted as a helper
            props.joint_stator_obj = None
            # Convert Stator to static mesh
            # We can simply clear its 'is_part' flag, as it's already a mesh object.
            # But to be consistent with 'Bake', we should apply any modifiers if they existed (AutoSmooth).
            # Since we are in a script, we can just apply the modifiers.
            # For now, just marking it as not a part is sufficient to "bake" it in the UI sense.
            stator.lsd_pg_mech_props.is_part = False
        if props.category == 'BASIC_JOINT' and props.joint_screw_obj:
            screw = props.joint_screw_obj
            props.joint_screw_obj = None
            screw.lsd_pg_mech_props.is_part = False
        if props.category == 'BASIC_JOINT' and props.joint_pin_obj:
            pin = props.joint_pin_obj
            props.joint_pin_obj = None
            pin.lsd_pg_mech_props.is_part = False
        # Find boolean cutters by inspecting modifiers.
        for mod in obj_to_bake.modifiers:
            if mod.type == 'BOOLEAN' and mod.object:
                objects_to_delete.add(mod.object)
        # Find helpers stored in pointer properties for specific part types.
        if props.category == 'SPRING':
            if props.spring_start_obj: objects_to_delete.add(props.spring_start_obj)
            if props.spring_end_obj: objects_to_delete.add(props.spring_end_obj)
        elif props.category == 'CHAIN':
            if props.instanced_link_obj: objects_to_delete.add(props.instanced_link_obj)
            if props.chain_driver: objects_to_delete.add(props.chain_driver)
            if props.chain_follower_proxy: objects_to_delete.add(props.chain_follower_proxy)
        # --- 5. Delete the original objects and their data-blocks ---
        for item in objects_to_delete:
            if item and item.name in bpy.data.objects:
                data = item.data
                is_data_user = isinstance(data, (bpy.types.Mesh, bpy.types.Curve))
                bpy.data.objects.remove(item, do_unlink=True)
                # Clean up orphan data-blocks to keep the .blend file clean.
                if is_data_user and data and data.users == 0:
                    if isinstance(data, bpy.types.Mesh):
                        bpy.data.meshes.remove(data)
                    elif isinstance(data, bpy.types.Curve):
                        bpy.data.curves.remove(data)
        # Mark the new object as no longer being a parametric part to hide the UI.
        baked_obj.lsd_pg_mech_props.is_part = False
        # --- 6. Select the new baked object for a good user experience ---
        bpy.ops.object.select_all(action='DESELECT')
        context.view_layer.objects.active = baked_obj
        baked_obj.select_set(True)
        self.report({'INFO'}, f"Baked '{original_obj_name}' to static mesh: '{baked_obj.name}'")
        return {'FINISHED'}
class LSD_OT_ReadJointSettings(bpy.types.Operator):
    """Reads the properties from the active bone into the global Joint Editor tool."""
    bl_idname = "lsd.read_joint_settings"
    bl_label = "Read From Active"
    bl_description = "Load settings from the currently active bone"
    bl_options = {'REGISTER', 'UNDO'}
    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        if context.mode == 'POSE':
            return context.active_pose_bone is not None
        if context.mode == 'OBJECT':
            obj = context.active_object
            rig = context.scene.lsd_active_rig
            return obj and rig and obj.parent == rig and obj.parent_type == 'BONE'
        return False
    def execute(self, context: bpy.types.Context) -> Set[str]:
        # AI Editor Note: Support reading from the parent bone of a selected mesh in Object Mode.
        if context.mode == 'OBJECT':
            bpy.ops.lsd.enter_pose_mode()
        tool_props = context.scene.lsd_pg_joint_editor_settings
        active_props = context.active_pose_bone.lsd_pg_kinematic_props
        tool_props.joint_type = active_props.joint_type
        tool_props.axis_alignment = active_props.axis_alignment
        tool_props.joint_radius = active_props.joint_radius
        tool_props.visual_gizmo_scale = active_props.visual_gizmo_scale
        tool_props.lower_limit = active_props.lower_limit
        tool_props.upper_limit = active_props.upper_limit
        tool_props.ik_chain_length = active_props.ik_chain_length
        self.report({'INFO'}, f"Read settings from '{context.active_pose_bone.name}'")
        return {'FINISHED'}
class LSD_OT_ApplyJointSettings(bpy.types.Operator):
    """Applies the properties from the global Joint Editor tool to all selected bones."""
    bl_idname = "lsd.apply_joint_settings"
    bl_label = "Apply to Selected"
    bl_description = "Apply the settings below to all selected bones"
    bl_options = {'REGISTER', 'UNDO'}
    # Flags to determine which properties to apply (enables retaining individual settings)
    apply_type: bpy.props.BoolProperty(name="Apply Type", default=True)
    apply_axis: bpy.props.BoolProperty(name="Apply Axis", default=True)
    apply_radius: bpy.props.BoolProperty(name="Apply Radius", default=True)
    apply_viz_scale: bpy.props.BoolProperty(name="Apply Visual Scale", default=True)
    apply_limits: bpy.props.BoolProperty(name="Apply Limits", default=True)
    apply_ik: bpy.props.BoolProperty(name="Apply IK", default=True)
    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        # AI Editor Note: Relaxed poll for timer-based stability.
        # Operators called via bpy.ops in a timer must have a permissive poll.
        if not context or not hasattr(context, 'scene'): return False
        return True
    def execute(self, context: bpy.types.Context) -> Set[str]:
        # AI Editor Note: Robust context retrieval for Timer-based calls.
        ctx = context if context and hasattr(context, 'scene') else bpy.context
        scene = ctx.scene
        tool_props = scene.lsd_pg_joint_editor_settings
        bones_to_update = []
        # 1. Determine Selection Context
        mode = getattr(ctx, 'mode', 'OBJECT')
        sel_objs = getattr(ctx, 'selected_objects', [])
        active_obj = getattr(ctx, 'active_object', None)
        if mode == 'POSE':
            # Direct bone selection in Pose Mode
            sel_bones = getattr(ctx, 'selected_pose_bones', [])
            for pb in sel_bones:
                if pb not in bones_to_update:
                    bones_to_update.append(pb)
            # Active Bone Fallback
            active_pb = getattr(ctx, 'active_pose_bone', None)
            if active_pb and active_pb not in bones_to_update:
                bones_to_update.append(active_pb)
        else: # OBJECT Mode
            # Greedy gathering from hierarchies and armatures
            search_scope = list(sel_objs)
            if active_obj and active_obj not in search_scope:
                search_scope.append(active_obj)
            for obj in search_scope:
                # A. Handle Armatures: look for selected bones
                if obj.type == 'ARMATURE':
                    has_sel_bones = False
                    for pb in obj.pose.bones:
                        if pb.bone.select:
                            if pb not in bones_to_update:
                                bones_to_update.append(pb)
                                has_sel_bones = True
                    # If an armature is selected but NO bones are selected, use the active one
                    if not has_sel_bones and obj.pose.bones.active:
                        pb = obj.pose.bones.active
                        if pb not in bones_to_update:
                            bones_to_update.append(pb)
                # B. Handle Mesh hierarchies: find parent bones (Sub-Selection)
                def find_bones_recursive(o):
                    curr = o
                    # Go UP to find if this mesh is parented to a bone
                    while curr:
                        if curr.parent and curr.parent.type == 'ARMATURE' and curr.parent_type == 'BONE' and curr.parent_bone:
                            pbone = curr.parent.pose.bones.get(curr.parent_bone)
                            if pbone and pbone not in bones_to_update:
                                bones_to_update.append(pbone)
                            break # Found the direct logical owner
                        curr = curr.parent
                    # Go DOWN to handle children of selected meshes
                    for child in o.children:
                        find_bones_recursive(child)
                find_bones_recursive(obj)
        if not bones_to_update:
            return {'CANCELLED'}
        # 2. Sequential Application with Protection Guards
        from . import core
        old_guard_prop = core._prop_update_guard
        old_guard_tool = core._joint_editor_update_guard
        core._prop_update_guard = True
        core._joint_editor_update_guard = True
        try:
            for bone in bones_to_update:
                props = bone.lsd_pg_kinematic_props
                # Transfer settings only if flags are enabled (enables multi-bone individual retention)
                if self.apply_type:
                    props.joint_type = tool_props.joint_type
                if self.apply_axis:
                    props.axis_alignment = tool_props.axis_alignment
                if self.apply_radius:
                    if tool_props.joint_radius == 0.0:
                        # Performance: Independent per-bone calculation for 'Automatic' mode
                        props.joint_radius = core.detect_bone_radius(scene, bone)
                    else:
                        props.joint_radius = tool_props.joint_radius
                if self.apply_viz_scale:
                    props.visual_gizmo_scale = tool_props.visual_gizmo_scale
                if self.apply_limits:
                    props.lower_limit = tool_props.lower_limit
                    props.upper_limit = tool_props.upper_limit
                if self.apply_ik:
                    props.ik_chain_length = tool_props.ik_chain_length
                # Trigger specialized state logic for the bone
                core.clean_conflicting_mechanics(bone)
                core.update_single_bone_gizmo(bone, scene.lsd_viz_gizmos, scene.lsd_gizmo_style)
                core.apply_native_constraints(bone)
                core.update_ik_chain_length(bone, ctx)
        except Exception as e:
            msg = f"LSD Error during joint application: {e}"
            print(msg)
            import traceback
            traceback.print_exc()
            self.report({'ERROR'}, msg)
            return {'CANCELLED'}
        finally:
            core._prop_update_guard = old_guard_prop
            core._joint_editor_update_guard = old_guard_tool
            # Final refresh to ensure depsgraph catches up with constraint changes
            core.cleanup_unused_gizmos(ctx)
            ctx.view_layer.update()
        # 3. Informative Reporting
        msg = f"Processed {len(bones_to_update)} bones."
        if not scene.lsd_viz_gizmos:
            msg += " (Note: Gizmos are HIDDEN in UI)"
        if scene.lsd_placement_mode:
            msg += " (Note: Constraints DISABLED in Joint Placement Mode)"
        self.report({'INFO'}, msg)
        return {'FINISHED'}
class LSD_OT_ApplyBoneConstraints(bpy.types.Operator):
    """Propagates the properties of the active bone to all other selected bones."""
    bl_idname = "lsd.apply_bone_constraints"
    bl_label = "Propagate Settings"
    bl_description = "Copy the LSD properties of the active bone to all selected bones"
    bl_options = {'REGISTER', 'UNDO'}
    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        # AI Editor Note: Relaxed poll for timer stability.
        # Operators called via bpy.ops in a timer must have a permissive poll.
        if not context or not hasattr(context, 'scene'):
            return False
        return True
    def execute(self, context: bpy.types.Context) -> Set[str]:
        # AI Editor Note: Robust context retrieval for Timer-based calls.
        ctx = context if context and hasattr(context, 'scene') else bpy.context
        source_bone = getattr(ctx, 'active_pose_bone', None)
        if not source_bone:
            # If no active bone, we can't propagate anything
            return {'CANCELLED'}
        source_props = source_bone.lsd_pg_kinematic_props
        # 1. Gather bones to update (selection + active)
        bones_to_update = []
        rigs = [obj for obj in context.selected_objects if obj.type == 'ARMATURE']
        if context.active_object and context.active_object.type == 'ARMATURE' and context.active_object not in rigs:
            rigs.append(context.active_object)
        for rig_obj in rigs:
            for pb in rig_obj.pose.bones:
                if pb.bone.select and pb != source_bone:
                    bones_to_update.append(pb)
        # Guard to prevent feedback loops
        core._prop_update_guard = True
        try:
            show = ctx.scene.lsd_viz_gizmos
            style = ctx.scene.lsd_gizmo_style
            for bone in bones_to_update:
                target_props = bone.lsd_pg_kinematic_props
                # Sync properties
                target_props.joint_type = source_props.joint_type
                target_props.axis_alignment = source_props.axis_alignment
                target_props.joint_radius = source_props.joint_radius
                target_props.visual_gizmo_scale = source_props.visual_gizmo_scale
                target_props.lower_limit = source_props.lower_limit
                target_props.upper_limit = source_props.upper_limit
                target_props.ik_chain_length = source_props.ik_chain_length
                # Update mechanics for each bone
                core.clean_conflicting_mechanics(bone)
                core.update_single_bone_gizmo(bone, show, style)
                core.apply_native_constraints(bone)
                core.update_ik_chain_length(bone, ctx)
            # Apply to active as well just to be sure
            core.clean_conflicting_mechanics(source_bone)
            core.update_single_bone_gizmo(source_bone, show, style)
            core.apply_native_constraints(source_bone)
            core.update_ik_chain_length(source_bone, ctx)
        finally:
            core._prop_update_guard = False
            core.cleanup_unused_gizmos(ctx)
            ctx.view_layer.update()
        return {'FINISHED'}
class LSD_OT_SetupIK(bpy.types.Operator):
    """
    Creates a standard Inverse Kinematics (IK) setup for the active bone,
    including a target bone and a pole target bone.
    """
    bl_idname = "lsd.setup_ik"
    bl_label = "Setup IK Chain"
    bl_description = "Create an IK chain from the active bone up to its root, including a target and pole bone"
    bl_options = {'REGISTER', 'UNDO'}
    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return context.mode == 'POSE' and context.active_pose_bone
    def execute(self, context: bpy.types.Context) -> Set[str]:
        rig = context.object
        ik_bone = context.active_pose_bone
        # --- Cleanup old IK constraint to make the operator re-runnable ---
        for c in list(ik_bone.constraints):
            if c.name == IK_CONSTRAINT_NAME:
                ik_bone.constraints.remove(c)
        # 1. Traverse up the hierarchy to find the chain of parent bones.
        chain = []
        current_bone = ik_bone
        while current_bone:
            chain.append(current_bone)
            if len(chain) >= 255:  # Blender's IK chain limit
                break
            current_bone = current_bone.parent
        if len(chain) < 1:
            self.report({'WARNING'}, "Could not determine bone chain.")
            return {'CANCELLED'}
        chain_length = len(chain)
        # 2. Switch to Edit Mode to create the new IK controller bones.
        bpy.ops.object.mode_set(mode='EDIT')
        edit_bones = rig.data.edit_bones
        # Get the edit bone versions of the chain, reversed (from tip to root).
        chain_edit_bones = [edit_bones.get(b.name) for b in chain]
        # 3. Create the IK Target Bone at the tip of the chain.
        ik_target_name = f"IK_Target_{ik_bone.name}"
        ik_target_eb = edit_bones.new(ik_target_name)
        ik_target_eb.head = chain_edit_bones[0].tail
        ik_target_eb.tail = ik_target_eb.head + (chain_edit_bones[0].tail - chain_edit_bones[0].head).normalized() * 0.2
        ik_target_eb.parent = None  # Un-parent it from the chain.
        # 4. Create the Pole Target Bone for controlling the "elbow" or "knee".
        pole_target_name = f"IK_Pole_{ik_bone.name}"
        pole_target_eb = None
        if chain_length > 1:
            pole_target_eb = edit_bones.new(pole_target_name)
            # Calculate a good default position for the pole target.
            root_pos = chain_edit_bones[-1].head
            tip_pos = chain_edit_bones[0].tail
            elbow_pos = chain_edit_bones[0].head
            v_root_tip = tip_pos - root_pos
            v_root_elbow = elbow_pos - root_pos
            v_proj = v_root_elbow.project(v_root_tip)
            v_pole = v_root_elbow - v_proj
            pole_distance = v_root_tip.length
            if v_pole.length < 0.001:
                # If the arm is straight, default to a pole vector along the world Z-axis.
                v_pole = mathutils.Vector((0, 0, 1.0))
            pole_target_eb.head = elbow_pos + v_pole.normalized() * pole_distance
            pole_target_eb.tail = pole_target_eb.head + mathutils.Vector((0, 0, 0.2))
            pole_target_eb.parent = None
        # 5. Switch back to Pose Mode to create the constraint.
        bpy.ops.object.mode_set(mode='POSE')
        ik_con = ik_bone.constraints.new('IK')
        ik_con.name = IK_CONSTRAINT_NAME
        ik_con.target = rig
        ik_con.subtarget = ik_target_name
        ik_con.chain_count = chain_length
        # ik_con.use_ik_limit = True # REMOVED: Attribute does not exist on KinematicConstraint
        if pole_target_eb:
            ik_con.pole_target = rig
            ik_con.pole_subtarget = pole_target_name
        # --- UX Improvement: Auto-select the target and activate the move tool ---
        bpy.ops.pose.select_all(action='DESELECT')
        target_pose_bone = rig.pose.bones.get(ik_target_name)
        if target_pose_bone:
            target_pose_bone.bone.select = True
            rig.data.bones.active = target_pose_bone.bone
            bpy.ops.wm.tool_set_by_id(name="builtin.move")
        self.report({'INFO'}, f"Created IK chain of length {chain_length} on '{ik_bone.name}'")
        return {'FINISHED'}
class LSD_OT_SetOriginToCursor(bpy.types.Operator):
    """Moves the origin of the selected object(s) to the 3D cursor"""
    bl_idname = "lsd.set_origin_to_cursor"
    bl_label = "Set Origin to Cursor"
    bl_description = "Moves the origin of the selected object(s) to the 3D cursor"
    bl_options = {'REGISTER', 'UNDO'}
    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return context.active_object is not None and len(context.selected_objects) > 0
    def execute(self, context: bpy.types.Context) -> Set[str]:
        # AI Editor Note: This operator is a simple wrapper around Blender's native
        # 'origin_set' function. To ensure it works from any mode (Pose, Edit, etc.),
        # we temporarily switch to Object Mode, perform the operation, and then restore the mode.
        # This aligns with the addon's philosophy of using native features while improving UX.
        original_mode = context.mode
        # --- AI Editor Note: Map specific edit modes (e.g., 'EDIT_MESH') to the generic 'EDIT' ---
        # The bpy.ops.object.mode_set() operator expects generic mode names like 'EDIT', not 'EDIT_MESH'.
        # This mapping prevents a TypeError when trying to restore the original mode.
        mode_to_restore = original_mode
        if original_mode.startswith('EDIT_'):
            mode_to_restore = 'EDIT'
        try:
            # Switch to Object Mode if necessary, as origin_set requires it.
            if original_mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT')
            # AI Editor Note: Support sub-selection/hierarchies.
            # Blender's origin_set works on selected objects. We must select descendants if the user expects them to be affected.
            for obj in context.selected_objects:
                def select_descendants(o):
                    for child in o.children:
                        child.select_set(True)
                        select_descendants(child)
                select_descendants(obj)
            bpy.ops.object.origin_set(type='ORIGIN_CURSOR', center='MEDIAN')
            # Restore the original mode if possible.
            if original_mode != 'OBJECT':
                try:
                    # AI Editor Note: Use the mapped mode name for restoration.
                    bpy.ops.object.mode_set(mode=mode_to_restore)
                except RuntimeError:
                    # If mode restoration fails (e.g. invalid context), stay in Object Mode.
                    pass
            self.report({'INFO'}, f"Moved origin of {len(context.selected_objects)} object(s) to cursor.")
        except RuntimeError as e:
            self.report({'ERROR'}, f"Could not set origin. Error: {e}")
            # Attempt to restore mode even on error to avoid leaving user in unexpected state.
            if context.mode != original_mode:
                try:
                    # AI Editor Note: Use the mapped mode name for restoration on error.
                    bpy.ops.object.mode_set(mode=mode_to_restore)
                except:
                    pass
            return {'CANCELLED'}
        return {'FINISHED'}
# ------------------------------------------------------------------------

#   SECTION: MATERIAL & TEXTURING SYSTEM (REWRITTEN)

# ------------------------------------------------------------------------

        bpy.app.timers.register(lambda: (bpy.ops.lsd.material_merge() and None), first_interval=0.01)

class LSD_OT_Apply_Paint_Bucket(bpy.types.Operator):
    """Applies the current paint bucket settings to selected faces."""
    bl_idname = "lsd.apply_paint_bucket"
    bl_label = "Apply Paint Bucket"
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}
    
    def execute(self, context):
        from . import core
        core.apply_paint_bucket(context)
        return {'FINISHED'}

class LSD_OT_Material_AddSmart(bpy.types.Operator):
    """Add a new material slot with a pre-configured smart material preset"""
    bl_idname = "lsd.material_add_smart"
    bl_label = "Add Smart Material"
    bl_description = "Adds a new material layer with a specific preset configuration"
    bl_options = {'REGISTER', 'UNDO'}
    mat_type: bpy.props.EnumProperty(
        name="Material Type",
        items=[
            ('PLASTIC', "Plastic", "Standard dielectric material"),
            ('METAL', "Metal", "Conductive metallic material"),
            ('RUBBER', "Rubber", "High roughness, dark material"),
            ('EMISSIVE', "Emissive", "Glowing light source"),
            ('GLASS', "Glass", "Transparent material"),
            ('CARBON', "Carbon Fiber", "Procedural carbon fiber pattern"),
            ('PRINTED', "3D Printed", "Plastic with layer lines"),
            ('ALUMINUM', "Brushed Aluminum", "Anisotropic metallic"),
        ]
    )
    def execute(self, context: bpy.types.Context) -> Set[str]:
        # Use scene property if not explicitly set (e.g. from the dropdown)
        mat_type = self.mat_type
        if not self.properties.is_property_set("mat_type"):
            mat_type = context.scene.lsd_smart_material_type
        obj = context.active_object
        if not obj or obj.type != 'MESH':
            self.report({'WARNING'}, "Active object must be a mesh.")
            return {'CANCELLED'}
        # 1. Create Slot
        bpy.ops.object.material_slot_add()
        slot = obj.material_slots[obj.active_material_index]
        # 2. Create Material
        mat = bpy.data.materials.new(name=f"{mat_type.capitalize()}_{obj.name}")
        mat.use_nodes = True
        slot.material = mat
        # 3. Configure Nodes
        nodes = mat.node_tree.nodes
        links = mat.node_tree.links
        bsdf = next((n for n in nodes if n.type == 'BSDF_PRINCIPLED'), None)
        if not bsdf:
            bsdf = nodes.new('ShaderNodeBsdfPrincipled')
            output = nodes.get("Material Output") or nodes.new('ShaderNodeOutputMaterial')
            links.new(bsdf.outputs['BSDF'], output.inputs['Surface'])
        # Apply Preset Logic
        if self.mat_type == 'PLASTIC':
            bsdf.inputs['Roughness'].default_value = 0.5
            bsdf.inputs['Metallic'].default_value = 0.0
        elif self.mat_type == 'METAL':
            bsdf.inputs['Roughness'].default_value = 0.2
            bsdf.inputs['Metallic'].default_value = 1.0
            bsdf.inputs['Base Color'].default_value = (0.8, 0.8, 0.8, 1.0)
        elif self.mat_type == 'RUBBER':
            bsdf.inputs['Base Color'].default_value = (0.1, 0.1, 0.1, 1.0)
            bsdf.inputs['Roughness'].default_value = 0.9
            bsdf.inputs['Metallic'].default_value = 0.0
        elif self.mat_type == 'EMISSIVE':
            bsdf.inputs['Emission Strength'].default_value = 5.0
        elif self.mat_type == 'GLASS':
            bsdf.inputs['Transmission Weight'].default_value = 1.0
            bsdf.inputs['Roughness'].default_value = 0.0
        elif self.mat_type == 'CARBON':
            coord = nodes.new('ShaderNodeTexCoord')
            coord.location = (-700, 0)
            mapping = nodes.new('ShaderNodeMapping')
            mapping.location = (-500, 0)
            links.new(coord.outputs['UV'], mapping.inputs['Vector'])
            tex = nodes.new('ShaderNodeTexBrick')
            tex.location = (-300, 0)
            links.new(mapping.outputs['Vector'], tex.inputs['Vector'])
            tex.inputs['Color1'].default_value = (0.1, 0.1, 0.1, 1)
            tex.inputs['Color2'].default_value = (0.05, 0.05, 0.05, 1)
            tex.inputs['Mortar'].default_value = (0, 0, 0, 1)
            tex.inputs['Scale'].default_value = 50
            links.new(tex.outputs['Color'], bsdf.inputs['Base Color'])
            links.new(tex.outputs['Color'], bsdf.inputs['Roughness'])
            bsdf.inputs['Metallic'].default_value = 0.0
        elif self.mat_type == 'PRINTED':
            coord = nodes.new('ShaderNodeTexCoord')
            coord.location = (-700, 0)
            mapping = nodes.new('ShaderNodeMapping')
            mapping.location = (-500, 0)
            links.new(coord.outputs['UV'], mapping.inputs['Vector'])
            tex = nodes.new('ShaderNodeTexWave')
            tex.location = (-300, 0)
            links.new(mapping.outputs['Vector'], tex.inputs['Vector'])
            tex.inputs['Scale'].default_value = 100
            tex.bands_direction = 'Z'
            bump = nodes.new('ShaderNodeBump')
            bump.location = (-100, -200)
            bump.inputs['Strength'].default_value = 0.2
            links.new(tex.outputs['Color'], bump.inputs['Height'])
            links.new(bump.outputs['Normal'], bsdf.inputs['Normal'])
            bsdf.inputs['Base Color'].default_value = (0.8, 0.3, 0.0, 1)
            bsdf.inputs['Roughness'].default_value = 0.6
        elif self.mat_type == 'ALUMINUM':
            coord = nodes.new('ShaderNodeTexCoord')
            coord.location = (-700, 0)
            mapping = nodes.new('ShaderNodeMapping')
            mapping.location = (-500, 0)
            links.new(coord.outputs['UV'], mapping.inputs['Vector'])
            bsdf.inputs['Base Color'].default_value = (0.9, 0.9, 0.9, 1)
            bsdf.inputs['Metallic'].default_value = 1.0
            bsdf.inputs['Roughness'].default_value = 0.3
            tex = nodes.new('ShaderNodeTexNoise')
            tex.location = (-300, 0)
            links.new(mapping.outputs['Vector'], tex.inputs['Vector'])
            tex.inputs['Scale'].default_value = 200
            bump = nodes.new('ShaderNodeBump')
            bump.location = (-100, -200)
            bump.inputs['Strength'].default_value = 0.05
            links.new(tex.outputs['Fac'], bump.inputs['Height'])
            links.new(bump.outputs['Normal'], bsdf.inputs['Normal'])
        # 4. Assign to selection if in Edit Mode
        if obj.mode == 'EDIT':
            bpy.ops.object.material_slot_assign()
        self.report({'INFO'}, f"Added smart material: {self.mat_type}")
        return {'FINISHED'}
class LSD_OT_Material_LoadTexture(bpy.types.Operator, ImportHelper):
    """Load an image texture into the active material's Base Color"""
    bl_idname = "lsd.material_load_texture"
    bl_label = "Load Texture"
    bl_description = "Load an image file and connect it to the Base Color of the active material"
    filter_glob: bpy.props.StringProperty(default="*.png;*.jpg;*.jpeg;*.tga;*.bmp", options={'HIDDEN'})
    def execute(self, context: bpy.types.Context) -> Set[str]:
        obj = context.active_object
        mat = obj.active_material
        if not mat or not mat.use_nodes:
            self.report({'WARNING'}, "Active material must use nodes.")
            return {'CANCELLED'}
        try:
            img = bpy.data.images.load(self.filepath)
        except Exception as e:
            self.report({'ERROR'}, f"Failed to load image: {e}")
            return {'CANCELLED'}
        nodes = mat.node_tree.nodes
        links = mat.node_tree.links
        bsdf = next((n for n in nodes if n.type == 'BSDF_PRINCIPLED'), None)
        if not bsdf:
            self.report({'WARNING'}, "No Principled BSDF found in material.")
            return {'CANCELLED'}
        # --- Declarative Node Setup ---
        # Find or create the required nodes for a standard texture setup.
        # This ensures a predictable structure for the UI to find and control.
        tex_coord_node = next((n for n in nodes if n.type == 'TEX_COORD'), None) or nodes.new('ShaderNodeTexCoord')
        mapping_node = next((n for n in nodes if n.type == 'MAPPING'), None) or nodes.new('ShaderNodeMapping')
        tex_node = None
        if bsdf.inputs['Base Color'].is_linked:
            from_node = bsdf.inputs['Base Color'].links[0].from_node
            if from_node.type == 'TEX_IMAGE':
                tex_node = from_node
        if not tex_node:
            tex_node = nodes.new('ShaderNodeTexImage')
        # Position nodes for a clean layout
        tex_coord_node.location = (bsdf.location.x - 700, bsdf.location.y)
        mapping_node.location = (bsdf.location.x - 500, bsdf.location.y)
        tex_node.location = (bsdf.location.x - 300, bsdf.location.y)
        tex_node.image = img
        # Forcefully create the correct node links, removing any old ones.
        for link in list(bsdf.inputs['Base Color'].links): links.remove(link)
        for link in list(tex_node.inputs['Vector'].links): links.remove(link)
        for link in list(mapping_node.inputs['Vector'].links): links.remove(link)
        links.new(tex_coord_node.outputs['UV'], mapping_node.inputs['Vector'])
        links.new(mapping_node.outputs['Vector'], tex_node.inputs['Vector'])
        links.new(tex_node.outputs['Color'], bsdf.inputs['Base Color'])
        self.report({'INFO'}, f"Loaded texture '{img.name}' into '{mat.name}'")
        return {'FINISHED'}
class LSD_OT_Material_FromImage(bpy.types.Operator, ImportHelper):
    """Create a new material from an image file"""
    bl_idname = "lsd.material_from_image"
    bl_label = "New Material from Image"
    bl_description = "Create a new material with the selected image as the Base Color"
    bl_options = {'REGISTER', 'UNDO'}
    filter_glob: bpy.props.StringProperty(default="*.png;*.jpg;*.jpeg;*.tga;*.bmp", options={'HIDDEN'})
    def execute(self, context: bpy.types.Context) -> Set[str]:
        obj = context.active_object
        if not obj or obj.type != 'MESH':
            self.report({'WARNING'}, "Active object must be a mesh.")
            return {'CANCELLED'}
        # 1. Load Image
        try:
            img = bpy.data.images.load(self.filepath)
        except Exception as e:
            self.report({'ERROR'}, f"Failed to load image: {e}")
            return {'CANCELLED'}
        # 2. Create Material Slot and Material
        bpy.ops.object.material_slot_add()
        slot = obj.material_slots[obj.active_material_index]
        mat_name = os.path.splitext(os.path.basename(self.filepath))[0]
        mat = bpy.data.materials.new(name=mat_name)
        mat.use_nodes = True
        slot.material = mat
        # 3. Configure Nodes
        nodes = mat.node_tree.nodes
        links = mat.node_tree.links
        bsdf = nodes.new('ShaderNodeBsdfPrincipled')
        output = nodes.new('ShaderNodeOutputMaterial')
        output.location = (bsdf.location.x + 300, bsdf.location.y)
        links.new(bsdf.outputs['BSDF'], output.inputs['Surface'])
        tex_node = nodes.new('ShaderNodeTexImage')
        tex_node.location = (bsdf.location.x - 300, bsdf.location.y)
        tex_node.image = img
        links.new(tex_node.outputs['Color'], bsdf.inputs['Base Color'])
        self.report({'INFO'}, f"Created material '{mat.name}' from '{img.name}'")
        return {'FINISHED'}
class LSD_OT_AddMappingNodes(bpy.types.Operator):
    """Add Texture Coordinate and Mapping nodes to the active material"""
    bl_idname = "lsd.add_mapping_nodes"
    bl_label = "Add Transform Controls"
    bl_options = {'REGISTER', 'UNDO'}
    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj and obj.active_material and obj.active_material.use_nodes
    def execute(self, context):
        mat = context.active_object.active_material
        nodes = mat.node_tree.nodes
        links = mat.node_tree.links
        # Create nodes
        coord = nodes.new('ShaderNodeTexCoord')
        coord.location = (-900, 0)
        mapping = nodes.new('ShaderNodeMapping')
        mapping.location = (-700, 0)
        links.new(coord.outputs['UV'], mapping.inputs['Vector'])
        # Try to connect to existing texture nodes
        for node in nodes:
            if node.type.startswith('TEX_') and 'Vector' in node.inputs:
                if not node.inputs['Vector'].is_linked:
                    links.new(mapping.outputs['Vector'], node.inputs['Vector'])
        self.report({'INFO'}, "Added mapping nodes")
        return {'FINISHED'}
class LSD_OT_UV_SmartUnwrap(bpy.types.Operator):
    """Apply smart UV unwrapping to the active mesh"""
    bl_idname = "lsd.uv_smart_unwrap"
    bl_label = "Smart UV Unwrap"
    bl_options = {'REGISTER', 'UNDO'}
    method: bpy.props.EnumProperty(
        items=[
            ('SMART', "Smart Project", "Automatic projection based on angle"),
            ('CUBE', "Cube Projection", "Cube mapping"),
            ('CYLINDER', "Cylinder Projection", "Cylindrical mapping"),
            ('SPHERE', "Sphere Projection", "Spherical mapping"),
            ('UNWRAP', "Unwrap", "Standard unwrap (requires seams)"),
        ],
        default='SMART'
    )
    @classmethod
    def poll(cls, context):
        return context.active_object and context.active_object.type == 'MESH'
    def execute(self, context):
        obj = context.active_object
        original_mode = obj.mode
        if original_mode != 'EDIT':
            bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_all(action='SELECT')
        if self.method == 'SMART':
            bpy.ops.uv.smart_project(angle_limit=66.0, island_margin=0.02)
        elif self.method == 'CUBE':
            bpy.ops.uv.cube_project(cube_size=1.0)
        elif self.method == 'CYLINDER':
            bpy.ops.uv.cylinder_project()
        elif self.method == 'SPHERE':
            bpy.ops.uv.sphere_project()
        elif self.method == 'UNWRAP':
            bpy.ops.uv.unwrap(method='ANGLE_BASED', margin=0.02)
        if original_mode != 'EDIT':
            bpy.ops.object.mode_set(mode=original_mode)
        self.report({'INFO'}, f"Applied UV: {self.method}")
        return {'FINISHED'}
class LSD_OT_Material_Merge(bpy.types.Operator):
    """Merge all enabled material slots into a single composite material"""
    bl_idname = "lsd.material_merge"
    bl_label = "Composite All Layers"
    bl_description = "Combines all enabled material layers into a single composite material (preserving settings) and deletes old layers"
    bl_options = {'REGISTER', 'UNDO'}
    def execute(self, context: bpy.types.Context) -> Set[str]:
        obj = context.active_object
        if not obj or not obj.material_slots:
            return {'CANCELLED'}
        comp_mat_name = f"Composite_{obj.name}"
        # Filter valid source slots
        source_slots = [
            slot for slot in obj.material_slots
            if slot.material and slot.material.name != comp_mat_name and getattr(slot, "lsd_enabled", True)
        ]
        if not source_slots:
            self.report({'WARNING'}, "No enabled material layers to merge.")
            return {'CANCELLED'}
        # Create or Reset Composite Material
        comp_mat = bpy.data.materials.get(comp_mat_name)
        if not comp_mat:
            comp_mat = bpy.data.materials.new(name=comp_mat_name)
        comp_mat.use_nodes = True
        tree = comp_mat.node_tree
        tree.nodes.clear()
        output_node = tree.nodes.new('ShaderNodeOutputMaterial')
        output_node.location = (300, 0)
        # Stack Shaders
        current_shader_socket = None
        x_offset = -300
        def copy_material_to_group(mat, group_tree):
            """Helper to copy a material's node tree into a node group."""
            group_tree.nodes.clear()
            # Create Group Outputs
            g_out = group_tree.nodes.new('NodeGroupOutput')
            g_out.location = (300, 0)
            group_tree.interface.new_socket(name="Surface", in_out='OUTPUT', socket_type='NodeSocketShader')
            group_tree.interface.new_socket(name="Alpha", in_out='OUTPUT', socket_type='NodeSocketFloat')
            # Copy nodes
            node_map = {}
            src_tree = mat.node_tree
            for node in src_tree.nodes:
                if node.type == 'OUTPUT_MATERIAL': continue
                new_node = group_tree.nodes.new(node.bl_idname)
                new_node.location = node.location
                new_node.label = node.label
                new_node.name = node.name
                # Copy properties
                for prop in node.bl_rna.properties:
                    if not prop.is_readonly:
                        try:
                            setattr(new_node, prop.identifier, getattr(node, prop.identifier))
                        except: pass
                # Copy inputs defaults
                for i, inp in enumerate(node.inputs):
                    if hasattr(inp, 'default_value'):
                        try:
                            new_node.inputs[i].default_value = inp.default_value
                        except: pass
                if node.type == 'TEX_IMAGE' and node.image:
                    new_node.image = node.image
                node_map[node] = new_node
            # Reconnect links
            for link in src_tree.links:
                if link.from_node in node_map and link.to_node in node_map:
                    try:
                        group_tree.links.new(node_map[link.from_node].outputs[link.from_socket.name],
                                             node_map[link.to_node].inputs[link.to_socket.name])
                    except: pass
            # Connect to Group Output
            # 1. Surface
            mat_out = next((n for n in src_tree.nodes if n.type == 'OUTPUT_MATERIAL'), None)
            if mat_out and mat_out.inputs['Surface'].is_linked:
                link = mat_out.inputs['Surface'].links[0]
                if link.from_node in node_map:
                    group_tree.links.new(node_map[link.from_node].outputs[link.from_socket.name], g_out.inputs['Surface'])
            # 2. Alpha (Find Principled BSDF)
            bsdf = next((n for n in node_map.values() if n.type == 'BSDF_PRINCIPLED'), None)
            if bsdf:
                if bsdf.inputs['Alpha'].is_linked:
                    link = bsdf.inputs['Alpha'].links[0]
                    # Connect the source of the alpha link to the group output
                    group_tree.links.new(link.from_node.outputs[link.from_socket.name], g_out.inputs['Alpha'])
                else:
                    # Create a value node for constant alpha
                    val = group_tree.nodes.new('ShaderNodeValue')
                    val.outputs[0].default_value = bsdf.inputs['Alpha'].default_value
                    val.location = (g_out.location.x - 200, g_out.location.y - 200)
                    group_tree.links.new(val.outputs[0], g_out.inputs['Alpha'])
            else:
                # Default Alpha 1.0
                val = group_tree.nodes.new('ShaderNodeValue')
                val.outputs[0].default_value = 1.0
                val.location = (g_out.location.x - 200, g_out.location.y - 200)
                group_tree.links.new(val.outputs[0], g_out.inputs['Alpha'])
        # Layer stacking: We iterate from bottom to top of the UI list.
        # To make the top-most item in the list the top-most visual layer,
        # we start from the bottom items as the base and stack upwards.
        for i, slot in enumerate(reversed(source_slots)):
            mat = slot.material
            if not mat or not mat.use_nodes: continue
            # Create a Group Node for each material
            group_node = tree.nodes.new('ShaderNodeGroup')
            group_node.location = (x_offset, i * -300)
            group_node.label = mat.name
            # Create a unique node group for this layer to preserve settings
            group_name = f"LSD_Layer_{mat.name}_{obj.name}_{i}"
            if group_name in bpy.data.node_groups:
                bpy.data.node_groups.remove(bpy.data.node_groups[group_name])
            new_group = bpy.data.node_groups.new(group_name, 'ShaderNodeTree')
            copy_material_to_group(mat, new_group)
            group_node.node_tree = new_group
            # Get outputs
            layer_shader = group_node.outputs.get('Surface')
            layer_alpha = group_node.outputs.get('Alpha')
            if not layer_shader: continue
            if current_shader_socket is None:
                current_shader_socket = layer_shader
            else:
                # Mix with previous layer
                mix_node = tree.nodes.new('ShaderNodeMixShader')
                mix_node.location = (x_offset + 200, i * -300)
                # Use the layer's alpha as the mix factor
                if layer_alpha:
                    tree.links.new(layer_alpha, mix_node.inputs['Fac'])
                else:
                    mix_node.inputs['Fac'].default_value = 1.0
                tree.links.new(current_shader_socket, mix_node.inputs[1])
                tree.links.new(layer_shader, mix_node.inputs[2])
                current_shader_socket = mix_node.outputs['Shader']
            x_offset -= 400
        if current_shader_socket:
            tree.links.new(current_shader_socket, output_node.inputs['Surface'])
        # AI Editor Note: Ensure the core composite material is Opaque.
        # This follows the user's request that alpha controls layer transparency
        # but not the global object transparency (unless they manually override it).
        comp_mat.blend_method = 'OPAQUE'
        comp_mat.shadow_method = 'OPAQUE'
        # Assign Composite Material to a new slot if needed
        comp_slot_index = obj.material_slots.find(comp_mat_name)
        if comp_slot_index == -1:
            bpy.ops.object.material_slot_add()
            comp_slot_index = len(obj.material_slots) - 1
        obj.material_slots[comp_slot_index].material = comp_mat
        # Ensure the composite slot is at the very bottom (base index) but assigned to all faces
        # Note: We keep the source slots for non-destructive editing.
        # Assign all faces to composite
        for poly in obj.data.polygons:
            poly.material_index = comp_slot_index
        # AI Editor Note: Removed destructive slot removal loop to allow real-time preview
        # and editing of individual layers. The composite material now acts as the
        # "flattened" output while preserving the working layers in the slot list.
        self.report({'INFO'}, f"Updated composite material '{comp_mat_name}'")
        return {'FINISHED'}
class LSD_OT_Material_Add(bpy.types.Operator):
    """Add a new material slot (Blank or Existing)"""
    bl_idname = "lsd.material_add"
    bl_label = "Add Material"
    bl_description = "Adds a new material slot. Use properties to select existing material."
    bl_options = {'REGISTER', 'UNDO'}
    mode: bpy.props.EnumProperty(items=[('NEW', "New", ""), ('EXISTING', "Existing", "")], default='NEW')
    def get_materials(self, context):
        return [(mat.name, mat.name, "") for mat in bpy.data.materials]
    material_name: bpy.props.EnumProperty(items=get_materials, name="Material")
    def execute(self, context: bpy.types.Context) -> Set[str]:
        obj = context.active_object
        if not obj: return {'CANCELLED'}
        bpy.ops.object.material_slot_add()
        if self.mode == 'NEW':
            mat = bpy.data.materials.new(name=f"Mat_{obj.name}")
            mat.use_nodes = True
            # --- AI Editor Note: Add default nodes for a valid base material ---
            nodes = mat.node_tree.nodes
            links = mat.node_tree.links
            # Ensure there's an output node
            output = nodes.get("Material Output") or nodes.new('ShaderNodeOutputMaterial')
            # Create and link a Principled BSDF
            bsdf = nodes.new('ShaderNodeBsdfPrincipled')
            bsdf.location = output.location.x - 250, output.location.y
            links.new(bsdf.outputs['BSDF'], output.inputs['Surface'])
        else:
            mat = bpy.data.materials.get(self.material_name)
        if mat:
            obj.material_slots[obj.active_material_index].material = mat
        if obj.mode == 'EDIT':
            bpy.ops.object.material_slot_assign()
        return {'FINISHED'}
    def invoke(self, context, event):
        if self.mode == 'EXISTING':
            return context.window_manager.invoke_search_popup(self)
        return {'RUNNING_MODAL'}
class LSD_UL_Mat_List(bpy.types.UIList):
    """Simple UI List for Materials"""
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        slot = item
        mat = slot.material
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            row.prop(slot, "lsd_enabled", text="")
            row.prop(slot, "material", text="", emboss=False, icon='MATERIAL')
        elif self.layout_type == 'GRID':
            layout.alignment = 'CENTER'
            layout.label(text="", icon_value=icon)
class LSD_UL_SlinkyHooks_List(bpy.types.UIList):
    """Simple UI List for Slinky Hooks"""
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        hook = item.target
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            if hook:
                row.label(text=hook.name, icon='EMPTY_AXIS')
            else:
                row.label(text="(Empty Slot)", icon='ERROR')
        elif self.layout_type == 'GRID':
            layout.alignment = 'CENTER'
            layout.label(text="", icon_value=icon)
class LSD_OT_Paint_SetupBrush(bpy.types.Operator):
    """Configure a texture paint brush with a specific preset"""
    bl_idname = "lsd.paint_setup_brush"
    bl_label = "Setup Smart Brush"
    bl_description = "Sets up the active brush with texture and settings for painting"
    bl_options = {'REGISTER', 'UNDO'}
    brush_type: bpy.props.EnumProperty(
        items=[
            ('DIRT', "Dirt/Grime", "Dark, low opacity, cloudy pattern"),
            ('SCRATCH', "Scratches", "Light, high contrast, noise pattern"),
            ('RUST', "Rust", "Reddish-brown, musgrave pattern"),
            ('OIL', "Oil Stain", "Dark, glossy, smooth"),
        ]
    )
    def execute(self, context: bpy.types.Context) -> Set[str]:
        obj = context.active_object
        if not obj or obj.type != 'MESH':
            self.report({'WARNING'}, "Select a mesh object.")
            return {'CANCELLED'}
        # 0. Ensure UV Map (Crucial for painting)
        if not obj.data.uv_layers:
            bpy.ops.mesh.uv_texture_add()
        # 1. Ensure Material and Image
        mat = obj.active_material or bpy.data.materials.new(name=f"Paint_{obj.name}")
        if not mat:
            bpy.ops.lsd.add_smart_material(mat_type='PLASTIC')
            mat = obj.active_material
        if not mat.use_nodes: mat.use_nodes = True
        nodes = mat.node_tree.nodes
        bsdf = next((n for n in nodes if n.type == 'BSDF_PRINCIPLED'), None)
        if not bsdf: return {'CANCELLED'}
        # Check for image texture connected to Base Color
        base_color_socket = bsdf.inputs['Base Color']
        img_node = next((l.from_node for l in base_color_socket.links if l.from_node.type == 'TEX_IMAGE'), None)
        if not img_node:
            img_node = nodes.new('ShaderNodeTexImage')
            img_node.location = (bsdf.location.x - 300, bsdf.location.y)
            img = bpy.data.images.new(name=f"{obj.name}_BaseColor", width=2048, height=2048)
            img.generated_color = base_color_socket.default_value
            img_node.image = img
            mat.node_tree.links.new(img_node.outputs['Color'], base_color_socket)
        # 2. Enter Texture Paint Mode
        if obj.mode != 'TEXTURE_PAINT':
            bpy.ops.object.mode_set(mode='TEXTURE_PAINT')
        # 3. Setup Brush
        brush = context.tool_settings.image_paint.brush
        if not brush:
            # Fix: Ensure a brush exists to make the feature usable
            if bpy.data.brushes:
                brush = bpy.data.brushes[0]
            else:
                brush = bpy.data.brushes.new(name="LSD_Smart_Brush", mode='TEXTURE_PAINT')
            context.tool_settings.image_paint.brush = brush
        if self.brush_type == 'DIRT':
            brush.color = (0.1, 0.08, 0.05); brush.strength = 0.4; brush.blend = 'MIX'
            tex = bpy.data.textures.get("LSD_Brush_Dirt") or bpy.data.textures.new("LSD_Brush_Dirt", type='CLOUDS')
            tex.noise_scale = 0.5; brush.texture = tex
        elif self.brush_type == 'SCRATCH':
            brush.color = (0.8, 0.8, 0.8); brush.strength = 0.8; brush.blend = 'LIGHTEN'
            tex = bpy.data.textures.get("LSD_Brush_Scratch") or bpy.data.textures.new("LSD_Brush_Scratch", type='NOISE')
            brush.texture = tex
        elif self.brush_type == 'RUST':
            brush.color = (0.4, 0.15, 0.05); brush.strength = 0.6; brush.blend = 'MIX'
            tex = bpy.data.textures.get("LSD_Brush_Rust") or bpy.data.textures.new("LSD_Brush_Rust", type='MUSGRAVE')
            tex.noise_scale = 0.8; brush.texture = tex
        elif self.brush_type == 'OIL':
            brush.color = (0.05, 0.05, 0.05); brush.strength = 0.5; brush.blend = 'MIX'; brush.texture = None
        self.report({'INFO'}, f"Brush set to {self.brush_type}")
        return {'FINISHED'}
class LSD_OT_ExportGazeboWorld(bpy.types.Operator):
    """Exports a simple Gazebo world file containing the robot"""
    bl_idname = "lsd.export_gazebo_world"
    bl_label = "Export Gazebo World"
    bl_options = {'INTERNAL'}
    filepath: bpy.props.StringProperty(subtype="FILE_PATH")
    def execute(self, context: bpy.types.Context) -> Set[str]:
        rig = context.scene.lsd_active_rig
        if not rig:
            self.report({'ERROR'}, "No active rig selected.")
            return {'CANCELLED'}
        robot_name = rig.name
        sdf = ET.Element('sdf', version='1.6')
        world = ET.SubElement(sdf, 'world', name='default')
        # Add lighting
        light = ET.SubElement(world, 'light', name='sun', type='directional')
        ET.SubElement(light, 'cast_shadows').text = 'true'
        ET.SubElement(light, 'pose').text = '0 0 10 0 0 0'
        ET.SubElement(light, 'direction').text = '-0.5 -0.5 -1'
        ET.SubElement(ET.SubElement(light, 'diffuse'), 'a').text = '0.8 0.8 0.8 1'
        ET.SubElement(ET.SubElement(light, 'specular'), 'a').text = '0.2 0.2 0.2 1'
        # Add ground plane
        include_ground = ET.SubElement(world, 'include')
        ET.SubElement(include_ground, 'uri').text = 'model://ground_plane'
        # Include the robot model
        include_robot = ET.SubElement(world, 'include')
        ET.SubElement(include_robot, 'uri').text = f'model://{robot_name}'
        ET.SubElement(include_robot, 'name').text = robot_name
        ET.SubElement(include_robot, 'pose').text = '0 0 0 0 0 0'
        tree = ET.ElementTree(sdf)
        ET.indent(tree, space="  ", level=0)
        tree.write(self.filepath, encoding='utf-8', xml_declaration=True)
        self.report({'INFO'}, f"Exported Gazebo world to {self.filepath}")
        return {'FINISHED'}
class LSD_OT_LinkChainDriver(bpy.types.Operator):
    """
    Links the chain's animation to the rotation of a selected object (e.g., a sprocket).
    Calculates the linear motion based on the driver's radius.
    """
    bl_idname = "lsd.link_chain_driver"
    bl_label = "Link Driver"
    bl_options = {'REGISTER', 'UNDO'}
    def execute(self, context: bpy.types.Context) -> Set[str]:
        path_obj = context.active_object
        props = path_obj.lsd_pg_mech_props
        driver_obj = props.chain_drive_target
        # If no specific driver is selected, try to use the first wrap object
        if not driver_obj and len(props.chain_wrap_items) > 0:
            driver_obj = props.chain_wrap_items[0].target
            props.chain_drive_target = driver_obj
        if not driver_obj:
            self.report({'WARNING'}, "No driver object selected or found in wrap list.")
            return {'CANCELLED'}
        # Determine the effective radius for the driver
        radius = 1.0
        if hasattr(driver_obj, "lsd_pg_mech_props") and driver_obj.lsd_pg_mech_props.is_part:
            # If it's a generated gear/pulley, use its parametric radius
            radius = driver_obj.lsd_pg_mech_props.radius
        else:
            # Otherwise, estimate from dimensions (assuming Z-axis rotation, so X/Y radius)
            radius = (driver_obj.dimensions.x + driver_obj.dimensions.y) / 4.0
        # AI Editor Note: Fallback for objects with zero dimensions (like Empties) to ensure movement.
        if radius < 0.001: radius = 1.0
        # Create the driver on the chain's animation offset property
        # AI Editor Note: Remove existing driver first to ensure a clean update.
        # This fixes the issue where the driver object couldn't be changed.
        path_obj.driver_remove('["lsd_native_anim_offset"]')
        # This property drives the Geometry Nodes modifier input
        fcurve_anim = path_obj.driver_add('["lsd_native_anim_offset"]')
        driver_anim = fcurve_anim.driver
        # AI Editor Note: Clear existing variables to ensure a clean switch when changing drivers.
        # This fixes the issue where the driver would stick to the previous object.
        for v in list(driver_anim.variables):
            driver_anim.variables.remove(v)
        driver_anim.type = 'SCRIPTED'
        var_rot = driver_anim.variables.new()
        var_rot.name = "rotation"
        # AI Editor Note: Use SINGLE_PROP for raw Euler rotation to avoid
        # -180/180 degree wrapping (snapping) that occurs with TRANSFORMS.
        var_rot.type = 'SINGLE_PROP'
        var_rot.targets[0].id = driver_obj
        # AI Editor Note: Target the Z-axis of the Euler rotation directly via data_path string.
        # DriverTarget does not have a 'data_path_index' attribute.
        var_rot.targets[0].data_path = "rotation_euler[2]"
        # Store the calculated radius. This might trigger the update callback,
        # but we call it explicitly below to ensure the expression is set correctly
        # even if the radius hasn't changed.
        props.chain_drive_radius = radius
        # Apply the driver expression using the new settings (Radius, Ratio, Invert)
        update_chain_driver_settings(props, context)
        self.report({'INFO'}, f"Linked chain to '{driver_obj.name}' with radius {radius:.3f}")
        return {'FINISHED'}
class LSD_OT_AddBoolean(bpy.types.Operator):
    bl_idname = "lsd.add_boolean"
    bl_label = "Add Boolean"
    bl_options = {'REGISTER', 'UNDO'}
    operation: bpy.props.EnumProperty(items=[('DIFFERENCE', "Cut", ""), ('UNION', "Join", ""), ('INTERSECT', "Intersect", "")])
    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        active_obj = context.active_object
        if not active_obj or active_obj.type != 'MESH':
            cls.poll_message_set("Active object must be a mesh.")
            return False
        selected_meshes = [obj for obj in context.selected_objects if obj != active_obj and obj.type == 'MESH']
        if not selected_meshes:
            cls.poll_message_set("Select at least one other mesh object.")
            return False
        return True
    def execute(self, context: bpy.types.Context) -> Set[str]:
        active = context.active_object
        # The objects to be used by the modifier are the selected ones, excluding the active one.
        selected = [o for o in context.selected_objects if o != active and o.type == 'MESH']
        for target in selected:
            mod = active.modifiers.new(name=f"{BOOL_PREFIX}{target.name}", type='BOOLEAN')
            mod.operation = self.operation
            mod.object = target
            mod.solver = 'EXACT'
            target.display_type = 'BOUNDS'
            target.hide_render = True
        self.report({'INFO'}, f"Added {len(selected)} boolean modifier(s) to '{active.name}'.")
        return {'FINISHED'}
def create_hook_anchor(context, mesh_obj, vert_indices, name_prefix="Hook", display_size=0.05, display_type='SINGLE_ARROW', force_location=None):
    """
    Creates a Parametric Hook anchor at the world-space center of the provided vertex indices.
    Uses hook_reset to ensure the mesh does not warp when the modifier is bound.
    Expects context to be in OBJECT mode.
    """
    import mathutils
    mw = mesh_obj.matrix_world
    
    # Project Task 1.1.2: Prevent stacking duplicate hooks on the exact same selection
    if vert_indices:
        target_set = set(vert_indices)
        for mod in mesh_obj.modifiers:
            if mod.type == 'HOOK' and mod.object and mod.vertex_group:
                vg = mesh_obj.vertex_groups.get(mod.vertex_group)
                if vg:
                    hooked_verts = set()
                    for v in mesh_obj.data.vertices:
                        try:
                            if vg.weight(v.index) > 0.5:
                                hooked_verts.add(v.index)
                        except RuntimeError:
                            pass
                    if target_set == hooked_verts:
                        # Re-use the existing anchor to prevent double-transform shear
                        if mod.object.get("lsd_anchor") or mod.object.get("lsd_is_dimension_hook"):
                            return mod.object

    if not vert_indices:
        if force_location is not None:
            empty = bpy.data.objects.new(f"{name_prefix}_{mesh_obj.name}", None)
            empty.location = force_location
            empty.empty_display_type = display_type
            empty.empty_display_size = display_size
            empty["lsd_anchor"] = True
            context.scene.collection.objects.link(empty)
            return empty
        return None
    coords = [mw @ mesh_obj.data.vertices[i].co.copy() for i in vert_indices]
    if not coords:
        return None
    # Determination of Target Placed Location (Project Task 1.1.2)
    if force_location is not None:
        final_location = force_location
    else:
        world_center = sum(coords, mathutils.Vector()) / len(coords)
        final_location = world_center
        # If 3D Cursor is chosen, use it for the Anchor visual position, while keeping the modifier center stable.
        if context.scene.lsd_anchor_placement_source == 'CURSOR':
             final_location = context.scene.cursor.location
    # 1. Create Empty
    empty = bpy.data.objects.new(f"{name_prefix}_{mesh_obj.name}", None)
    empty.location = final_location
    empty.empty_display_type = display_type
    empty.empty_display_size = display_size
    empty["lsd_anchor"] = True
    context.scene.collection.objects.link(empty)
    # 2. Setup Vertex Group
    vg_name = f"VG_{empty.name}"
    vg = mesh_obj.vertex_groups.new(name=vg_name)
    vg.add(vert_indices, 1.0, 'REPLACE')
    # 3. Setup Modifier
    mod = mesh_obj.modifiers.new(name="HookAnchor", type='HOOK')
    mod.show_viewport = False # ATOMIC GUARD: Prevents initial jump on binding
    mod.object = empty
    mod.vertex_group = vg.name
    # Ensure matrix data is absolute after location set and linking
    context.view_layer.update()
    # ZERO-WARP: Force evaluate to ensure world matrices are absolute before inverse calculation
    context.evaluated_depsgraph_get().update()
    # 4. Neutralize Binding (Prevent Warping)
    # Correct inverse matrix for a hook is the relative transform from mesh to empty.
    mod.matrix_inverse = empty.matrix_world.inverted() @ mesh_obj.matrix_world
    mod.show_viewport = True # RE-ENABLE: Zero-warp is now baked
    return empty
class LSD_OT_AlignAllSelectedDimensions(bpy.types.Operator):
    """Applies the current alignment settings of the active dimension to all selected dimensions."""
    bl_idname = "lsd.align_all_selected_dimensions"
    bl_label = "Align All Selected Dimensions"
    bl_description = "Apply the active dimension's alignment offsets (+X/-X etc) to all selected dimension assemblies"
    bl_options = {'REGISTER', 'UNDO'}
    @classmethod
    def poll(cls, context):
        from . import core
        return context.active_object and core.get_dimension_host(context.active_object) is not None
    def execute(self, context):
        from . import core
        host = core.get_dimension_host(context.active_object)
        if not host: return {'CANCELLED'}
        src_props = host.lsd_pg_dim_props
        count = 0
        # 1. Capture alignment state
        tags = {
            "align_x": src_props.align_x,
            "align_nx": src_props.align_nx,
            "align_y": src_props.align_y,
            "align_ny": src_props.align_ny,
            "align_z": src_props.align_z,
            "align_nz": src_props.align_nz
        }
        # 2. Propagate to selection
        for obj in context.selected_objects:
            target_host = core.get_dimension_host(obj)
            if target_host and target_host != host:
                tgt_props = target_host.lsd_pg_dim_props
                for attr, val in tags.items():
                    setattr(tgt_props, attr, val)
                count += 1
        self.report({'INFO'}, f"Applied alignment offsets to {count} dimension(s).")
        return {'FINISHED'}
class LSD_OT_AddParametricAnchor(bpy.types.Operator):
    """Creates an Empty object and adds a Hook modifier to the selected mesh elements"""
    bl_idname = "lsd.add_parametric_anchor"
    bl_label = "Attach Hook to Selected"
    bl_options = {'REGISTER', 'UNDO'}
    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return context.active_object and context.active_object.type == 'MESH'
    def execute(self, context: bpy.types.Context) -> Set[str]:
        initial_mode = context.mode
        initial_active = context.view_layer.objects.active
        scene = context.scene
        obj = initial_active
        if not obj or obj.type != 'MESH':
            self.report({'WARNING'}, "Requires an active Mesh object.")
            return {'CANCELLED'}
        active_name = obj.name
        try:
            # Scale normalization
            unit_scale = scene.unit_settings.scale_length
            s = 1.0 / unit_scale if unit_scale > 0 else 1.0
            targets = [o for o in context.selected_objects if o.type == 'MESH']
            if not targets:
                if obj and obj.type == 'MESH': targets = [obj]
                else:
                    self.report({'WARNING'}, "Action requires selected Mesh object(s).")
                    return {'CANCELLED'}
            # Initialize island mapping
            all_islands = {} # Mapping obj -> List[List[vertex_indices]]
            # --- Collection Phase ---
            # Read grouping mode first so the collection logic can branch appropriately.
            # GROUP mode: merge contiguous selections into connectivity islands (one hook per group).
            # INDIVIDUAL mode: one island per selected face/edge/vertex to guarantee one hook each.
            group_mode = scene.lsd_anchor_grouping_mode
            for target in targets:
                if initial_mode.startswith('EDIT'):
                    bm = bmesh.from_edit_mesh(target.data)
                    bm.verts.ensure_lookup_table()
                    bm.edges.ensure_lookup_table()
                    bm.faces.ensure_lookup_table()
                    islands = []
                    if group_mode == 'INDIVIDUAL':
                        # Detect the active mesh selection mode to build per-element islands.
                        # Face select → one island per selected face (all verts of the face).
                        # Edge select → one island per selected edge (its two verts).
                        # Vertex select → one island per selected vertex (single vert).
                        mesh_select_mode = context.tool_settings.mesh_select_mode  # (vert, edge, face)
                        if mesh_select_mode[2]:  # Face select mode
                            for face in bm.faces:
                                if face.select:
                                    islands.append([v.index for v in face.verts])
                        elif mesh_select_mode[1]:  # Edge select mode
                            for edge in bm.edges:
                                if edge.select:
                                    islands.append([v.index for v in edge.verts])
                        else:  # Vertex select mode (default)
                            for vert in bm.verts:
                                if vert.select:
                                    islands.append([vert.index])
                    else:
                        # GROUP mode: walk adjacency to merge connected selected verts into islands.
                        remaining = {v for v in bm.verts if v.select}
                        while remaining:
                            root_vert = remaining.pop()
                            island = {root_vert}
                            stack = [root_vert]
                            while stack:
                                v = stack.pop()
                                for edge in v.link_edges:
                                    other = edge.other_vert(v)
                                    if other.select and other in remaining:
                                        remaining.remove(other)
                                        island.add(other)
                                        stack.append(other)
                            islands.append([v.index for v in island])
                    if islands:
                        all_islands[target] = islands
                    bmesh.update_edit_mesh(target.data)
                else:
                    # Object Mode: Single "island" containing all vertices
                    all_islands[target] = [[v.index for v in target.data.vertices]]
            if not all_islands:
                self.report({'WARNING'}, "No selection found on objects.")
                return {'CANCELLED'}
            # --- Transient Context Transition (USER ESCAPE) ---
            if initial_mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT')
            # --- Generation & Assignment Phase ---
            # --- Generation & Assignment Phase ---
            # Determination of Target Placed Location (Project Task 1.1.2)
            import mathutils
            # --- PATH A: Grouped Anchor ---
            if group_mode == 'GROUP':
                # 1. Shared World Position
                if scene.lsd_anchor_placement_source == 'CURSOR':
                    shared_location = mathutils.Vector(scene.cursor.location)
                else:
                    all_coords = []
                    for target, islands in all_islands.items():
                        mw = target.matrix_world
                        for island_indices in islands:
                             all_coords.extend([mw @ target.data.vertices[i].co.copy() for i in island_indices])
                    shared_location = sum(all_coords, mathutils.Vector()) / len(all_coords) if all_coords else mathutils.Vector()
                # 2. Precision Sizing
                ref_objs = list(all_islands.keys())
                ref_obj = initial_active if (initial_active and initial_active in ref_objs) else ref_objs[0]
                if scene.lsd_anchor_auto_size:
                    dims = ref_obj.dimensions
                    avg_dim = (dims.x + dims.y + dims.z) / 3.0
                    if avg_dim < 0.001: avg_dim = 0.1
                    final_display_size = avg_dim * 0.2
                else:
                    final_display_size = scene.lsd_anchor_initial_size
                # 3. Create SINGLE Empty
                empty = bpy.data.objects.new(f"HookGroup_{ref_obj.name}", None)
                empty.location = shared_location
                empty.empty_display_type = 'SINGLE_ARROW'
                empty.empty_display_size = final_display_size
                empty["lsd_anchor"] = True
                empty.show_in_front = True
                context.scene.collection.objects.link(empty)
                # 4. Bind ALL
                for target, islands in all_islands.items():
                    # Flatten islands for grouped binding
                    flat_indices = [idx for island in islands for idx in island]
                    vg_name = f"VG_{empty.name}"
                    vg = target.vertex_groups.new(name=vg_name)
                    vg.add(flat_indices, 1.0, 'REPLACE')
                    mod = target.modifiers.new(name="HookAnchor", type='HOOK')
                    mod.object = empty
                    mod.vertex_group = vg_name
                    # ZERO-WARP: Core Fix. Explicitly set matrix_inverse for total stability in 4.x.
                    context.view_layer.update()
                    mod.matrix_inverse = empty.matrix_world.inverted() @ target.matrix_world
            # --- PATH B: Individual Anchors ---
            else:
                created_count = 0
                for target, islands in all_islands.items():
                    # AI Editor Note: Islands already grouped by connectivity in collection phase.
                    for island_indices in islands:
                        # 1. Location
                        if scene.lsd_anchor_placement_source == 'CURSOR':
                             loc = mathutils.Vector(scene.cursor.location)
                        else:
                             mw = target.matrix_world
                             loc = sum([mw @ target.data.vertices[i].co.copy() for i in island_indices], mathutils.Vector()) / len(island_indices)
                        # 2. Size
                        if scene.lsd_anchor_auto_size:
                            avg_dim = (target.dimensions.x + target.dimensions.y + target.dimensions.z) / 3.0
                            if avg_dim < 0.001: avg_dim = 0.1
                            fs = avg_dim * 0.2
                        else:
                            fs = scene.lsd_anchor_initial_size
                        # 3. Create
                        # Unique name per island
                        name_base = f"Hook_{target.name}"
                        if len(islands) > 1:
                             name_base += f"_{island_indices[0]}"
                        empty = bpy.data.objects.new(name_base, None)
                        empty.location = loc
                        empty.empty_display_type = 'SINGLE_ARROW'
                        empty.empty_display_size = fs
                        empty["lsd_anchor"] = True
                        empty.show_in_front = True
                        context.scene.collection.objects.link(empty)
                        # 4. Bind
                        vg_name = f"VG_{empty.name}"
                        vg = target.vertex_groups.new(name=vg_name)
                        vg.add(island_indices, 1.0, 'REPLACE')
                        mod = target.modifiers.new(name="HookAnchor", type='HOOK')
                        mod.object = empty
                        mod.vertex_group = vg_name
                        # ZERO-WARP Fix: Force matrix resolution
                        context.view_layer.update()
                        mod.matrix_inverse = empty.matrix_world.inverted() @ target.matrix_world
                        created_count += 1
                self.report({'INFO'}, f"Created {created_count} individual anchor(s).")
            if not empty:
                self.report({'ERROR'}, "Failed to generate anchors.")
                return {'CANCELLED'}
            self.report({'INFO'}, f"[LSD v1.3.1] Hook Attached. Workspace Restored: {context.mode}")
        except Exception as e:
            self.report({'ERROR'}, f"Hook System Failure: {str(e)}")
            return {'CANCELLED'}
        finally:
            # --- Categorical Mode Restoration (USER PRIORITY) ---
            if active_name in bpy.data.objects:
                target_obj = bpy.data.objects[active_name]
                # 2. State Prep for Mode Set
                if context.view_layer.objects.active != target_obj:
                    context.view_layer.objects.active = target_obj
                target_obj.select_set(True)
                # 3. Aggressive Restoration
                # Check for any 'EDIT' flavor (EDIT_MESH, EDIT_CURVE, etc.)
                if initial_mode and "EDIT" in initial_mode:
                    if context.mode != initial_mode:
                        try:
                            bpy.ops.object.mode_set(mode='EDIT')
                        except:
                            pass
                elif initial_mode == 'OBJECT':
                    if context.mode != 'OBJECT':
                        try:
                            bpy.ops.object.mode_set(mode='OBJECT')
                        except:
                            pass
            self.report({'INFO'}, f"[LSD v1.3.1] Hook Attached. Workspace Restored: {context.mode}")
        return {'FINISHED'}
class LSD_OT_AddMarker(bpy.types.Operator):
    """Creates an Empty marker at each selected vertex"""
    bl_idname = "lsd.add_marker"
    bl_label = "Attach Marker"
    bl_description = "Attach a Marker empty follower to selected."
    bl_options = {'REGISTER', 'UNDO'}
    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        obj = context.active_object
        if not obj: return False
        # Allow Mesh OR an existing LSD anchor/hook
        if obj.type == 'MESH': return True
        if obj.type == 'EMPTY' and (obj.get("lsd_anchor") or obj.get("lsd_is_dimension_hook")):
            return True
        return False
    def execute(self, context: bpy.types.Context) -> Set[str]:
        # --- Context Store ---
        initial_mode = context.mode
        initial_active = context.view_layer.objects.active
        obj = initial_active
        if not obj:
            self.report({'WARNING'}, "Requires an active Mesh or Anchor object.")
            return {'CANCELLED'}
        active_name = obj.name
        try:
            # Target Resolution (Mesh or LSD Anchor)
            targets = []
            for o in context.selected_objects:
                if o.type == 'MESH': targets.append(o)
                elif o.type == 'EMPTY' and (o.get("lsd_anchor") or o.get("lsd_is_dimension_hook")):
                    targets.append(o)
            if not targets:
                if obj and (obj.type == 'MESH' or obj.type == 'EMPTY'): targets = [obj]
                else: return {'CANCELLED'}
            created_count = 0
            created_empties = []
            all_indices = {} # Mapping target_name -> list of indices (or None for empties)
            # --- Collection Phase ---
            for target in targets:
                if target.type == 'EMPTY':
                    # Empties have no vertices, we mark their origin
                    all_indices[target.name] = None
                    continue
                if initial_mode.startswith('EDIT'):
                    bm = bmesh.from_edit_mesh(target.data)
                    bm.verts.ensure_lookup_table()
                    indices = [v.index for v in bm.verts if v.select]
                    if indices:
                        all_indices[target.name] = indices
                    bmesh.update_edit_mesh(target.data)
                else:
                    all_indices[target.name] = [v.index for v in target.data.vertices]
            if not all_indices:
                self.report({'WARNING'}, "No selection available.")
                return {'CANCELLED'}
            # --- Context Switch ---
            if initial_mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT')
            group_mode = context.scene.lsd_anchor_grouping_mode
            if group_mode == 'GROUP':
                # --- SHARED MARKER PATH ---
                ref_obj = initial_active if initial_active in targets else targets[0]
                empty = bpy.data.objects.new(f"MarkerGroup_{ref_obj.name}", None)
                empty.empty_display_type = 'PLAIN_AXES'
                # Sizing
                if context.scene.lsd_anchor_auto_size:
                    dims = ref_obj.dimensions
                    avg_dim = (dims.x + dims.y + dims.z) / 3.0
                    if avg_dim < 0.001: avg_dim = 0.1
                    empty.empty_display_size = avg_dim * 0.1
                else:
                    empty.empty_display_size = context.scene.lsd_anchor_initial_size
                context.scene.collection.objects.link(empty)
                # Placement
                if context.scene.lsd_anchor_placement_source == 'CURSOR':
                    empty.location = context.scene.cursor.location
                else:
                    # Shared center for all targets
                    all_coords = []
                    for t_name, inds in all_indices.items():
                        t = bpy.data.objects.get(t_name)
                        if t:
                            if t.type == 'MESH' and inds is not None:
                                all_coords.extend([t.matrix_world @ t.data.vertices[i].co.copy() for i in inds])
                            else:
                                all_coords.append(t.matrix_world.translation.copy())
                    if all_coords:
                        empty.location = sum(all_coords, mathutils.Vector()) / len(all_coords)
                    else:
                        empty.location = (0, 0, 0)
                empty["lsd_anchor"] = True
                empty.show_in_front = True
                created_empties.append(empty)
                created_count = 1
                # --- Parenting logic for grouped marker ---
                # We now ALWAYS parent markers so they follow their target components.
                empty.parent = ref_obj
                empty.matrix_parent_inverse = ref_obj.matrix_world.inverted()
            else:
                # --- INDIVIDUAL MARKER PATH (LEGACY) ---
                for target_name, indices in all_indices.items():
                    target = bpy.data.objects.get(target_name)
                    if not target: continue
                    empty = bpy.data.objects.new(f"Marker_{target.name}", None)
                    empty.empty_display_type = 'PLAIN_AXES'
                    if context.scene.lsd_anchor_auto_size:
                        dims = target.dimensions
                        avg_dim = (dims.x + dims.y + dims.z) / 3.0
                        if avg_dim < 0.001: avg_dim = 0.1
                        empty.empty_display_size = avg_dim * 0.1
                    else:
                        empty.empty_display_size = context.scene.lsd_anchor_initial_size
                    context.scene.collection.objects.link(empty)
                    if context.scene.lsd_anchor_placement_source == 'CURSOR':
                        empty.location = context.scene.cursor.location
                    else:
                        # Handling Mesh Vertex Center vs Empty Origin
                        if target.type == 'MESH' and indices is not None:
                            mw = target.matrix_world
                            loc = sum([mw @ target.data.vertices[i].co.copy() for i in indices], mathutils.Vector()) / len(indices)
                        else:
                            # For hook-parents, use their world origin
                            loc = target.matrix_world.translation.copy()
                        empty.location = loc
                    empty["lsd_anchor"] = True
                    empty.show_in_front = True
                    # --- Always parent individual markers ---
                    empty.parent = target
                    empty.matrix_parent_inverse = target.matrix_world.inverted()
                    created_empties.append(empty)
                    created_count += 1
        except Exception as e:
            self.report({'ERROR'}, f"Marker failure: {e}")
            return {'CANCELLED'}
        finally:
            # --- Categorical Mode Restoration (USER PRIORITY) ---
            if active_name in bpy.data.objects:
                target_obj = bpy.data.objects[active_name]
                if context.view_layer.objects.active != target_obj:
                    context.view_layer.objects.active = target_obj
                target_obj.select_set(True)
                if initial_mode and "EDIT" in initial_mode:
                    if context.mode != initial_mode:
                        try:
                            bpy.ops.object.mode_set(mode='EDIT')
                        except:
                            pass
                elif initial_mode == 'OBJECT':
                    if context.mode != 'OBJECT':
                        try:
                            bpy.ops.object.mode_set(mode='OBJECT')
                        except:
                            pass
            self.report({'INFO'}, f"[LSD v1.3.1] Markers Attached. Workspace Restored: {context.mode}")
            return {'FINISHED'}
class LSD_OT_ToggleHookPlacement(bpy.types.Operator):
    """Toggle placement mode for the hook anchor. Move the empty without deforming the mesh, then stop to rebind."""
    bl_idname = "lsd.toggle_hook_placement"
    bl_label = "Start/Stop Anchor Transform Mode"
    bl_options = {'REGISTER', 'UNDO'}
    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return context.active_object and context.active_object.type == 'EMPTY'
    def execute(self, context: bpy.types.Context) -> Set[str]:
        empty = context.active_object
        scene = context.scene
        # Toggle state
        is_starting = not scene.lsd_hook_placement_mode
        scene.lsd_hook_placement_mode = is_starting
        if is_starting:
            # Start Placement: Apply modifiers to bake deformation, allowing empty to move freely
            target_meshes = []
            hook_data = []
            for obj in context.scene.objects:
                if obj.type == 'MESH':
                    # 1. Scan for Hook Modifiers (driving the mesh)
                    for mod in obj.modifiers:
                        if mod.type == 'HOOK' and mod.object == empty:
                            target_meshes.append(('HOOK', obj, mod))
                # 2. Scan for Markers (children or vertex-parents) where 'empty' is child of 'obj'
                if empty.parent == obj:
                    target_meshes.append(('MARKER', obj, None))
            if not target_meshes:
                self.report({'WARNING'}, "This object is not an anchor for any meshes.")
                scene.lsd_hook_placement_mode = False
                return {'CANCELLED'}
            for mtype, mesh, mod in target_meshes:
                if mtype == 'HOOK':
                    # Prep Hook Re-binding
                    hook_data.append({
                        "type": "HOOK",
                        "mesh_name": mesh.name,
                        "vg_name": mod.vertex_group
                    })
                    # Bake Deformation temporarily so anchor moves freely
                    try:
                        with context.temp_override(object=mesh):
                            bpy.ops.object.modifier_apply(modifier=mod.name)
                    except: pass
                else:
                    # Prep Marker Re-parenting
                    hook_data.append({
                        "type": "MARKER",
                        "mesh_name": mesh.name,
                        "parent_type": empty.parent_type
                    })
                    # Disconnect Parenting to allow free coordinate adjustment
                    mw = empty.matrix_world.copy()
                    empty.parent = None
                    empty.matrix_world = mw
            # Store hook data on the empty
            empty["lsd_hook_data"] = hook_data
            self.report({'INFO'}, "Anchor Transform Mode Started. Target links temporarily detached.")
            return {'FINISHED'}
        else:
            # Stop Placement: Re-create Hook modifiers at new empty position
            original_active = context.view_layer.objects.active
            original_selected = context.selected_objects
            # Ensure Object Mode to start
            if context.mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT')
            bpy.ops.object.select_all(action='DESELECT')
            hook_data = empty.get("lsd_hook_data", [])
            for item in hook_data:
                mesh = bpy.data.objects.get(item["mesh_name"])
                if not mesh: continue
                if item.get("type") == "MARKER":
                    # Restore Parenting link
                    mw = empty.matrix_world.copy()
                    empty.parent = mesh
                    empty.parent_type = item.get("parent_type", 'OBJECT')
                    empty.matrix_world = mw
                    continue
                # Restore Hook Modifier link
                vg_name = item.get("vg_name")
                mod = mesh.modifiers.new(name="Hook_Restored", type='HOOK')
                mod.object = empty
                if vg_name: mod.vertex_group = vg_name
                # Re-synchronize
                context.view_layer.objects.active = mesh
                mesh.select_set(True)
                try:
                    bpy.ops.object.mode_set(mode='EDIT')
                    if vg_name and vg_name in mesh.vertex_groups:
                        mesh.vertex_groups.active_index = mesh.vertex_groups[vg_name].index
                        bpy.ops.object.vertex_group_select()
                    else:
                        bpy.ops.mesh.select_all(action='SELECT')
                    # Reset the hook to new visual position
                    bpy.ops.object.hook_reset(modifier=mod.name)
                except Exception as e:
                    self.report({'WARNING'}, f"Hook restoration failed on {mesh.name}: {str(e)}")
                finally:
                    if context.mode != 'OBJECT':
                        bpy.ops.object.mode_set(mode='OBJECT')
                mesh.select_set(False)
            # Cleanup and Restore State
            if "lsd_hook_data" in empty: del empty["lsd_hook_data"]
            # Restore original active/selected state
            if original_active and original_active.name in context.scene.objects:
                context.view_layer.objects.active = original_active
            for o in original_selected:
                if o.name in context.scene.objects:
                    o.select_set(True)
            self.report({'INFO'}, "Anchor Transform Mode Stopped. All targets re-linked.")
            return {'FINISHED'}
class LSD_OT_CleanupAnchor(bpy.types.Operator):
    bl_idname = "lsd.cleanup_anchor"
    bl_label = "Remove Selected Hook/Marker"
    bl_description = "Purge selected Hook/Marker"
    bl_options = {'REGISTER', 'UNDO'}
    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return context.selected_objects
    def execute(self, context: bpy.types.Context) -> Set[str]:
        # Implementation Note: Recursive cleanup ensures that selecting a MESH
        # will purge all of its associated LSD parametric anchors and markers.
        # Selecting an anchor Empty directly will purge it universally.
        targets = context.selected_objects
        if not targets:
            targets = [context.active_object] if context.active_object else []
        all_anchors = set()
        # Robust Identification: Matches our creation pattern
        def is_lsd_anchor(o):
            return o.get("lsd_anchor") or o.name.startswith(("Hook_", "Marker_"))
        # Phase 1: Harvesting hooks/markers from selection
        for o in targets:
            if o.type == 'EMPTY' and is_lsd_anchor(o):
                all_anchors.add(o)
            elif o.type == 'MESH':
                # Deep scan for attached Hooks
                for mod in o.modifiers:
                    if mod.type == 'HOOK' and mod.object:
                        if is_lsd_anchor(mod.object):
                            all_anchors.add(mod.object)
                # Deep scan for attached Markers (children)
                for child in o.children:
                    if child.type == 'EMPTY' and is_lsd_anchor(child):
                        # Vertex parenting is the primary indicator for LSD markers
                        if child.parent_type == 'VERTEX' or child.name.startswith("Marker_"):
                            all_anchors.add(child)
        if not all_anchors:
            self.report({'WARNING'}, "No LSD hooks or markers found in selection.")
            return {'CANCELLED'}
        cleaned_count = 0
        # Phase 2: Systematic Purge
        # Pre-collection of MESH objects to optimize universal cleanup
        scene_meshes = [obj for obj in context.scene.objects if obj.type == 'MESH']
        for empty in all_anchors:
            for obj in scene_meshes:
                # 1. Clean up Hook Modifiers
                mods_to_remove = [m for m in obj.modifiers if m.type == 'HOOK' and m.object == empty]
                for mod in mods_to_remove:
                    vg_name = mod.vertex_group
                    try:
                        obj.modifiers.remove(mod)
                        # 2. Cleanup associated Vertex Group
                        if vg_name and vg_name in obj.vertex_groups:
                            vg = obj.vertex_groups.get(vg_name)
                            if vg: obj.vertex_groups.remove(vg)
                    except Exception as e:
                        print(f"Cleanup non-critical error: {e}")
            # 3. Terminate the Empty
            try:
                bpy.data.objects.remove(empty, do_unlink=True)
                cleaned_count += 1
            except:
                pass
        self.report({'INFO'}, f"Purged {cleaned_count} LSD parametric hooks/markers.")
        return {'FINISHED'}
class LSD_OT_BakeAnchor(bpy.types.Operator):
    """Applies the hook deformation to the mesh geometry (Bake)"""
    bl_idname = "lsd.bake_anchor"
    bl_label = "Bake Anchor (Apply)"
    bl_options = {'REGISTER', 'UNDO'}
    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return context.active_object is not None
    def execute(self, context: bpy.types.Context) -> Set[str]:
        targets = context.selected_objects
        if not targets: targets = [context.active_object]
        meshes_to_process = set()
        # Identify meshes to process
        for obj in targets:
            if obj.type == 'MESH':
                meshes_to_process.add(obj)
            elif obj.type == 'EMPTY':
                for m_obj in context.scene.objects:
                    if m_obj.type == 'MESH':
                        for mod in m_obj.modifiers:
                            if mod.type == 'HOOK' and mod.object == obj:
                                meshes_to_process.add(m_obj)
        if not meshes_to_process:
            self.report({'WARNING'}, "No meshes with hooks found.")
            return {'CANCELLED'}
        processed_count = 0
        # Ensure Object Mode
        if context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        # Process each mesh
        for mesh_obj in meshes_to_process:
            mods_to_apply = []
            hooks_to_restore = []
            for mod in mesh_obj.modifiers:
                if mod.type == 'HOOK':
                    # Apply if mesh selected OR if hook is linked to selected empty
                    if mesh_obj in targets or (mod.object in targets):
                        mods_to_apply.append(mod.name)
                        # Save state to restore after baking
                        hooks_to_restore.append({
                            'name': mod.name,
                            'object': mod.object,
                            'vertex_group': mod.vertex_group,
                            'strength': mod.strength,
                            'falloff_type': mod.falloff_type,
                            'falloff_radius': mod.falloff_radius,
                            'uniform': mod.use_falloff_uniform
                        })
            if mods_to_apply:
                context.view_layer.objects.active = mesh_obj
                for mod_name in mods_to_apply:
                    try:
                        bpy.ops.object.modifier_apply(modifier=mod_name)
                        processed_count += 1
                    except Exception as e:
                        self.report({'WARNING'}, f"Failed to apply {mod_name}: {e}")
                # Restore hooks to maintain parametric control over the new rest pose
                for hook_data in reversed(hooks_to_restore):
                    try:
                        new_mod = mesh_obj.modifiers.new(name=hook_data['name'], type='HOOK')
                        new_mod.object = hook_data['object']
                        if hook_data['vertex_group']:
                            new_mod.vertex_group = hook_data['vertex_group']
                        new_mod.strength = hook_data['strength']
                        new_mod.falloff_type = hook_data['falloff_type']
                        new_mod.falloff_radius = hook_data['falloff_radius']
                        new_mod.use_falloff_uniform = hook_data['uniform']
                        # Reset (Rebind) to current state
                        bpy.ops.object.mode_set(mode='EDIT')
                        bpy.ops.mesh.select_all(action='DESELECT')
                        if hook_data['vertex_group'] and hook_data['vertex_group'] in mesh_obj.vertex_groups:
                            mesh_obj.vertex_groups.active_index = mesh_obj.vertex_groups[hook_data['vertex_group']].index
                            bpy.ops.object.vertex_group_select()
                        else:
                            bpy.ops.mesh.select_all(action='SELECT')
                        bpy.ops.object.hook_reset(modifier=new_mod.name)
                        bpy.ops.object.mode_set(mode='OBJECT')
                        # Move to top to maintain stack order logic
                        bpy.ops.object.modifier_move_to_index(modifier=new_mod.name, index=0)
                    except Exception as e:
                        self.report({'ERROR'}, f"Failed to restore hook {hook_data['name']}: {e}")
        self.report({'INFO'}, f"Baked {processed_count} hook modifiers (Pose Updated).")
        return {'FINISHED'}
def setup_dimension_gn(text_obj: bpy.types.Object, obj_a: bpy.types.Object, obj_b: bpy.types.Object, context: bpy.types.Context) -> None:
    """
    Creates a Geometry Nodes setup to dynamically display the distance between two objects as text.
    """
    gn_name = "LSD_Dynamic_Dimension_GN"
    gn_group = bpy.data.node_groups.get(gn_name)
    if not gn_group:
        gn_group = bpy.data.node_groups.new(name=gn_name, type='GeometryNodeTree')
    # AI Editor Note: Only build if missing or empty to preserve socket IDs for existing users.
    # FIX: Check for missing "Object Scale" socket to ensure compatibility with new features.
    needs_build = False
    if not gn_group.nodes:
        needs_build = True
    elif gn_group.interface:
        itree = gn_group.interface.items_tree
        if not itree.get("Object Scale") or not itree.get("Font") or not itree.get("Text Size"):
             needs_build = True
    if needs_build:
        gn_group.nodes.clear()
        gn_group.interface.clear()
        # --- Interface ---
        iface = gn_group.interface
        iface.new_socket(name="Object A", in_out="INPUT", socket_type='NodeSocketObject')
        iface.new_socket(name="Object B", in_out="INPUT", socket_type='NodeSocketObject')
        scale_sock = iface.new_socket(name="Scale", in_out="INPUT", socket_type='NodeSocketFloat')
        scale_sock.default_value = 1.0
        obj_scale_sock = iface.new_socket(name="Object Scale", in_out="INPUT", socket_type='NodeSocketFloat')
        obj_scale_sock.default_value = 1.0
        suffix_sock = iface.new_socket(name="Suffix", in_out="INPUT", socket_type='NodeSocketString')
        suffix_sock.default_value = "m"
        text_size_sock = iface.new_socket(name="Text Size", in_out="INPUT", socket_type='NodeSocketFloat')
        text_size_sock.default_value = 0.15
        shear_sock = iface.new_socket(name="Shear", in_out="INPUT", socket_type='NodeSocketFloat')
        shear_sock.default_value = 0.0
        font_sock = iface.new_socket(name="Font", in_out="INPUT", socket_type='NodeSocketFont')
        iface.new_socket(name="Geometry", in_out="OUTPUT", socket_type='NodeSocketGeometry')
        # --- Nodes ---
        nodes = gn_group.nodes
        links = gn_group.links
        g_in = nodes.new('NodeGroupInput')
        g_in.location = (-800, 0)
        g_out = nodes.new('NodeGroupOutput')
        g_out.location = (800, 0)
        # Get Locations
        info_a = nodes.new('GeometryNodeObjectInfo')
        info_a.location = (-600, 100)
        info_a.transform_space = 'RELATIVE'
        info_b = nodes.new('GeometryNodeObjectInfo')
        info_b.location = (-600, -100)
        info_b.transform_space = 'RELATIVE'
        # Calculate Distance
        dist = nodes.new('ShaderNodeVectorMath')
        dist.operation = 'DISTANCE'
        dist.location = (-400, 0)
        # --- NEW: Scale Compensation ---
        # AI Editor Note: Use explicit input for Object Scale to ensure reliability.
        # Relative distance (from Object Info) is divided by object scale.
        # We must multiply by Object Scale to get World Distance.
        math_compensate = nodes.new('ShaderNodeMath')
        math_compensate.operation = 'MULTIPLY'
        math_compensate.location = (-200, 0)
        # Apply Unit Scale
        math_scale = nodes.new('ShaderNodeMath')
        math_scale.operation = 'MULTIPLY'
        math_scale.location = (0, 0)
        # Value to String
        val_to_str = nodes.new('FunctionNodeValueToString')
        val_to_str.inputs['Decimals'].default_value = 3
        val_to_str.location = (200, 0)
        # Join Suffix
        join_str = nodes.new('GeometryNodeStringJoin')
        join_str.location = (400, 0)
        join_str.inputs['Delimiter'].default_value = " "
        # String to Curves
        str_to_curve = nodes.new('GeometryNodeStringToCurves')
        str_to_curve.location = (600, 0)
        str_to_curve.align_x = 'CENTER'
        str_to_curve.align_y = 'MIDDLE'
        links.new(g_in.outputs['Font'], str_to_curve.inputs['Font'])
        links.new(g_in.outputs['Text Size'], str_to_curve.inputs['Size'])
        links.new(g_in.outputs['Shear'], str_to_curve.inputs['Shear'])
        # Fill Curve
        fill = nodes.new('GeometryNodeFillCurve')
        fill.location = (800, 0)
        fill.mode = 'NGONS'
        # Set Material
        set_mat = nodes.new('GeometryNodeSetMaterial')
        set_mat.location = (1000, 0)
        mat = core.get_or_create_text_material(text_obj)
        set_mat.inputs['Material'].default_value = mat
        # --- Links ---
        links.new(g_in.outputs['Object A'], info_a.inputs['Object'])
        links.new(g_in.outputs['Object B'], info_b.inputs['Object'])
        links.new(info_a.outputs['Location'], dist.inputs[0])
        links.new(info_b.outputs['Location'], dist.inputs[1])
        # Link Scale Compensation
        links.new(dist.outputs['Value'], math_compensate.inputs[0])
        links.new(g_in.outputs['Object Scale'], math_compensate.inputs[1])
        links.new(math_compensate.outputs['Value'], math_scale.inputs[0])
        links.new(g_in.outputs['Scale'], math_scale.inputs[1])
        links.new(math_scale.outputs['Value'], val_to_str.inputs['Value'])
        links.new(val_to_str.outputs['String'], join_str.inputs['Strings'])
        links.new(g_in.outputs['Suffix'], join_str.inputs['Strings'])
        links.new(join_str.outputs['String'], str_to_curve.inputs['String'])
        links.new(g_in.outputs['Font'], str_to_curve.inputs['Font'])
        links.new(str_to_curve.outputs['Curve Instances'], fill.inputs['Curve'])
        links.new(fill.outputs['Mesh'], set_mat.inputs['Geometry'])
        links.new(set_mat.outputs['Geometry'], g_out.inputs['Geometry'])
    # --- Apply Modifier ---
    mod = text_obj.modifiers.new(name="Dynamic_Dimension", type='NODES')
    mod.node_group = gn_group
    # AI Editor Note: Robustly assign objects using socket identifiers
    if hasattr(gn_group, "interface"):
        for item in gn_group.interface.items_tree:
            if item.name == "Object A":
                mod[item.identifier] = obj_a
            elif item.name == "Object B":
                mod[item.identifier] = obj_b
        pass # Functions moved to generators.py and core.py
class LSD_OT_AddTextDescription(bpy.types.Operator):
    """Add a text description label to the selected element"""
    bl_idname = "lsd.add_text_description"
    bl_label = "Add Text Description"
    bl_options = {'REGISTER', 'UNDO'}
    text: bpy.props.StringProperty(name="Description", default="Label")
    @classmethod
    def poll(cls, context):
        return context.active_object is not None
    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)
    def execute(self, context):
        obj = context.active_object
        location = obj.matrix_world.translation.copy()
        parent_args = {}
        original_mode = context.mode
        if context.mode == 'EDIT_MESH' and obj.type == 'MESH':
            bm = bmesh.from_edit_mesh(obj.data)
            selected_verts = [v for v in bm.verts if v.select]
            if selected_verts:
                target_v = selected_verts[-1]
                location = obj.matrix_world @ target_v.co
                parent_args = {'parent': obj, 'parent_type': 'VERTEX', 'parent_vertices': (target_v.index, 0, 0)}
            else:
                self.report({'WARNING'}, "No vertices selected")
                return {'CANCELLED'}
            bpy.ops.object.mode_set(mode='OBJECT')
        elif context.mode == 'OBJECT':
             parent_args = {'parent': obj, 'parent_type': 'OBJECT'}
        # Create Text Object
        font_curve = bpy.data.curves.new(type="FONT", name="Description_Curve")
        font_curve.body = self.text
        font_curve.align_x = 'CENTER'
        font_curve.align_y = 'CENTER'
        text_obj = bpy.data.objects.new(name="Description_Text", object_data=font_curve)
        # Apply standard LSD font discovery for consistency
        try:
            from . import core
            f_name = getattr(context.scene, 'lsd_dim_font_name', 'DEFAULT')
            f_bold = getattr(context.scene, 'lsd_dim_font_bold', False)
            f_italic = getattr(context.scene, 'lsd_dim_font_italic', False)
            f_data = core.get_font_data(f_name, f_bold, f_italic)
            if f_data:
                font_curve.font = f_data
                # Simulation Fallback for Text Description
                f_path_low = f_data.filepath.lower()
                if f_italic and not any(p in f_path_low for p in ['it', 'ital', 'oblique', 'z']):
                    font_curve.shear = 0.3
                else:
                    font_curve.shear = 0.0
                if f_bold and not any(p in f_path_low for p in ['bd', 'bold', 'black', 'heavy', 'z']):
                    font_curve.offset = 0.002
                else:
                    font_curve.offset = 0.0
        except: pass
        coll = bpy.data.collections.get("LSD_Dimensions")
        if not coll:
            coll = bpy.data.collections.new("LSD_Dimensions")
            context.scene.collection.children.link(coll)
        coll.objects.link(text_obj)
        text_obj.location = location
        text_obj.scale = (0.2, 0.2, 0.2)
        text_obj.show_in_front = True # Always visible (X-Ray)
        # Assign Material
        mat = core.get_or_create_text_material(text_obj)
        if text_obj.data.materials:
            text_obj.data.materials[0] = mat
        else:
            text_obj.data.materials.append(mat)
        if parent_args:
            text_obj.parent = parent_args['parent']
            text_obj.parent_type = parent_args['parent_type']
            if 'parent_vertices' in parent_args:
                text_obj.parent_vertices = parent_args['parent_vertices']
                text_obj.location = (0,0,0)
        if context.scene.camera:
            c = text_obj.constraints.new('DAMPED_TRACK')
            c.target = context.scene.camera
            c.track_axis = 'TRACK_Z'
        if original_mode == 'EDIT_MESH':
             context.view_layer.objects.active = obj
             bpy.ops.object.mode_set(mode='EDIT')
        return {'FINISHED'}
class LSD_OT_Remove_Dimension(bpy.types.Operator):
    """Removes all selected dimensions and purges their associated mechatronic hooks."""
    bl_idname = "lsd.remove_dimension"
    bl_label = "Delete Selected Dimensions"
    bl_options = {'REGISTER', 'UNDO'}
    @classmethod
    def poll(cls, context):
        from . import core
        # Check if any selected object is a dimension host
        for o in context.selected_objects:
            if core.get_dimension_host(o): return True
        return core.get_dimension_host(context.active_object) is not None
    def execute(self, context):
        from . import core
        # 1. Harvest UNIQUE dimension roots from selection
        unique_roots = set()
        for o in context.selected_objects:
            host = core.get_dimension_host(o)
            if host:
                root = host.parent if host.parent else host
                unique_roots.add(root)
        if not unique_roots:
            active_host = core.get_dimension_host(context.active_object)
            if active_host:
                unique_roots.add(active_host.parent if active_host.parent else active_host)
        if not unique_roots:
            self.report({'WARNING'}, "No selected dimensions found.")
            return {'CANCELLED'}
        all_affected_meshes = set()
        # Ensure Object Mode for modifier/deletion operations
        if context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        removed_count = 0
        for root in unique_roots:
            # Safety Check: Object might have been deleted if selected WITH its parent root
            if not root or root.name not in bpy.data.objects: continue
            # Identify participants
            participants = []
            if root.get("lsd_parent_obj"): participants.append(root["lsd_parent_obj"])
            if root.get("lsd_slave_obj"):  participants.append(root["lsd_slave_obj"])
            if root.get("lsd_hook_start"): participants.append(root["lsd_hook_start"])
            if root.get("lsd_hook_end"):   participants.append(root["lsd_hook_end"])
            # --- PHASE 1: BAKE & DETACH ---
            to_delete = {root}
            for hook_empty in participants:
                if not (hook_empty and hook_empty.name in bpy.data.objects): continue
                
                # If it's an offset hook, delete the entire offset line assembly
                if hook_empty.get("lsd_is_offset_hook"):
                    offset_root = hook_empty if "StartHook" in hook_empty.name else hook_empty.parent
                    if offset_root:
                        to_delete.add(offset_root)
                        for child in offset_root.children:
                            to_delete.add(child)
                            for grandchild in child.children:
                                to_delete.add(grandchild)
                    # Bake any mesh modifiers pointing to this offset hook since the hook will be deleted
                    for scene_obj in bpy.data.objects:
                        if scene_obj.type != 'MESH': continue
                        for mod in scene_obj.modifiers:
                            if mod.type == 'HOOK' and mod.object == hook_empty:
                                all_affected_meshes.add(scene_obj)
                                try:
                                    context.view_layer.objects.active = scene_obj
                                    scene_obj.select_set(True)
                                    bpy.ops.object.modifier_apply(modifier=mod.name)
                                except: pass
                                finally:
                                    scene_obj.select_set(False)
                # If it's an auto-generated internal dimension hook (not user-created),
                # mark for deletion and bake any mesh modifiers pointing to it.
                # NOTE: This MUST run before the generic lsd_anchor check, because auto-generated
                # dimension hooks also have the lsd_anchor property set to True.
                elif hook_empty.name.startswith("Hook_Dim"):
                    to_delete.add(hook_empty)
                    # Freeze the hook's location before baking to guarantee stability
                    frozen_world_pos = hook_empty.matrix_world.translation.copy()
                    for con in list(hook_empty.constraints):
                        hook_empty.constraints.remove(con)
                    if hook_empty.parent:
                        hook_empty.location = hook_empty.parent.matrix_world.inverted() @ frozen_world_pos
                    else:
                        hook_empty.location = frozen_world_pos
                    context.view_layer.update()
                    
                    # Bake mesh modifiers pointing to this auto-generated hook since it will be deleted
                    for scene_obj in bpy.data.objects:
                        if scene_obj.type != 'MESH': continue
                        for mod in scene_obj.modifiers:
                            if mod.type == 'HOOK' and mod.object == hook_empty:
                                all_affected_meshes.add(scene_obj)
                                try:
                                    context.view_layer.objects.active = scene_obj
                                    scene_obj.select_set(True)
                                    bpy.ops.object.modifier_apply(modifier=mod.name)
                                except:
                                    pass
                                finally:
                                    if mod.name in scene_obj.modifiers:
                                        # Bake failed (e.g., multi-user mesh or shape keys). Preserve the empty to prevent retraction.
                                        to_delete.discard(hook_empty)
                                        # Re-tag it as a manual anchor so it behaves nicely in the future
                                        hook_empty["lsd_anchor"] = True
                                    scene_obj.select_set(False)
                # If it's a user-created "Attach Hook" (lsd_anchor=True) OR a generic Empty used as an anchor,
                # detach it but DO NOT delete it and DO NOT bake its modifier. The hook empty survives and
                # its hook modifier on the mesh must remain intact to preserve the mesh deformation.
                # Only clean up the dimension-owned constraints and back-pointer properties.
                elif hook_empty.get("lsd_anchor") or hook_empty.get("lsd_is_dimension_hook"):
                    # Remove only the COPY_LOCATION constraints this dimension injected.
                    # Identify them by their target being an lsd_is_dimension_anchor=="MASTER"
                    # empty whose parent is this specific root.
                    cons_to_remove = []
                    for con in hook_empty.constraints:
                        if con.type == 'COPY_LOCATION':
                            if con.target and con.target.get("lsd_is_dimension_anchor") == "MASTER":
                                if con.target.parent == root:
                                    cons_to_remove.append(con)
                    if cons_to_remove:
                        # CRITICAL: Freeze the hook's current constraint-evaluated world position
                        # BEFORE removing the constraints. The COPY_LOCATION override completely
                        # replaces the hook's own location, so removing it would snap the empty
                        # back to its pre-constraint position, causing the mesh to retract.
                        # We capture the evaluated world translation and bake it into location.
                        frozen_world_pos = hook_empty.matrix_world.translation.copy()
                        for con in cons_to_remove:
                            hook_empty.constraints.remove(con)
                        # Apply the frozen world position as the hook's new local location,
                        # accounting for any parent transform on the hook.
                        if hook_empty.parent:
                            hook_empty.location = hook_empty.parent.matrix_world.inverted() @ frozen_world_pos
                        else:
                            hook_empty.location = frozen_world_pos
                    # Clean up back-pointer properties this dimension placed on the hook
                    if hook_empty.get("lsd_is_dimension_hook") is not None:
                        del hook_empty["lsd_is_dimension_hook"]
                    if hook_empty.get("lsd_dim_root") == root:
                        del hook_empty["lsd_dim_root"]
            # --- PHASE 2: PURGE ASSEMBLY ---
            for child in root.children:
                if child: to_delete.add(child)
                for grandchild in child.children:
                    if grandchild: to_delete.add(grandchild)
                    
            # --- PHASE 2.5: AGGRESSIVE ORPHAN CLEANUP ---
            # To fix scenes corrupted by the previous deletion bug, we proactively hunt down
            # any 'Hook_Dim' empties that have no parent and no dimension driving them.
            for obj in bpy.data.objects:
                if obj.name.startswith("Hook_Dim_Obj") and obj.parent is None:
                    is_driven = False
                    for con in obj.constraints:
                        if con.type == 'COPY_LOCATION' and con.target and con.target.get("lsd_is_dimension_anchor") == "MASTER":
                            is_driven = True
                            break
                    if not is_driven:
                        to_delete.add(obj)
                        # Bake any modifiers pointing to this orphaned hook
                        for scene_obj in bpy.data.objects:
                            if scene_obj.type != 'MESH': continue
                            for mod in scene_obj.modifiers:
                                if mod.type == 'HOOK' and mod.object == obj:
                                    try:
                                        context.view_layer.objects.active = scene_obj
                                        scene_obj.select_set(True)
                                        bpy.ops.object.modifier_apply(modifier=mod.name)
                                        scene_obj.select_set(False)
                                    except: pass

            # Unparent orphans with Keep Transform
            for p_obj in to_delete:
                if not p_obj or p_obj.name not in bpy.data.objects: continue
                for victim in bpy.data.objects:
                    if victim and victim.parent == p_obj:
                        mw = victim.matrix_world.copy()
                        victim.parent = None
                        victim.matrix_world = mw
            for obj in list(to_delete):
                if obj and obj.name in bpy.data.objects:
                    data = obj.data
                    bpy.data.objects.remove(obj, do_unlink=True)
                    if data and data.users == 0:
                        try:
                            if isinstance(data, bpy.types.Mesh): bpy.data.meshes.remove(data)
                            elif isinstance(data, bpy.types.Curve): bpy.data.curves.remove(data)
                        except: pass
            removed_count += 1
        # --- PHASE 3: FINAL ANCHOR CLEANUP ---
        # Targeted cleanup: only remove dangling hook modifiers on affected meshes whose
        # hook target object was deleted (no longer exists in bpy.data.objects).
        # We do NOT call the global cleanup_anchor operator here because it would
        # indiscriminately remove all hooks from those meshes, including user-created ones.
        if all_affected_meshes:
            original_sel = list(context.selected_objects)
            original_act = context.view_layer.objects.active
            for m in all_affected_meshes:
                if not m or m.name not in bpy.data.objects: continue
                dangling_mods = [
                    mod for mod in m.modifiers
                    if mod.type == 'HOOK' and (mod.object is None or mod.object.name not in bpy.data.objects)
                ]
                if dangling_mods:
                    context.view_layer.objects.active = m
                    m.select_set(True)
                    for mod in dangling_mods:
                        vg_name = mod.vertex_group
                        try:
                            m.modifiers.remove(mod)
                            if vg_name and vg_name in m.vertex_groups:
                                vg = m.vertex_groups.get(vg_name)
                                if vg: m.vertex_groups.remove(vg)
                        except: pass
                    m.select_set(False)
            # Restore selection (minus what was deleted)
            for o in original_sel:
                if o and o.name in bpy.data.objects: o.select_set(True)
            if original_act and original_act.name in bpy.data.objects:
                context.view_layer.objects.active = original_act
        self.report({'INFO'}, f"Purged {removed_count} Dimension(s) and associated mechatronic links.")
        return {'FINISHED'}
class LSD_OT_Manually_Draw_Offset_Line(bpy.types.Operator):
    """Draw a dimension offset line manually by clicking in the viewport"""
    bl_idname = "lsd.manually_draw_offset_line"
    bl_label = "Manually Draw Offset Line"
    bl_options = {'REGISTER', 'UNDO'}

    use_snapping: bpy.props.BoolProperty(default=False)

    @classmethod
    def poll(cls, context):
        return context.mode in {'OBJECT', 'EDIT_MESH'}

    def invoke(self, context, event):
        if context.space_data.type != 'VIEW_3D':
            self.report({'WARNING'}, "Active space must be a View3d")
            return {'CANCELLED'}

        self.obj1 = context.active_object
        if not self.obj1:
            self.report({'WARNING'}, "Please select an object, mesh, or hook first.")
            return {'CANCELLED'}

        self.initial_mode = context.mode

        if self.initial_mode == 'EDIT_MESH' and self.obj1.type == 'MESH':
            import bmesh
            import mathutils
            bm = bmesh.from_edit_mesh(self.obj1.data)
            bm.verts.ensure_lookup_table()
            sel_verts = [v for v in bm.verts if v.select]
            if not sel_verts:
                self.report({'WARNING'}, "No vertices selected.")
                return {'CANCELLED'}
            pts = [self.obj1.matrix_world @ v.co.copy() for v in sel_verts]
            center = sum(pts, mathutils.Vector()) / len(pts)
            self.p1 = center
            
            bpy.ops.object.mode_set(mode='OBJECT')
            self.hook1 = bpy.data.objects.new(f"{self.obj1.name}_StartHook", None)
            context.collection.objects.link(self.hook1)
            self.hook1.empty_display_type = 'PLAIN_AXES'
            self.hook1.empty_display_size = 0.5
            self.hook1.location = self.p1
            self.hook1["lsd_is_offset_hook"] = "START"
            
            mod = self.obj1.modifiers.new(name="Offset_Hook", type='HOOK')
            mod.object = self.hook1
            
            context.view_layer.objects.active = self.obj1
            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.object.hook_assign(modifier="Offset_Hook")
            bpy.ops.object.mode_set(mode='OBJECT')
        else:
            self.p1 = self.obj1.matrix_world.translation.copy()
            
            if self.obj1.type == 'EMPTY' and "anchor" in self.obj1.name.lower() or "hook" in self.obj1.name.lower():
                self.hook1 = self.obj1
            else:
                self.hook1 = bpy.data.objects.new(f"{self.obj1.name}_StartHook", None)
                context.collection.objects.link(self.hook1)
                self.hook1.empty_display_type = 'PLAIN_AXES'
                self.hook1.empty_display_size = 0.5
                self.hook1.location = self.p1
                self.hook1["lsd_is_offset_hook"] = "START"
                
                # Parent the selected object to the newly created hook so the hook acts as a handle
                bpy.ops.object.select_all(action='DESELECT')
                self.obj1.select_set(True)
                self.hook1.select_set(True)
                context.view_layer.objects.active = self.hook1
                bpy.ops.object.parent_set(type='OBJECT', keep_transform=True)

        self.use_snapping = False
        context.workspace.status_text_set("Draw Offset: Click to place SECOND point (Depth 0 Viewplane) | [B] Toggle Snapping | [ESC] Cancel")
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type in {'RIGHTMOUSE', 'ESC'}:
            context.workspace.status_text_set(None)
            return {'CANCELLED'}

        if event.type == 'B' and event.value == 'PRESS':
            self.use_snapping = not self.use_snapping
            if self.use_snapping:
                context.workspace.status_text_set("Draw Offset: Click to place SECOND point (SNAPPING ENABLED) | [B] Toggle Snapping | [ESC] Cancel")
            else:
                context.workspace.status_text_set("Draw Offset: Click to place SECOND point (Depth 0 Viewplane) | [B] Toggle Snapping | [ESC] Cancel")
            return {'RUNNING_MODAL'}

        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            import mathutils
            from bpy_extras import view3d_utils
            region = context.region
            rv3d = context.region_data
            coord = event.mouse_region_x, event.mouse_region_y

            if self.use_snapping:
                view_vector = view3d_utils.region_2d_to_vector_3d(region, rv3d, coord)
                ray_origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, coord)
                
                res = context.scene.ray_cast(context.view_layer.depsgraph, ray_origin, view_vector)
                result, location, normal, index, obj, matrix = res
                
                if result:
                    if obj.type == 'MESH':
                        depsgraph = context.evaluated_depsgraph_get()
                        eval_obj = obj.evaluated_get(depsgraph)
                        mesh = eval_obj.to_mesh()
                        
                        closest_v = None
                        min_dist = float('inf')
                        for v in mesh.vertices:
                            v_world = obj.matrix_world @ v.co
                            dist = (v_world - location).length
                            if dist < min_dist:
                                min_dist = dist
                                closest_v = v_world
                                
                        closest_f = None
                        min_dist_f = float('inf')
                        for f in mesh.polygons:
                            f_world = obj.matrix_world @ f.center
                            dist = (f_world - location).length
                            if dist < min_dist_f:
                                min_dist_f = dist
                                closest_f = f_world
                                
                        if closest_v and closest_f:
                            if min_dist < min_dist_f:
                                self.p2 = closest_v
                            else:
                                self.p2 = closest_f
                        else:
                            self.p2 = location
                        eval_obj.to_mesh_clear()
                    else:
                        self.p2 = location
                else:
                    self.report({'WARNING'}, "No geometry hit! Disabling snapping or click on geometry.")
                    return {'RUNNING_MODAL'}
            else:
                depth_location = context.scene.cursor.location
                self.p2 = view3d_utils.region_2d_to_location_3d(region, rv3d, coord, depth_location)

            self.hook2 = bpy.data.objects.new("Offset_EndHook", None)
            context.collection.objects.link(self.hook2)
            self.hook2.empty_display_type = 'PLAIN_AXES'
            self.hook2.empty_display_size = 0.5
            self.hook2.location = self.p2
            self.hook2["lsd_is_offset_hook"] = "END"

            # The first hook should always be parented to the second hook
            bpy.ops.object.select_all(action='DESELECT')
            self.hook1.select_set(True)
            self.hook2.select_set(True)
            context.view_layer.objects.active = self.hook2
            bpy.ops.object.parent_set(type='OBJECT', keep_transform=True)

            from . import generators
            line_mesh = generators.create_dimension_line_mesh("Standalone_Offset")
            line_obj = bpy.data.objects.new("Standalone_Offset_Line", line_mesh)
            context.collection.objects.link(line_obj)
            line_obj.parent = self.hook1
            line_obj.location = (0, 0, 0)
            
            line_obj.lsd_is_standalone_offset = True
            line_obj.scale.z = (self.p2 - self.p1).length
            
            from . import core
            line_mat = core.get_or_create_line_material(line_obj)
            line_obj.active_material = line_mat
            line_obj.color = line_mat.diffuse_color
            
            con_track = line_obj.constraints.new('TRACK_TO')
            con_track.target = self.hook2
            con_track.track_axis = 'TRACK_Z'
            con_track.up_axis = 'UP_Y'
            
            bpy.ops.object.select_all(action='DESELECT')
            context.view_layer.objects.active = line_obj
            line_obj.select_set(True)

            if self.initial_mode == 'EDIT_MESH':
                bpy.ops.object.mode_set(mode='EDIT')

            context.workspace.status_text_set(None)
            self.report({'INFO'}, "Standalone Offset Line Generated!")
            return {'FINISHED'}

        return {'RUNNING_MODAL'}
class LSD_OT_Add_Dimension(bpy.types.Operator):
    """
    Generate a parametric dimension between two points.
    In Object Mode: measures between bounding-box centers of selected objects.
    In Edit Mode: attaches a Parametric Anchor hook to each selection group's center,
    then generates the dimension between those hooks in Object Mode.
    """
    bl_idname = "lsd.add_dimension"
    bl_label = "Generate Dimension"
    bl_description = "First Selected is Static Point, Second Selected is Dynamic Point"
    bl_options = {'REGISTER', 'UNDO'}
    @classmethod
    def poll(cls, context):
        return context.active_object is not None
    def execute(self, context):
        from . import generators
        initial_mode = context.mode
        initial_active = context.active_object
        scene = context.scene
        dim_was_generated = False  # Flag to skip workspace restoration if a new dimension was spawned
        try:
            # ======================================================================
            # PRE-GENERATION ORPHAN CLEANUP
            # To handle cases where users manually deleted previous dimensions (leaving residual hooks),
            # proactively bake and purge any orphaned parametric hooks in the scene.
            # ======================================================================
            scene_meshes = [obj for obj in context.scene.objects if obj.type == 'MESH']
            orphaned_hooks = []
            for obj in context.scene.objects:
                if obj.type == 'EMPTY' and obj.name.startswith("Hook_Dim_"):
                    # Check if it is actively driven by a dimension
                    is_driven = False
                    for con in obj.constraints:
                        if con.type == 'COPY_LOCATION' and con.target and con.target.get("lsd_is_dimension_anchor") == "MASTER":
                            is_driven = True
                            break
                    if not is_driven:
                        orphaned_hooks.append(obj)
            
            if orphaned_hooks:
                for empty in orphaned_hooks:
                    for m_obj in scene_meshes:
                        mods_to_remove = [m for m in m_obj.modifiers if m.type == 'HOOK' and m.object == empty]
                        for mod in mods_to_remove:
                            vg_name = mod.vertex_group
                            try:
                                m_obj.modifiers.remove(mod)
                                if vg_name and vg_name in m_obj.vertex_groups:
                                    vg = m_obj.vertex_groups.get(vg_name)
                                    if vg: m_obj.vertex_groups.remove(vg)
                            except: pass
                    try:
                        bpy.data.objects.remove(empty, do_unlink=True)
                    except: pass

            # ======================================================================
            # EDIT MODE PATH — Hook-first, then dimension
            # ======================================================================
            if initial_mode == 'EDIT_MESH':
                import bmesh
                # --- Step 1: Capture selection in Edit Mode ---
                depsgraph = context.evaluated_depsgraph_get()
                per_obj_data = [] # (mesh_obj, [vert_indices])
                for obj in context.objects_in_mode:
                    if obj.type != 'MESH': continue
                    bm = bmesh.from_edit_mesh(obj.data)
                    bm.verts.ensure_lookup_table()
                    sel_verts = [v for v in bm.verts if v.select]
                    if not sel_verts: continue
                    
                    indices = [v.index for v in sel_verts]
                    per_obj_data.append((obj, indices))
                    
                if not per_obj_data:
                    self.report({'WARNING'}, "No vertices selected.")
                    return {'CANCELLED'}
                    
                anchor_spec_indices = [] # (mesh_obj, vert_indices_1, vert_indices_2)
                
                if len(per_obj_data) >= 2:
                    # Multiple objects: pick two for the dimension
                    o2c = initial_active
                    d2 = next((d for d in per_obj_data if d[0] == o2c), per_obj_data[-1])
                    d1 = next((d for d in per_obj_data if d != d2), per_obj_data[0])
                    anchor_spec_indices.append((d1[0], d1[1], None))
                    anchor_spec_indices.append((d2[0], d2[1], None))
                else:
                    # Single object: pick two elements or vertices
                    obj0, indices0 = per_obj_data[0]
                    bm0 = bmesh.from_edit_mesh(obj0.data)
                    bm0.verts.ensure_lookup_table()
                    bm0.edges.ensure_lookup_table()
                    bm0.faces.ensure_lookup_table()
                    
                    islands = []
                    mesh_select_mode = context.tool_settings.mesh_select_mode
                    if mesh_select_mode[2]:  # Face select
                        for face in bm0.faces:
                            if face.select: islands.append([v.index for v in face.verts])
                    else:  # Vertex or Edge select (group by connected components)
                        selected_verts = [v for v in bm0.verts if v.select]
                        visited = set()
                        for v in selected_verts:
                            if v in visited: continue
                            island = []
                            queue = [v]
                            visited.add(v)
                            while queue:
                                curr = queue.pop(0)
                                island.append(curr.index)
                                for edge in curr.link_edges:
                                    other_v = edge.other_vert(curr)
                                    if other_v.select and other_v not in visited:
                                        visited.add(other_v)
                                        queue.append(other_v)
                            islands.append(island)
                            
                    if len(islands) >= 2:
                        active_island = None
                        active_elem = bm0.select_history.active
                        if active_elem and active_elem.select:
                            active_verts = set()
                            if isinstance(active_elem, bmesh.types.BMFace):
                                active_verts = set(v.index for v in active_elem.verts)
                            elif isinstance(active_elem, bmesh.types.BMEdge):
                                active_verts = set(v.index for v in active_elem.verts)
                            elif isinstance(active_elem, bmesh.types.BMVert):
                                active_verts = {active_elem.index}
                                
                            for isl in islands:
                                if active_verts.intersection(isl):
                                    active_island = isl
                                    break
                                    
                        if not active_island:
                            active_island = islands[-1]
                            
                        # Estimate furthest using base coords for now since it's just relative distance
                        furthest_island = None
                        max_dist = -1
                        def base_center(idx_list):
                            pts = [bm0.verts[i].co for i in idx_list]
                            return sum(pts, mathutils.Vector()) / len(pts)
                            
                        c_active_base = base_center(active_island)
                        for isl in islands:
                            if isl == active_island: continue
                            dist = (base_center(isl) - c_active_base).length
                            if dist > max_dist:
                                max_dist = dist
                                furthest_island = isl
                        if furthest_island is None:
                            furthest_island = islands[0]
                            
                        anchor_spec_indices.append((obj0, furthest_island, active_island))
                    else:
                        # Fallback: only 1 island (e.g. 1 face). Bridge the entire island to the 3D cursor.
                        active_island = islands[0] if islands else []
                        if active_island:
                            anchor_spec_indices.append((obj0, active_island, "CURSOR"))
                        else:
                            self.report({'WARNING'}, "Select at least 1 element.")
                            return {'CANCELLED'}

                # --- STEP 1.5: Workspace Transition (MANDATORY for creation) ---
                # Clear edit mode selection so subsequent dimension generation doesn't accidentally bridge to old selections
                if context.mode != 'OBJECT':
                    for obj in context.objects_in_mode:
                        if obj.type == 'MESH':
                            bm = bmesh.from_edit_mesh(obj.data)
                            for v in bm.verts: v.select = False
                            for e in bm.edges: e.select = False
                            for f in bm.faces: f.select = False
                            bm.select_flush(False)
                            bmesh.update_edit_mesh(obj.data)
                    bpy.ops.object.mode_set(mode='OBJECT')
                # In Edit Mode, evaluated meshes are often undeformed. Now that we are in Object Mode,
                # the evaluated mesh contains the true displaced coordinates.
                depsgraph = context.evaluated_depsgraph_get()
                context.view_layer.update()
                
                anchor_spec = [] # (mesh_obj, [vert_indices], world_point)
                
                for spec in anchor_spec_indices:
                    obj = spec[0]
                    eval_obj = obj.evaluated_get(depsgraph)
                    eval_mesh = eval_obj.to_mesh()
                    
                    if spec[2] is None:
                        # Multiple objects case
                        pts = [obj.matrix_world @ eval_mesh.vertices[i].co.copy() for i in spec[1]]
                        center = sum(pts, mathutils.Vector()) / len(pts)
                        anchor_spec.append((obj, spec[1], center))
                    elif spec[2] == "CURSOR":
                        # Single-island fallback: bridge to 3D cursor
                        pts1 = [obj.matrix_world @ eval_mesh.vertices[i].co.copy() for i in spec[1]]
                        c1 = sum(pts1, mathutils.Vector()) / len(pts1)
                        anchor_spec.append((obj, spec[1], c1))
                        
                        c2 = context.scene.cursor.location.copy()
                        anchor_spec.append((obj, [], c2))
                    else:
                        # Single object case (two islands/vertices)
                        pts1 = [obj.matrix_world @ eval_mesh.vertices[i].co.copy() for i in spec[1]]
                        c1 = sum(pts1, mathutils.Vector()) / len(pts1)
                        anchor_spec.append((obj, spec[1], c1))
                        
                        pts2 = [obj.matrix_world @ eval_mesh.vertices[i].co.copy() for i in spec[2]]
                        c2 = sum(pts2, mathutils.Vector()) / len(pts2)
                        anchor_spec.append((obj, spec[2], c2))
                        
                    eval_obj.to_mesh_clear()

                # --- Step 2: Process anchors (Consolidated Workflow) ---
                # We use the same helper as the standard Attach Hook feature
                # to guarantee consistency and non-warping behavior.
                hook_empties = []
                for mesh_obj, vert_indices, world_point in anchor_spec:
                    empty = create_hook_anchor(context, mesh_obj, vert_indices, name_prefix="Hook_Dim", display_type='PLAIN_AXES', force_location=world_point)
                    if empty:
                        empty.hide_viewport = True
                        empty.hide_render = True
                        hook_empties.append((empty, world_point))
                if len(hook_empties) < 2:
                    self.report({'ERROR'}, "Failed to create anchor hooks.")
                    return {'CANCELLED'}
                # --- Step 3: Generate Dimension ---
                hook_a_obj, p1_v = hook_empties[0]
                hook_b_obj, p2_v = hook_empties[1]
                # CAST TO CONCRETE TUPLES TO AVOID MATRIX POINTERS
                p1 = (p1_v.x, p1_v.y, p1_v.z)
                p2 = (p2_v.x, p2_v.y, p2_v.z)
                # Dim creation must happen in Object Mode
                if context.mode != 'OBJECT':
                    bpy.ops.object.mode_set(mode='OBJECT')
                bpy.ops.object.select_all(action='DESELECT')
                dim_result = generators.generate_smart_dimension_parametric(
                    context, p1, p2,
                    parent_a=(hook_a_obj, 'OBJECT', 0),
                    parent_b=(hook_b_obj, 'OBJECT', 0))
                if dim_result:
                    def _deferred_select_edit():
                        try:
                            if dim_result.name in bpy.data.objects:
                                bpy.ops.object.select_all(action='DESELECT')
                                dim_result.select_set(True)
                                bpy.context.view_layer.objects.active = dim_result
                                bpy.context.view_layer.update()
                                for area in bpy.context.screen.areas:
                                    if area.type in ['VIEW_3D', 'PROPERTIES']:
                                        area.tag_redraw()
                        except: pass
                        return None
                    bpy.app.timers.register(_deferred_select_edit, first_interval=0.2)
                    dim_was_generated = True
                return {'FINISHED'}
            # ======================================================================
            # OBJECT MODE PATH — measure between centers of two objects
            # ======================================================================
            p1, p2 = None, None
            parent_a, parent_b = (None, 'OBJECT', 0), (None, 'OBJECT', 0)
            sel = context.selected_objects
            # Filter: Exclude dimension internals (text/arrows/anchors), but INCLUDE parametric hooks (lsd_anchor)
            valid_sel = [o for o in sel if not o.get("lsd_is_dimension") and not o.get("lsd_is_dimension_anchor")]
            if len(valid_sel) >= 2:
                # Identification: Active is the dynamic end (p2), Other is start (p1)
                o2 = context.active_object if context.active_object in valid_sel else valid_sel[-1]
                o1 = next((o for o in valid_sel if o != o2), valid_sel[0])
                # --- Pre-calculate Definitive World Coordinates ---
                # We do this BEFORE any parenting or hook creation to bypass matrix-update lag.
                context.view_layer.update()
                # CAST TO CONCRETE TUPLES TO AVOID MATRIX POINTERS
                p1_v = o1.matrix_world.translation.copy()
                p2_v = o2.matrix_world.translation.copy()
                p1 = (p1_v.x, p1_v.y, p1_v.z)
                p2 = (p2_v.x, p2_v.y, p2_v.z)
                # --- Attachment Phase (Stable Anchor Identification or Generation) ---
                # Strategy: If the object is ALREADY a hook, use it.
                # If the object is a MESH, search for existing LSD hook modifiers to reuse.
                # Otherwise, generate a fresh hook anchor.
                h1, h2 = None, None
                def resolve_anchor(obj, measured_p):
                    # 1. Direct pass-through if the object is already a hook/anchor or EMPTY
                    if obj.type == 'EMPTY' or obj.get("lsd_anchor") or obj.get("lsd_is_dimension_hook") or obj.get("lsd_is_offset_hook"):
                        return obj
                    # 1.5 Check if the object is parented to an offset line hook
                    if getattr(obj, "lsd_is_standalone_offset", False):
                        hook1 = obj.parent
                        track_con = next((c for c in obj.constraints if c.type == 'TRACK_TO'), None)
                        hook2 = track_con.target if track_con else None
                        if hook1 and hook2:
                            d1 = (hook1.matrix_world.translation - measured_p).length
                            d2 = (hook2.matrix_world.translation - measured_p).length
                            return hook1 if d1 < d2 else hook2
                        elif hook1:
                            return hook1
                    
                    if obj.parent and (obj.parent.get("lsd_is_offset_hook") or "StartHook" in obj.parent.name or "Offset_EndHook" in obj.parent.name):
                        return obj.parent
                    # 2. Search for existing LSD-controlled hook modifier on mesh objects
                    if obj.type == 'MESH':
                        for mod in obj.modifiers:
                            if mod.type == 'HOOK' and mod.object:
                                target_o = mod.object
                                if target_o.get("lsd_anchor") or target_o.get("lsd_is_dimension_hook"):
                                    # Re-use existing stable anchor
                                    return target_o
                        # 3. Fallback: Create a fresh hook anchor at the measured position
                        empty = create_hook_anchor(context, obj, [v.index for v in obj.data.vertices],
                                                 name_prefix="Hook_Dim_Obj", display_type='PLAIN_AXES', force_location=measured_p)
                        if empty:
                            empty.hide_viewport = True
                            empty.hide_render = True
                        return empty
                    return None
                h1 = resolve_anchor(o1, p1_v)
                h2 = resolve_anchor(o2, p2_v)
                if h1 and h2:
                    parent_a = (h1, 'OBJECT', 0)
                    parent_b = (h2, 'OBJECT', 0)
            if p1 is not None and p2 is not None:
                bpy.ops.object.select_all(action='DESELECT')
                dim_result = generators.generate_smart_dimension_parametric(context, p1, p2, parent_a=parent_a, parent_b=parent_b)
                if dim_result:
                    def _deferred_select_obj():
                        try:
                            if dim_result.name in bpy.data.objects:
                                bpy.ops.object.select_all(action='DESELECT')
                                dim_result.select_set(True)
                                bpy.context.view_layer.objects.active = dim_result
                                bpy.context.view_layer.update()
                                for area in bpy.context.screen.areas:
                                    if area.type in ['VIEW_3D', 'PROPERTIES']:
                                        area.tag_redraw()
                        except: pass
                        return None
                    bpy.app.timers.register(_deferred_select_obj, first_interval=0.2)
                    dim_was_generated = True
                return {'FINISHED'}
            # --- FALLBACK: Bounding Box on single active mesh ---
            obj = initial_active
            if not obj or obj.type != 'MESH':
                self.report({'WARNING'}, "Select two objects or a mesh for BBox measurement.")
                return {'CANCELLED'}
            axis_pref = getattr(scene, 'lsd_dim_axis', 'ALL')
            axes_to_gen = ['X', 'Y', 'Z'] if axis_pref == 'ALL' else [axis_pref]
            offset_val = getattr(scene, 'lsd_dim_offset', 0.1)
            bb_verts = [obj.matrix_world @ mathutils.Vector(c) for c in obj.bound_box]
            xs = [v.x for v in bb_verts]; ys = [v.y for v in bb_verts]; zs = [v.z for v in bb_verts]
            min_x, max_x = min(xs), max(xs)
            min_y, max_y = min(ys), max(ys)
            min_z, max_z = min(zs), max(zs)
            mid_x, mid_y = (min_x + max_x) / 2, (min_y + max_y) / 2
            # Collect all generated dims to handle selection focus
            new_dims = []
            for ax in axes_to_gen:
                if ax == 'X':
                    p1 = mathutils.Vector((min_x, mid_y, max_z + offset_val))
                    p2 = mathutils.Vector((max_x, mid_y, max_z + offset_val))
                elif ax == 'Y':
                    p1 = mathutils.Vector((mid_x, min_y, max_z + offset_val))
                    p2 = mathutils.Vector((mid_x, max_y, max_z + offset_val))
                else:
                    p1 = mathutils.Vector((max_x + offset_val, mid_y, min_z))
                    p2 = mathutils.Vector((max_x + offset_val, mid_y, max_z))
                # DO NOT pass parent_a=(obj,...) to the generator because it will add a COPY_LOCATION
                # constraint to the mesh itself, forcefully snapping the mesh to the bounding box corner!
                dim = generators.generate_smart_dimension_parametric(context, p1, p2, name=f"BBox_{ax}")
                if dim: 
                    new_dims.append(dim)
                    # Instead, safely parent the dimension's Root directly to the object so it travels with it.
                    dim_root = dim.parent
                    if dim_root:
                        old_mat = dim_root.matrix_world.copy()
                        dim_root.parent = obj
                        dim_root.matrix_world = old_mat
            if new_dims:
                # Select the last generated dimension (host label) so properties appear instantly
                last_host = new_dims[-1]
                if last_host:
                    # Defer selection slightly to ensure Blender panel updates properly
                    def deferred_select():
                        try:
                            # Re-verify existence in case of undo/cancel
                            if last_host.name in bpy.data.objects:
                                bpy.ops.object.select_all(action='DESELECT')
                                last_host.select_set(True)
                                bpy.context.view_layer.objects.active = last_host
                                # Force redraw of all GUI components
                                for area in bpy.context.screen.areas:
                                     if area.type in ['VIEW_3D', 'PROPERTIES']:
                                          area.tag_redraw()
                                # CRITICAL: Workspace Update
                                bpy.context.view_layer.update()
                        except: pass
                        return None
                    bpy.app.timers.register(deferred_select, first_interval=0.2)
                # Signal to the finally block to skip workspace restoration
                dim_was_generated = True
            return {'FINISHED'}
        except Exception as e:
            self.report({'ERROR'}, f"Dimension failure: {e}")
            return {'CANCELLED'}
        finally:
            # --- Categorical Workspace Enforcement (LSD v1.0.6) ---
            try:
                if not dim_was_generated and initial_active and initial_active.name in bpy.data.objects:
                    target_obj = bpy.data.objects[initial_active.name]
                    if context.view_layer.objects.active != target_obj:
                        context.view_layer.objects.active = target_obj
                    target_obj.select_set(True)
                    # --- Anti-Warping Normalization (LSD v1.4.1) ---
                    # Use a stable, mathematically correct matrix-inverse to lock the rest state
                    try:
                        for mod in target_obj.modifiers:
                             if mod.type == 'HOOK' and mod.object:
                                  # Correct normalization: Δ = Empty^-1 * Object
                                  mod.matrix_inverse = mod.object.matrix_world.inverted() @ target_obj.matrix_world
                                  mod.center = (0, 0, 0)
                    except:
                        pass
                    if initial_mode and "EDIT" in initial_mode:
                        # AI Editor Note: Logic Change - Context Switching.
                        # If a dimension was created, stay in OBJECT mode to show properties.
                        if not new_dims:
                            if context.mode != initial_mode:
                                try:
                                    bpy.ops.object.mode_set(mode='EDIT')
                                except:
                                    pass
                    elif initial_mode == 'OBJECT':
                        if context.mode != 'OBJECT':
                            try:
                                bpy.ops.object.mode_set(mode='OBJECT')
                            except:
                                pass
            except:
                pass
            self.report({'INFO'}, f"[LSD v1.5.1] Dimension Process Complete. Workspace Sync: {context.mode}")
class LSD_OT_AddModifier(bpy.types.Operator):
    bl_idname = "lsd.add_parametric_mod"
    bl_label = "Add Parametric Feature"
    bl_options = {'REGISTER', 'UNDO'}
    type: bpy.props.EnumProperty(items=[('BEVEL', "Fillet/Chamfer", ""), ('SOLIDIFY', "Shell/Thicken", ""), ('SCREW', "Revolve", ""), ('MIRROR', "Mirror", "")])
    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return context.active_object is not None
    def execute(self, context: bpy.types.Context) -> Set[str]:
        obj = context.active_object
        if self.type == 'BEVEL':
            mod = obj.modifiers.new(f"{MOD_PREFIX}Parametric_Fillet", 'BEVEL')
            mod.limit_method = 'ANGLE'
            mod.angle_limit = math.radians(30)
            mod.harden_normals = True
        elif self.type == 'SOLIDIFY':
            # --- Create the Solidify modifier for the shell effect ---
            mod = obj.modifiers.new(f"{MOD_PREFIX}Parametric_Shell", 'SOLIDIFY')
            # --- Set offset to 1 for an outward shell ---
            # The default is -1 (inward), but an outward shell is often more predictable
            # for mechanical parts, preventing self-intersection with internal geometry.
            mod.offset = 1.0
            # --- Calculate a sensible default thickness based on object size ---
            # Set the thickness to 10% of the object's average dimension.
            # This provides a reasonable starting point relative to the object's scale.
            avg_dimension = (obj.dimensions.x + obj.dimensions.y + obj.dimensions.z) / 3.0 if obj.dimensions.length > 0 else 1.0
            mod.thickness = avg_dimension * 0.1
        elif self.type == 'SCREW':
            obj.modifiers.new(f"{MOD_PREFIX}Parametric_Revolve", 'SCREW')
        elif self.type == 'MIRROR':
            obj.modifiers.new(f"{MOD_PREFIX}Parametric_Mirror", 'MIRROR')
        self.report({'INFO'}, f"Added {self.type.lower()} modifier to '{obj.name}'.")
        return {'FINISHED'}
class LSD_OT_AddSimplify(bpy.types.Operator):
    """Applies geometry cleanup operations (Weld or Simplify) directly to the mesh"""
    bl_idname = "lsd.add_simplify"
    bl_label = "Simplify"
    bl_options = {'REGISTER', 'UNDO'}
    mode: bpy.props.EnumProperty(
        name="Mode",
        items=[
            ('WELD', "Weld (Merge)", "Merge vertices by distance to fix broken lines"),
            ('COLLAPSE', "Decimate (Simplify)", "Reduce vertex count globally"),
            ('QUADIFY', "Quad-ify", "Convert triangles to quads"),
        ],
        default='WELD'
    )
    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return context.active_object is not None and context.active_object.type == 'MESH'
    def execute(self, context: bpy.types.Context) -> Set[str]:
        obj = context.active_object
        original_mode = obj.mode
        # --- AI Editor Note: Handle Multi-Object Editing ---
        # Collect objects to process based on context.
        objects_to_process = [obj]
        if context.mode == 'OBJECT':
            objects_to_process = [o for o in context.selected_objects if o.type == 'MESH']
        if self.mode == 'WELD':
            for target_obj in objects_to_process:
                context.view_layer.objects.active = target_obj
                # 1. Destructive Intersect (Create vertices at overlaps)
                # This fulfills the requirement to "create new vertices in between overlapping edges".
                if target_obj.mode != 'EDIT':
                    bpy.ops.object.mode_set(mode='EDIT')
                    bpy.ops.mesh.select_all(action='SELECT')
                # AI Editor Note: Use EXACT solver for precise cuts at intersections
                bpy.ops.mesh.intersect(mode='SELECT', solver='EXACT')
                # --- AI Editor Note: Auto Merge & Split Edges ---
                # Enable Auto Merge and Split Edges & Faces to handle vertices on edges.
                # This ensures that vertices lying on edges split the edge, creating valid topology.
                context.tool_settings.use_mesh_automerge = True
                context.tool_settings.use_mesh_automerge_and_split = True
                # Trigger the auto-merge logic by performing a zero-distance translation.
                # This forces Blender to process the geometry with the enabled settings.
                bpy.ops.transform.translate(value=(0, 0, 0))
                if original_mode == 'OBJECT':
                    bpy.ops.object.mode_set(mode='OBJECT')
                # 2. Non-Destructive Weld (Merge vertices)
                # Adds the requested Weld modifier to handle the merging.
                if f"{MOD_PREFIX}Weld" not in target_obj.modifiers:
                    mod = target_obj.modifiers.new(f"{MOD_PREFIX}Weld", 'WELD')
                    mod.merge_threshold = 0.001
            self.report({'INFO'}, "Applied Intersect, Auto-Merge & Added Weld Modifier")
        elif self.mode == 'COLLAPSE':
            for target_obj in objects_to_process:
                # AI Editor Note: Use Decimate Modifier for non-destructive reduction
                mod_name = f"{MOD_PREFIX}Decimate"
                mod = target_obj.modifiers.get(mod_name)
                if not mod:
                    mod = target_obj.modifiers.new(mod_name, 'DECIMATE')
                # AI Editor Note: Switch to Planar (Dissolve) mode for maximum simplification while retaining shape
                mod.decimate_type = 'DISSOLVE'
                mod.angle_limit = math.radians(20.0) # Increased to catch subdivided flat faces reliably
                mod.use_dissolve_boundaries = True # Ensure grid topology on flat faces is cleaned up
            self.report({'INFO'}, "Applied Decimate Modifier (Planar)")
        elif self.mode == 'QUADIFY':
            for target_obj in objects_to_process:
                context.view_layer.objects.active = target_obj
                # Ensure Edit Mode for mesh operation
                if target_obj.mode != 'EDIT':
                    bpy.ops.object.mode_set(mode='EDIT')
                    bpy.ops.mesh.select_all(action='SELECT')
                bpy.ops.mesh.tris_convert_to_quads()
                if original_mode == 'OBJECT':
                    bpy.ops.object.mode_set(mode='OBJECT')
            self.report({'INFO'}, "Converted Tris to Quads")
        # Restore original mode
        if original_mode == 'OBJECT' and context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode=original_mode)
        return {'FINISHED'}
class LSD_OT_SetupLinearArray(bpy.types.Operator):
    bl_idname = "lsd.setup_linear_array"
    bl_label = "Linear Pattern"
    bl_options = {'REGISTER', 'UNDO'}
    count: bpy.props.IntProperty(name="Count", default=3, min=2)
    offset: bpy.props.FloatVectorProperty(name="Offset", default=(1.0, 0.0, 0.0), subtype='TRANSLATION')
    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return context.active_object is not None
    def execute(self, context: bpy.types.Context) -> Set[str]:
        obj = context.active_object
        base_name = f"{MOD_PREFIX}Linear_Pattern"
        mod_name = base_name
        i = 0
        while obj.modifiers.get(mod_name):
            i += 1
            mod_name = f"{base_name}.{i:03d}"
        mod = obj.modifiers.new(mod_name, 'ARRAY')
        mod.count = self.count
        # Default to relative offset for a non-destructive, scalable pattern.
        # Constant offset can be enabled manually by the user if absolute distances are needed.
        mod.use_relative_offset = True
        mod.use_constant_offset = False
        mod.relative_offset_displace = self.offset
        self.report({'INFO'}, f"Added linear array to '{obj.name}'.")
        return {'FINISHED'}
class LSD_OT_SetupRadialArray(bpy.types.Operator):
    bl_idname = "lsd.setup_radial_array"
    bl_label = "Radial Pattern"
    bl_options = {'REGISTER', 'UNDO'}
    count: bpy.props.IntProperty(default=6, min=2)
    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return context.active_object is not None
    def execute(self, context: bpy.types.Context) -> Set[str]:
        obj = context.active_object
        # --- Create and configure the axis Empty ---
        # This empty object will act as the pivot point for the radial array.
        empty = bpy.data.objects.new(f"Radial_Axis_{obj.name}", None)
        context.collection.objects.link(empty)
        # Parent the empty to the object first. This ensures that all subsequent
        # transformations on the empty are in the object's local space.
        empty.parent = obj
        # Explicitly set the empty's local transform to the parent's origin (identity).
        # This guarantees it is perfectly centered and has no initial rotation or scale,
        # preventing unwanted offsets in the array that cause a screw pattern.
        empty.location = (0, 0, 0)
        empty.rotation_euler = (0, 0, 0)
        empty.scale = (1, 1, 1)
        # The empty's local rotation now determines the angle between array instances.
        empty.rotation_euler.z = (math.pi * 2) / self.count
        # --- Create and configure the Array modifier ---
        mod = obj.modifiers.new(f"{MOD_PREFIX}Radial_Array_{empty.name}", 'ARRAY')
        mod.count = self.count
        # Enable Relative Offset by default with a factor of 1.0 on the X-axis.
        # This creates a spiral pattern by default. For a flat radial pattern,
        # the user can set the relative offset to zero in the modifier panel.
        mod.use_relative_offset = True
        mod.relative_offset_displace = (1.0, 0.0, 0.0)
        # The primary control for the radial pattern is the Object Offset.
        # Because the empty is a child, its local transform is used as the offset.
        mod.use_object_offset = True
        mod.offset_object = empty
        self.report({'INFO'}, f"Added radial array to '{obj.name}'.")
        return {'FINISHED'}
class LSD_OT_CreateCurveForPath(bpy.types.Operator):
    """Spawns a single vertex or control point at the 3D cursor for path drafting."""
    bl_idname = "lsd.create_curve_for_path"
    bl_label = "Spawn Vertex"
    bl_options = {'REGISTER', 'UNDO'}
    type: bpy.props.EnumProperty(
        name="Path Type",
        items=[
            ('BEZIER', "Bezier Path", "Draft a path using Bezier control points"),
            ('NURBS', "NURBS Path", "Draft a path using NURBS control points"),
            ('POLY', "Mesh Vertex", "Spawn a real mesh vertex object for precision vertex-instancing"),
        ],
        default='BEZIER'
    )
    def execute(self, context: bpy.types.Context) -> Set[str]:
        import bmesh
        # Ensure we are in object mode before creating new objects
        if context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        if self.type == 'POLY':
            # Create a real MESH object with a single vertex
            mesh_data = bpy.data.meshes.new("Path0_Data")
            bm = bmesh.new()
            bm.verts.new((0, 0, 0)) # Start with a pure, single point
            bm.to_mesh(mesh_data)
            bm.free()
            path_obj = bpy.data.objects.new("Path_Origin", mesh_data)
            path_obj.location = context.scene.cursor.location
            context.collection.objects.link(path_obj)
            # Select and enter edit mode
            bpy.ops.object.select_all(action='DESELECT')
            context.view_layer.objects.active = path_obj
            path_obj.select_set(True)
            bpy.ops.object.mode_set(mode='EDIT')
            # --- SELECTION FIX: Select the single vertex for immediate path extension ---
            bm = bmesh.from_edit_mesh(path_obj.data)
            for v in bm.verts: v.select = True
            bmesh.update_edit_mesh(path_obj.data)
            self.report({'INFO'}, f"Spawned new Mesh Vertex '{path_obj.name}' at cursor.")
            return {'FINISHED'}
        # Create curve data and object
        curve_data = bpy.data.curves.new("Path_Data", type='CURVE')
        curve_data.dimensions = '3D'
        spline = curve_data.splines.new(self.type)
        if self.type == 'BEZIER':
            spline.bezier_points[0].co = (0, 0, 0)
            spline.bezier_points[0].handle_left_type = 'AUTO'
            spline.bezier_points[0].handle_right_type = 'AUTO'
        else: # NURBS
            spline.points[0].co = (0, 0, 0, 1)
        path_obj = bpy.data.objects.new("Path_Path", curve_data)
        path_obj.location = context.scene.cursor.location
        context.collection.objects.link(path_obj)
        # Make the new curve active and enter edit mode for immediate shaping
        bpy.ops.object.select_all(action='DESELECT')
        context.view_layer.objects.active = path_obj
        path_obj.select_set(True)
        bpy.ops.object.mode_set(mode='EDIT')
        # Select the last created point for immediate extrusion by the user.
        bpy.ops.curve.select_all(action='DESELECT')
        spline = path_obj.data.splines[0]
        if spline.type == 'BEZIER':
            # For Bezier, select the last point's control point
            if len(spline.bezier_points) > 0:
                spline.bezier_points[-1].select_control_point = True
        elif spline.points: # For NURBS/POLY
            if len(spline.points) > 0:
                spline.points[-1].select = True
        self.report({'INFO'}, f"Created new {self.type} curve '{path_obj.name}' at cursor.")
        return {'FINISHED'}
class LSD_OT_SetupCurveArray(bpy.types.Operator):
    """Creates an array of the selected object(s) that follows the active path (Curve or Vertex Mesh)"""
    bl_idname = "lsd.setup_curve_array"
    bl_label = "Follow Path"
    bl_options = {'REGISTER', 'UNDO'}
    mode: bpy.props.EnumProperty(
        name="Path Type",
        description="Choose how the array follows the path",
        items=[
            ('DEFORM', "Deform", "Stretches and bends the mesh to fit the path. (Curves only)"),
            ('RIGID', "Rigid Instances Array", "Creates rigid, non-deforming copies along the path. (Curves or Mesh Vertices)"),
        ],
        default='DEFORM'
    )
    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        active_obj = context.active_object
        if not active_obj or active_obj.type not in {'CURVE', 'MESH'}:
            cls.poll_message_set("Active object must be a Curve or a Mesh Path.")
            return False
        selected_meshes = [obj for obj in context.selected_objects if obj != active_obj and obj.type == 'MESH']
        if not selected_meshes:
            cls.poll_message_set("Select a mesh object to array, then the curve object last.")
            return False
        for mesh_obj in selected_meshes:
            # AI Editor Note: Allow parametric parts to be used with this tool, but explicitly
            # block 'CHAIN' types, as they have their own dedicated curve-following system.
            # This allows users to create arrays of gears, custom links, etc., along a curve.
            if hasattr(mesh_obj, 'lsd_pg_mech_props') and mesh_obj.lsd_pg_mech_props.is_part:
                if mesh_obj.lsd_pg_mech_props.category == 'CHAIN':
                    cls.poll_message_set("Cannot use 'Follow Curve' on a Chain part. Use its own settings.")
                    return False
        return True
    def execute(self, context: bpy.types.Context) -> Set[str]:
        path_obj = context.active_object  # The active object is the curve path
        objects_to_modify = [obj for obj in context.selected_objects if obj != path_obj and obj.type == 'MESH']
        # --- Main loop to apply the selected mode to each object ---
        for obj in objects_to_modify:
            # --- STEP 1: Clean up any previous setups from this tool ---
            for mod_name in [f"{MOD_PREFIX}FollowPath_Array", f"{MOD_PREFIX}FollowPath_Deform"]:
                if mod_name in obj.modifiers:
                    obj.modifiers.remove(obj.modifiers[mod_name])
            # B. If the object was part of a 'Rigid' setup, dismantle it completely.
            # Use startswith for robustness in case of .001 suffixes.
            proxy_name_prefix = f"Proxy_{obj.name}"
            if obj.parent and obj.parent.name.startswith(proxy_name_prefix):
                proxy_obj = obj.parent
                # Restore object's world transform before unparenting.
                original_matrix = obj.matrix_world.copy()
                obj.parent = None
                obj.matrix_world = original_matrix
                # Delete the old proxy object and its mesh data to prevent orphans.
                if proxy_obj.data:
                    bpy.data.meshes.remove(proxy_obj.data, do_unlink=True)
                bpy.data.objects.remove(proxy_obj, do_unlink=True)
            # C. Ensure the object is visible and selectable again.
            obj.hide_set(False)
            obj.hide_select = False
            obj.hide_viewport = False
            obj.hide_render = False
            # --- STEP 2: Apply the new setup based on the selected mode ---
            if self.mode == 'DEFORM':
                # --- Deforming Array Method ---
                # --- FIX: Auto-align object to curve start ---
                # To prevent the object from jumping when the Curve modifier is added,
                # its origin must be aligned with the starting point of the curve.
                # This code block calculates the world-space position of the curve's
                # first vertex and moves the object there before adding modifiers.
                if path_obj.data.splines:
                    spline = path_obj.data.splines[0]
                    if spline.type == 'BEZIER' and spline.bezier_points:
                        first_vtx_local_co = spline.bezier_points[0].co
                    elif spline.points:
                        first_vtx_local_co = spline.points[0].co.to_3d()
                    else:
                        # Fallback for empty curves: use the curve object's origin.
                        first_vtx_local_co = mathutils.Vector((0,0,0))
                    # Move the object's origin to the curve's starting point.
                    obj.matrix_world.translation = path_obj.matrix_world @ first_vtx_local_co
                # This is the classic setup that bends and stretches the mesh itself.
                # It requires the object to have enough geometry to deform smoothly.
                if path_obj.type != 'CURVE':
                    self.report({'WARNING'}, f"Object '{obj.name}' cannot use DEFORM on a non-curve path.")
                    continue
                # --- NEW: Synchronize origins to prevent "floating" displacement ---
                # The object should be at the curve's location for accurate deformation.
                obj.matrix_world.translation = path_obj.matrix_world.translation
                array_mod = obj.modifiers.new(f"{MOD_PREFIX}FollowPath_Array", 'ARRAY')
                array_mod.fit_type = 'FIT_CURVE'
                array_mod.curve = path_obj
                array_mod.use_relative_offset = True
                array_mod.relative_offset_displace = (1.0, 0.0, 0.0)
                curve_mod = obj.modifiers.new(f"{MOD_PREFIX}FollowPath_Deform", 'CURVE')
                curve_mod.object = path_obj
                curve_mod.deform_axis = 'POS_X'
            elif self.mode == 'RIGID':
                # --- NEW: Geometry Nodes Rigid Instancing Method ---
                # This modern approach uses a GN modifier on the curve itself.
                # It avoids parenting and hiding the original object, which resolves
                # the user's issue of the object being grayed out and hard to edit.
                # The original object remains visible and fully editable.
                # Get or create the reusable node group for this tool.
                gn_group = generators.setup_gn_for_rigid_array(path_obj)
                # Add a dedicated GN modifier to the curve for this specific object.
                mod_name = f"{MOD_PREFIX}RigidArray_{obj.name}"
                if mod_name in path_obj.modifiers:
                    path_obj.modifiers.remove(path_obj.modifiers[mod_name])
                mod = path_obj.modifiers.new(name=mod_name, type='NODES')
                mod.node_group = gn_group
                # --- Connect Modifier Inputs ---
                iface = gn_group.interface
                instance_obj_socket = iface.items_tree.get("Instance Object")
                spacing_socket = iface.items_tree.get("Spacing")
                is_mesh_socket = iface.items_tree.get("Is Mesh")
                if instance_obj_socket:
                    mod[instance_obj_socket.identifier] = obj
                if spacing_socket:
                    # Use the object's X-dimension as the default spacing.
                    spacing = obj.dimensions.x if obj.dimensions.x > 0.01 else 1.0
                    mod[spacing_socket.identifier] = spacing
                if is_mesh_socket:
                    # Inform GN if we are using a Mesh Path (Spawn Vertex) or a Curve Path
                    mod[is_mesh_socket.identifier] = (path_obj.type == 'MESH')
        self.report({'INFO'}, f"Set {len(objects_to_modify)} object(s) to follow '{path_obj.name}' using '{self.mode}' mode.")
        return {'FINISHED'}
class LSD_OT_CommitPathAlignment(bpy.types.Operator):
    """Bakes the current alignment offsets and clears the alignment mask."""
    bl_idname = "lsd.commit_path_alignment"
    bl_label = "Commit Path Alignment"
    bl_options = {'REGISTER', 'UNDO'}
    def execute(self, context: bpy.types.Context) -> Set[str]:
        scene = context.scene
        # Final pass to ensure maximum precision before bake
        from . import core
        core.apply_path_vertex_alignment(context)
        # Reset and Clear
        scene.lsd_path_live_align = False
        scene.lsd_path_align_pos = (False, False, False)
        scene.lsd_path_align_neg = (False, False, False)
        self.report({'INFO'}, "Path alignment committed and drafting masks reset.")
        return {'FINISHED'}
class LSD_OT_SmartSmooth(bpy.types.Operator):
    bl_idname = "lsd.smart_smooth"
    bl_label = "Smart Smooth"
    bl_options = {'REGISTER', 'UNDO'}
    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return context.active_object is not None and context.active_object.type == 'MESH'
    def execute(self, context: bpy.types.Context) -> Set[str]:
        obj = context.active_object
        # Apply the base auto smooth (Edge Split via GN)
        apply_auto_smooth(obj)
        if f"{MOD_PREFIX}Weighted_Normal" not in obj.modifiers:
            mod = obj.modifiers.new(f"{MOD_PREFIX}Weighted_Normal", 'WEIGHTED_NORMAL')
            mod.keep_sharp = True
        self.report({'INFO'}, f"Applied smart smooth to '{obj.name}'.")
        return {'FINISHED'}
class LSD_OT_AddMimic(bpy.types.Operator):
    bl_idname = "lsd.add_mimic"
    bl_label = "Add / Update Driver"
    bl_options = {'REGISTER', 'UNDO'}
    def execute(self, context: bpy.types.Context) -> Set[str]:
        bones_to_process = context.selected_pose_bones if context.selected_pose_bones else [context.active_pose_bone]
        count = 0
        for bone in bones_to_process:
            if not bone: continue
            props = bone.lsd_pg_kinematic_props
            driver_name = props.ratio_target_bone
            if not driver_name:
                continue
            # 1. Update/Add to the persistent list first
            found = False
            for m in props.mimic_drivers:
                if m.target_bone == driver_name:
                    m.ratio = props.ratio_value
                    m.invert = props.ratio_invert
                    m.drive_x = props.ratio_drive_x
                    m.drive_y = props.ratio_drive_y
                    m.drive_z = props.ratio_drive_z
                    found = True
            if not found:
                item = props.mimic_drivers.add()
                item.target_bone = driver_name
                item.ratio = props.ratio_value
                item.invert = props.ratio_invert
                item.drive_x = props.ratio_drive_x
                item.drive_y = props.ratio_drive_y
                item.drive_z = props.ratio_drive_z
            # 2. Re-apply drivers based on the UPDATED list
            core.reapply_mimic_drivers(bone)
            count += 1
        self.report({'INFO'}, f"Added mimic driver to {count} bone(s).")
        return {'FINISHED'}
class LSD_OT_RemoveMimic(bpy.types.Operator):
    bl_idname = "lsd.remove_mimic"
    bl_label = "Remove Gear"
    bl_options = {'REGISTER', 'UNDO'}
    index: bpy.props.IntProperty()
    def execute(self, context: bpy.types.Context) -> Set[str]:
        bones_to_process = context.selected_pose_bones if context.selected_pose_bones else [context.active_pose_bone]
        count = 0
        for bone in bones_to_process:
            if not bone or self.index >= len(bone.lsd_pg_kinematic_props.mimic_drivers):
                continue
            item = bone.lsd_pg_kinematic_props.mimic_drivers[self.index]
            if bone.id_data.animation_data and bone.id_data.animation_data.drivers:
                drivers = bone.id_data.animation_data.drivers
                for i in range(len(drivers)-1, -1, -1):
                    d = drivers[i]
                    for v in d.driver.variables:
                        # Check both standard bone-target and single-prop data paths (for rotation)
                        target = v.targets[0]
                        if target.bone_target == item.target_bone or \
                           (target.data_path and f'["{item.target_bone}"]' in target.data_path):
                            drivers.remove(d)
                            break
            bone.lsd_pg_kinematic_props.mimic_drivers.remove(self.index)
            count += 1
        self.report({'INFO'}, f"Removed mimic driver from {count} bone(s).")
        return {'FINISHED'}
class LSD_OT_ClearConfig(bpy.types.Operator):
    bl_idname = "lsd.clear_config"
    bl_label = "Clear Config"
    def execute(self, context: bpy.types.Context) -> Set[str]:
        bones_to_process = context.selected_pose_bones if context.selected_pose_bones else [context.active_pose_bone]
        count = 0
        for bone in bones_to_process:
            if not bone: continue
            props = bone.lsd_pg_kinematic_props
            # Setting joint_type to 'none' triggers update_gizmo_prop,
            # which handles gizmos, FK constraints, and IK limits.
            props.joint_type = 'none'
            # Clear remaining properties manually
            props.lower_limit = -90.0
            props.upper_limit = 90.0
            # Reset sizing properties to their defaults
            props.joint_radius = 0.5
            props.visual_gizmo_scale = 1.0
            props.use_ratio = False
            props.ratio_value = 1.0
            props.ratio_invert = False
            props.ratio_target_bone = ""
            props.ratio_ref_bone = ""
            props.mimic_drivers.clear()
            if bone.id_data.animation_data:
                drivers = bone.id_data.animation_data.drivers
                for i in range(len(drivers)-1, -1, -1):
                    d = drivers[i]
                    if d.data_path.startswith(f"pose.bones[\"{bone.name}\"]"):
                        drivers.remove(d)
            count += 1
        self.report({'INFO'}, f"Cleared configuration for {count} bone(s).")
        return {'FINISHED'}
class LSD_OT_PickBone(bpy.types.Operator):
    bl_idname = "lsd.pick_bone"
    bl_label = "Pick Bone"
    mode: bpy.props.IntProperty(default=0)
    source_bone_name: bpy.props.StringProperty(name="Source Bone Name")
    def invoke(self, context: bpy.types.Context, event: bpy.types.Event) -> Set[str]:
        if context.mode == 'POSE':
            self.source_bone_name = context.active_pose_bone.name
            context.window.cursor_set('EYEDROPPER')
            context.window_manager.modal_handler_add(self)
            return {'RUNNING_MODAL'}
        return {'CANCELLED'}
    def modal(self, context: bpy.types.Context, event: bpy.types.Event) -> Set[str]:
        if event.type in {'RIGHTMOUSE', 'ESC'}:
            context.window.cursor_set('DEFAULT')
            return {'CANCELLED'}
        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            bpy.ops.view3d.select(location=(event.mouse_region_x, event.mouse_region_y))
            if context.active_pose_bone and context.active_pose_bone.name != self.source_bone_name:
                rig = context.object
                src = rig.pose.bones.get(self.source_bone_name)
                if self.mode == 0:
                    src.lsd_pg_kinematic_props.ratio_target_bone = context.active_pose_bone.name
                else:
                    src.lsd_pg_kinematic_props.ratio_ref_bone = context.active_pose_bone.name
                rig.data.bones.active = src.bone
                context.window.cursor_set('DEFAULT')
                return {'FINISHED'}
        return {'RUNNING_MODAL'}
def write_urdf(context: bpy.types.Context, filepath: str, rig: bpy.types.Object, mesh_format: str) -> None:
    """
    Generates and writes the complete URDF file for the given armature.
    This function orchestrates the entire export process. It constructs the root
    <robot> XML element, adds required Gazebo plugins, finds the root(s) of the
    kinematic chain, and then recursively builds the URDF structure from there.
    Args:
        context: The current Blender context.
        filepath: The absolute path to write the URDF file to.
        rig: The armature object to export.
        mesh_format: The file format for the exported meshes (e.g., 'STL').
    """
    robot_name = rig.name
    robot = ET.Element('robot', name=robot_name)
    # --- Gazebo Plugin for ros2_control ---
    # This is a standard Gazebo plugin required to interface with ROS 2 controllers.
    gazebo = ET.SubElement(robot, 'gazebo')
    plugin = ET.SubElement(gazebo, 'plugin', name='gazebo_ros2_control', filename='libgazebo_ros2_control.so')
    ET.SubElement(plugin, 'robot_param').text = 'robot_description'
    ET.SubElement(plugin, 'robot_param_node').text = 'robot_state_publisher'
    # Find the root bone(s) of the armature. A robot can have multiple disconnected kinematic chains.
    root_bones = [b for b in rig.pose.bones if not b.parent]
    if not root_bones:
        print("URDF Export Error: No root bone found in the armature.")
        return
    # A set to keep track of links that have already been processed.
    # This is crucial for robots with multiple roots or complex branching.
    processed_links: Set[str] = set()
    for root_bone in root_bones:
        add_link_recursive(robot, root_bone, context, robot_name, mesh_format, processed_links)
    # --- Write the final XML file ---
    try:
        tree = ET.ElementTree(robot)
        ET.indent(tree, space="  ", level=0)
        tree.write(filepath, encoding='utf-8', xml_declaration=True)
    except IOError as e:
        print(f"Error writing URDF file to {filepath}: {e}")
        # It would be beneficial to report this error to the user via the UI.
        # self.report({'ERROR'}, f"File I/O Error: {e}") # (if in an operator)
def add_link_recursive(
    robot: ET.Element,
    bone: bpy.types.PoseBone,
    context: bpy.types.Context,
    robot_name: str,
    mesh_format: str,
    processed_links: Set[str]
) -> None:
    """
    Recursively adds a <link> and its corresponding <joint> to the URDF tree.
    This function is the core of the export logic. For each bone, it:
    1. Creates a <link> element with the bone's name.
    2. Adds <visual>, <collision>, and <inertial> tags for all associated meshes.
    3. Adds Gazebo-specific tags for simulation appearance.
    4. If the bone has a parent, it creates a <joint> element to connect it.
    5. Recursively calls itself for all child bones.
    Args:
        robot: The root <robot> XML element.
        bone: The current bone being processed.
        context: The current Blender context.
        robot_name: The name of the robot (used for package paths).
        mesh_format: The file format for exported meshes.
        processed_links: A set to track processed links to prevent duplicates in
                         complex or multi-root armatures.
    """
    if bone.name in processed_links:
        return
    processed_links.add(bone.name)
    link = ET.SubElement(robot, 'link', name=bone.name)
    child_meshes = get_all_children_objects(bone, context)
    # --- Add Visual and Collision Tags ---
    # A single link can be composed of multiple visual and collision meshes.
    if not child_meshes:
        # Add a dummy box to represent the link if no mesh is attached.
        # This can be useful for visualizing the kinematic chain.
        add_dummy_visual(link, bone)
    else:
        for mesh_obj in child_meshes:
            add_visual(link, bone, mesh_obj, robot_name, mesh_format)
            add_collision(link, bone, mesh_obj, robot_name, mesh_format)
    # --- Add Inertial Tag (once per link) ---
    # The inertial properties are defined for the link as a whole.
    add_inertial(link, bone)
    # --- Add Gazebo-specific Tags (e.g., material for simulation) ---
    add_gazebo_tags(link, bone)
    # --- Add Joint connecting this link to its parent ---
    if bone.parent:
        add_joint(robot, parent_bone=bone.parent, child_bone=bone)
    # --- Add Transmission Tag (for ros2_control) ---
    add_transmission(robot, bone)
    # --- Recurse for all children ---
    for child_bone in bone.children:
        add_link_recursive(robot, child_bone, context, robot_name, mesh_format, processed_links)
def add_dummy_visual(link_element: ET.Element, bone: bpy.types.PoseBone) -> None:
    """Adds a placeholder box visual for links with no associated mesh."""
    visual = ET.SubElement(link_element, 'visual')
    ET.SubElement(visual, 'origin', xyz="0 0 0", rpy="0 0 0")
    geometry = ET.SubElement(visual, 'geometry')
    box = ET.SubElement(geometry, 'box')
    size = max(0.01, bone.length) # Ensure a minimum size
    box.set('size', f"{size} {size} {size}")
    material = ET.SubElement(visual, 'material', name=f"{bone.name}_dummy_mat")
    ET.SubElement(material, 'color', rgba="0.7 0.7 0.7 1.0")
def add_visual(link_element: ET.Element, bone: bpy.types.PoseBone, mesh_obj: bpy.types.Object, robot_name: str, mesh_format: str) -> None:
    """
    Adds a <visual> tag to a link for a specific mesh object.
    Args:
        link_element: The <link> XML element to add to.
        bone: The bone that defines the link's frame of reference.
        mesh_obj: The mesh object to create the visual tag for.
        robot_name: The name of the robot package.
        mesh_format: The file format for the mesh.
    """
    visual = ET.SubElement(link_element, 'visual')
    # --- Origin Tag ---
    # The <origin> tag defines the pose of the visual mesh relative to the
    # link's origin (which is the bone's head).
    #
    # Transformation Logic:
    # 1. `bone.matrix`: The transform of the bone's head from Armature Space to World Space.
    #    This defines the link's coordinate frame.
    # 2. `mesh_obj.matrix_local`: The transform of the mesh object relative to its parent (the armature).
    # 3. `bone.matrix.inverted()`: Creates a matrix that transforms from World Space into the
    #    bone's (link's) local space.
    # 4. `bone.matrix.inverted() @ mesh_obj.matrix_local`: This multiplication results in the
    #    transform of the mesh object directly relative to the bone's head, which is
    #    exactly what the URDF <origin> tag requires.
    relative_matrix = bone.matrix.inverted() @ mesh_obj.matrix_local
    loc, rot, _ = relative_matrix.decompose()
    rpy = rot.to_euler('XYZ')
    origin = ET.SubElement(visual, 'origin')
    origin.set('xyz', f"{loc.x:.6f} {loc.y:.6f} {loc.z:.6f}")
    origin.set('rpy', f"{rpy.x:.6f} {rpy.y:.6f} {rpy.z:.6f}")
    # --- Geometry Tag ---
    geometry = ET.SubElement(visual, 'geometry')
    mesh_elem = ET.SubElement(geometry, 'mesh')
    mesh_ext = mesh_format.lower()
    if mesh_format == 'GLTF':
        mesh_ext = 'glb'
    mesh_filepath = os.path.join("meshes", f"{mesh_obj.name}.{mesh_ext}")
    mesh_elem.set('filename', f"package://{robot_name}/{mesh_filepath}")
    mesh_elem.set('scale', "1 1 1") # Scale is baked into the exported mesh.
    # --- Material Tag ---
    # Use the material from the bone's properties for consistency across the link.
    material_props = bone.lsd_pg_kinematic_props.material
    mat_name = f"{bone.name}_material" # Use bone name for a single link material
    material = ET.SubElement(visual, 'material', name=mat_name)
    color = material_props.color
    ET.SubElement(material, 'color', rgba=f"{color[0]:.3f} {color[1]:.3f} {color[2]:.3f} {color[3]:.3f}")
    if material_props.texture and material_props.texture.name:
        ET.SubElement(material, 'texture', filename=f"package://{robot_name}/textures/{material_props.texture.name}")
def add_collision(link_element: ET.Element, bone: bpy.types.PoseBone, mesh_obj: bpy.types.Object, robot_name: str, mesh_format: str) -> None:
    """
    Adds a <collision> tag to a link.
    It can use either the visual mesh itself or a dedicated collision object
    defined in the bone's properties.
    Args:
        link_element: The <link> XML element.
        bone: The bone defining the link's frame.
        mesh_obj: The primary visual mesh object (used as a fallback).
        robot_name: The name of the robot package.
        mesh_format: The file format for the mesh.
    """
    collision = ET.SubElement(link_element, 'collision')
    collision_props = bone.lsd_pg_kinematic_props.collision
    # Default to the visual mesh if no specific collision object is assigned.
    collision_obj = collision_props.collision_object if collision_props.collision_object else mesh_obj
    if not collision_obj: return
    # --- Origin Tag (same logic as visual) ---
    relative_matrix = bone.matrix.inverted() @ collision_obj.matrix_local
    loc, rot, _ = relative_matrix.decompose()
    rpy = rot.to_euler('XYZ')
    origin = ET.SubElement(collision, 'origin')
    origin.set('xyz', f"{loc.x:.6f} {loc.y:.6f} {loc.z:.6f}")
    origin.set('rpy', f"{rpy.x:.6f} {rpy.y:.6f} {rpy.z:.6f}")
    # --- Geometry Tag ---
    geometry = ET.SubElement(collision, 'geometry')
    shape = collision_props.shape
    # Use dimensions of the evaluated collision object for primitives.
    depsgraph = bpy.context.evaluated_depsgraph_get()
    eval_coll_obj = collision_obj.evaluated_get(depsgraph)
    dims = eval_coll_obj.dimensions
    if shape == 'MESH':
        mesh_elem = ET.SubElement(geometry, 'mesh')
        mesh_ext = mesh_format.lower()
        if mesh_format == 'GLTF':
            mesh_ext = 'glb'
        mesh_filepath = os.path.join("meshes", f"{collision_obj.name}.{mesh_ext}")
        mesh_elem.set('filename', f"package://{robot_name}/{mesh_filepath}")
        mesh_elem.set('scale', "1 1 1")
    elif shape == 'BOX':
        box = ET.SubElement(geometry, 'box')
        box.set('size', f"{dims.x:.6f} {dims.y:.6f} {dims.z:.6f}")
    elif shape == 'CYLINDER':
        cylinder = ET.SubElement(geometry, 'cylinder')
        # URDF assumes Z is the cylinder's height axis.
        radius = (dims.x + dims.y) / 4.0
        cylinder.set('radius', f"{radius:.6f}")
        cylinder.set('length', f"{dims.z:.6f}")
    elif shape == 'SPHERE':
        sphere = ET.SubElement(geometry, 'sphere')
        radius = (dims.x + dims.y + dims.z) / 6.0
        sphere.set('radius', f"{radius:.6f}")
def add_inertial(link_element: ET.Element, bone: bpy.types.PoseBone) -> None:
    """
    Adds the <inertial> tag to a link using properties from the bone.
    Args:
        link_element: The <link> XML element.
        bone: The bone containing the inertial properties.
    """
    inertial = ET.SubElement(link_element, 'inertial')
    inertial_props = bone.lsd_pg_kinematic_props.inertial
    ET.SubElement(inertial, 'mass', value=f"{inertial_props.mass:.6f}")
    # The origin of the inertial frame is the center of mass, relative to the link frame.
    com = inertial_props.center_of_mass
    ET.SubElement(inertial, 'origin', xyz=f"{com.x:.6f} {com.y:.6f} {com.z:.6f}", rpy="0 0 0")
    ET.SubElement(inertial, 'inertia',
                   ixx=f"{inertial_props.ixx:.6f}", iyy=f"{inertial_props.iyy:.6f}", izz=f"{inertial_props.izz:.6f}",
                   ixy=f"{inertial_props.ixy:.6f}", ixz=f"{inertial_props.ixz:.6f}", iyz=f"{inertial_props.iyz:.6f}")
def add_gazebo_tags(link_element: ET.Element, bone: bpy.types.PoseBone) -> None:
    """
    Adds Gazebo-specific tags to a link, such as material for simulation.
    Args:
        link_element: The <link> XML element.
        bone: The bone containing the material properties.
    """
    material_props = bone.lsd_pg_kinematic_props.material
    color = material_props.color
    # This <gazebo> tag with a 'reference' is a Gazebo extension.
    # It allows setting simulation-specific properties for the link.
    gazebo_tag = ET.SubElement(link_element, 'gazebo', reference=bone.name)
    material_tag = ET.SubElement(gazebo_tag, 'material')
    # Define the material script for Gazebo's rendering engine.
    ET.SubElement(material_tag, 'script').append(ET.Element('name', text='Gazebo/Grey'))
    # Set color properties for the simulator.
    ET.SubElement(material_tag, 'ambient').text = f"{color[0]} {color[1]} {color[2]} {color[3]}"
    ET.SubElement(material_tag, 'diffuse').text = f"{color[0]} {color[1]} {color[2]} {color[3]}"
    ET.SubElement(material_tag, 'specular').text = "0.1 0.1 0.1 1.0"
    ET.SubElement(material_tag, 'pbr') # Add empty PBR element for modern Gazebo versions
def add_joint(robot: ET.Element, parent_bone: bpy.types.PoseBone, child_bone: bpy.types.PoseBone) -> None:
    """
    Adds a <joint> tag to the robot, connecting a child link to a parent link.
    Args:
        robot: The root <robot> XML element.
        parent_bone: The parent bone in the kinematic chain.
        child_bone: The child bone being connected.
    """
    props = child_bone.lsd_pg_kinematic_props
    joint_type = props.joint_type
    if joint_type == 'none': # 'none' means it's just a link in the chain, not a joint.
        return
    # AI Editor Note: Treat 'base' as 'fixed' for URDF export compatibility.
    # The 'base' type is a Blender-only concept for a freely movable root.
    # In URDF, if it has a parent, it must be rigidly attached.
    export_joint_type = 'fixed' if joint_type == 'base' else joint_type
    joint = ET.SubElement(robot, 'joint', name=f"{parent_bone.name}_to_{child_bone.name}", type=export_joint_type)
    ET.SubElement(joint, 'parent', link=parent_bone.name)
    ET.SubElement(joint, 'child', link=child_bone.name)
    # --- Origin Tag ---
    # The joint's origin is the pose of the child bone's head relative to the
    # parent bone's head. This uses the same transformation logic as the visual/collision origin.
    relative_matrix = parent_bone.matrix.inverted() @ child_bone.matrix
    loc, rot, _ = relative_matrix.decompose()
    rpy = rot.to_euler('XYZ')
    origin = ET.SubElement(joint, 'origin')
    origin.set('xyz', f"{loc.x:.6f} {loc.y:.6f} {loc.z:.6f}")
    origin.set('rpy', f"{rpy.x:.6f} {rpy.y:.6f} {rpy.z:.6f}")
    # --- Axis Tag (for non-fixed joints) ---
    if export_joint_type not in ['fixed']:
        axis_elem = ET.SubElement(joint, 'axis')
        axis_alignment = props.axis_alignment
        # Convert UI enum to a vector. The axis is defined in the joint's local frame.
        if 'X' in axis_alignment: axis_vec = mathutils.Vector((1, 0, 0))
        elif 'Y' in axis_alignment: axis_vec = mathutils.Vector((0, 1, 0))
        else: axis_vec = mathutils.Vector((0, 0, 1)) # Z
        if '-' in axis_alignment: axis_vec *= -1.0
        axis_elem.set('xyz', f"{axis_vec.x} {axis_vec.y} {axis_vec.z}")
    # --- Limit Tag (for revolute and prismatic joints) ---
    if export_joint_type in ['revolute', 'prismatic']:
        limit = ET.SubElement(joint, 'limit', effort="1000.0", velocity="1.0")
        if export_joint_type == 'revolute':
            limit.set('lower', f"{math.radians(props.lower_limit):.6f}")
            limit.set('upper', f"{math.radians(props.upper_limit):.6f}")
        else: # prismatic
            limit.set('lower', f"{props.lower_limit:.6f}")
            limit.set('upper', f"{props.upper_limit:.6f}")
    # --- Mimic Tag ---
    for mimic in props.mimic_drivers:
        # Find the full joint name for the mimic target.
        mimic_target_bone = robot.find(f".//link[@name='{mimic.target_bone}']..../joint")
        if mimic_target_bone is not None:
             ET.SubElement(joint, 'mimic', joint=mimic_target_bone.get('name'), multiplier=f"{mimic.ratio:.6f}")
def add_transmission(robot: ET.Element, bone: bpy.types.PoseBone) -> None:
    """
    Adds a <transmission> tag for a joint, required for ros2_control.
    Args:
        robot: The root <robot> XML element.
        bone: The bone whose joint needs a transmission.
    """
    props = bone.lsd_pg_kinematic_props
    if props.joint_type not in ['revolute', 'prismatic', 'continuous'] or not bone.parent:
        return
    joint_name = f"{bone.parent.name}_to_{bone.name}"
    trans_name = f"{joint_name}_transmission"
    trans_props = props.transmission
    transmission = ET.SubElement(robot, 'transmission', name=trans_name)
    ET.SubElement(transmission, 'type').text = trans_props.type
    joint_elem = ET.SubElement(transmission, 'joint', name=joint_name)
    ET.SubElement(joint_elem, 'hardwareInterface').text = trans_props.hardware_interface
    actuator_elem = ET.SubElement(transmission, 'actuator', name=f"{joint_name}_motor")
    ET.SubElement(actuator_elem, 'hardwareInterface').text = trans_props.hardware_interface
    ET.SubElement(actuator_elem, 'mechanicalReduction').text = str(trans_props.mechanical_reduction)
class LSD_OT_GenerateROS2Workspace(bpy.types.Operator):
    """Generates a standard ROS 2 workspace structure with a description package"""
    bl_idname = "lsd.generate_ros2_workspace"
    bl_label = "Generate ROS 2 Workspace"
    bl_description = "Create a full ROS 2 workspace structure (src, build, install, log) and a description package template"
    bl_options = {'REGISTER', 'UNDO'}
    workspace_name: bpy.props.StringProperty(
        name="Workspace Name",
        default="ros2_ws",
        description="Name of the workspace directory to create"
    )
    directory: bpy.props.StringProperty(
        name="Workspace Path",
        description="Root directory for the ROS 2 workspace",
        subtype='DIR_PATH'
    )
    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}
    def execute(self, context):
        if not self.directory:
            self.report({'ERROR'}, "Invalid directory path")
            return {'CANCELLED'}
        workspace_root = os.path.join(self.directory, self.workspace_name)
        rig = context.scene.lsd_active_rig
        robot_name = rig.name if rig else "my_robot"
        # Sanitize robot name for ROS 2 (lowercase, underscores)
        pkg_name = re.sub(r'[^a-z0-9_]', '_', robot_name.lower()) + "_description"
        # --- Level 1: The Workspace ---
        for folder in ['src', 'build', 'install', 'log']:
            os.makedirs(os.path.join(workspace_root, folder), exist_ok=True)
        # --- Level 2: The Package ---
        pkg_root = os.path.join(workspace_root, 'src', pkg_name)
        # --- Level 3: Functional Directories ---
        # Create standard folders
        for folder in ['launch', 'config', 'urdf', 'meshes', 'worlds', 'maps', 'include', 'src', 'test']:
            os.makedirs(os.path.join(pkg_root, folder), exist_ok=True)
        # Create subfolders for meshes
        os.makedirs(os.path.join(pkg_root, 'meshes', 'visual'), exist_ok=True)
        os.makedirs(os.path.join(pkg_root, 'meshes', 'collision'), exist_ok=True)
        # 1. package.xml (The Manifest)
        package_xml_content = f"""<?xml version="1.0"?>
<?xml-model href="http://download.ros.org/schema/package_format3.xsd" schematypens="http://www.w3.org/2001/XMLSchema"?>

<package format="3">
  <name>{pkg_name}</name>
  <version>0.0.0</version>
  <description>The {pkg_name} package</description>
  <maintainer email="user@todo.todo">user</maintainer>
  <license>TODO: License declaration</license>
  <buildtool_depend>ament_cmake</buildtool_depend>
  <depend>urdf</depend>
  <depend>xacro</depend>
  <depend>rclcpp</depend>
  <depend>rclpy</depend>
  <depend>std_msgs</depend>
  <depend>gazebo_ros</depend>
  <test_depend>ament_lint_auto</test_depend>
  <test_depend>ament_lint_common</test_depend>
  <export>
    <build_type>ament_cmake</build_type>
  </export>
</package>

"""
        with open(os.path.join(pkg_root, 'package.xml'), 'w') as f:
            f.write(package_xml_content)
        # 2. CMakeLists.txt (Build Rules)
        cmake_content = f"""cmake_minimum_required(VERSION 3.8)
project({pkg_name})

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

find_package(ament_cmake REQUIRED)

find_package(urdf REQUIRED)

find_package(xacro REQUIRED)

install(
  DIRECTORY config launch meshes urdf worlds maps
  DESTINATION share/${{PROJECT_NAME}}
)

ament_package()

"""
        with open(os.path.join(pkg_root, 'CMakeLists.txt'), 'w') as f:
            f.write(cmake_content)
        # 3. Dummy Launch File
        launch_content = f"""import os
from launch import LaunchDescription
from launch.actions import IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch_ros.actions import Node
from ament_index_python.packages import get_package_share_directory

def generate_launch_description():
    return LaunchDescription([
        IncludeLaunchDescription(
            PythonLaunchDescriptionSource([os.path.join(
                get_package_share_directory('gazebo_ros'), 'launch', 'gazebo.launch.py')]),
        ),
    ])
"""
        # AI Editor Note: Renamed to gazebo.launch.py to match the functional requirement.
        # This ensures the template is ready for Gazebo simulation out of the box.
        with open(os.path.join(pkg_root, 'launch', 'gazebo.launch.py'), 'w') as f:
            f.write(launch_content)
        self.report({'INFO'}, f"Generated ROS 2 workspace at {workspace_root}")
        return {'FINISHED'}
class LSD_OT_ExportList_Add(bpy.types.Operator):
    """Add selected armatures to the export list"""
    bl_idname = "lsd.export_list_add"
    bl_label = "Add Selected"
    def execute(self, context):
        added = 0
        for obj in context.selected_objects:
            if obj.type == 'ARMATURE':
                # Check if already in list
                if not any(item.rig == obj for item in context.scene.lsd_export_list):
                    item = context.scene.lsd_export_list.add()
                    item.rig = obj
                    added += 1
        if added == 0 and context.scene.lsd_active_rig:
             if not any(item.rig == context.scene.lsd_active_rig for item in context.scene.lsd_export_list):
                item = context.scene.lsd_export_list.add()
                item.rig = context.scene.lsd_active_rig
                added += 1
        return {'FINISHED'}
class LSD_OT_ExportList_Remove(bpy.types.Operator):
    """Remove active item from export list"""
    bl_idname = "lsd.export_list_remove"
    bl_label = "Remove"
    def execute(self, context):
        idx = context.scene.lsd_export_list_index
        try:
            context.scene.lsd_export_list.remove(idx)
            context.scene.lsd_export_list_index = min(max(0, idx - 1), len(context.scene.lsd_export_list) - 1)
        except:
            pass
        return {'FINISHED'}
class LSD_OT_Export(bpy.types.Operator, ExportHelper):
    bl_idname = "lsd.export_general"
    bl_label = "Export Robot Package"
    filename_ext = ".urdf"
    filter_glob: bpy.props.StringProperty(
        default="*.urdf",
        options={'HIDDEN'},
        maxlen=255,
    )
    def execute(self, context: bpy.types.Context) -> Set[str]:
        scene = context.scene
        # AI Editor Note: Determine which rigs to export (List vs Active)
        rigs_to_export = []
        if len(scene.lsd_export_list) > 0:
            rigs_to_export = [item.rig for item in scene.lsd_export_list if item.rig]
        elif scene.lsd_active_rig:
            rigs_to_export = [scene.lsd_active_rig]
        if not rigs_to_export:
            self.report({'ERROR'}, "No rigs selected for export.")
            return {'CANCELLED'}
        # --- Base path and name from the file dialog ---
        base_dir = os.path.dirname(self.filepath)
        robot_name_from_file = os.path.splitext(os.path.basename(self.filepath))[0]
        # Sanitize package name for ROS 2 (lowercase, underscores)
        # Used if generating config files
        pkg_name = re.sub(r'[^a-z0-9_]', '_', robot_name_from_file.lower())
        # --- URDF Export Logic ---
        if scene.lsd_export_as_urdf:
            mesh_format = scene.lsd_export_mesh_format
            # Export URDF for each rig
            for i, rig in enumerate(rigs_to_export):
                # If exporting multiple, use rig name. If single, use filename.
                if len(rigs_to_export) > 1:
                    lsd_name = f"{rig.name}.urdf"
                else:
                    lsd_name = f"{robot_name_from_file}.urdf"
                lsd_filepath = os.path.join(base_dir, lsd_name)
                write_urdf(context, lsd_filepath, rig, mesh_format)
            # --- Generate Package Config (package.xml, CMakeLists.txt) ---
            if scene.lsd_export_check_config:
                package_xml_content = f"""<?xml version="1.0"?>
<?xml-model href="http://download.ros.org/schema/package_format3.xsd" schematypens="http://www.w3.org/2001/XMLSchema"?>

<package format="3">
  <name>{pkg_name}</name>
  <version>0.0.0</version>
  <description>The {pkg_name} package</description>
  <maintainer email="user@todo.todo">user</maintainer>
  <license>TODO: License declaration</license>
  <buildtool_depend>ament_cmake</buildtool_depend>
  <depend>urdf</depend>
  <depend>xacro</depend>
  <depend>rclcpp</depend>
  <depend>rclpy</depend>
  <depend>std_msgs</depend>
  <depend>gazebo_ros</depend>
  <test_depend>ament_lint_auto</test_depend>
  <test_depend>ament_lint_common</test_depend>
  <export>
    <build_type>ament_cmake</build_type>
  </export>
</package>"""
                with open(os.path.join(base_dir, 'package.xml'), 'w') as f:
                    f.write(package_xml_content)
                cmake_content = f"""cmake_minimum_required(VERSION 3.8)
project({pkg_name})

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

find_package(ament_cmake REQUIRED)

find_package(urdf REQUIRED)

find_package(xacro REQUIRED)

install(
  DIRECTORY config launch meshes urdf worlds maps
  DESTINATION share/${{PROJECT_NAME}}
)

ament_package()"""
                with open(os.path.join(base_dir, 'CMakeLists.txt'), 'w') as f:
                    f.write(cmake_content)
            # --- Generate Launch Files ---
            if scene.lsd_export_check_launch:
                launch_dir = os.path.join(base_dir, 'launch')
                os.makedirs(launch_dir, exist_ok=True)
                # Generate launch file for each rig
                for rig in rigs_to_export:
                    if len(rigs_to_export) > 1:
                        launch_filename = f"gazebo_{rig.name}.launch.py"
                        lsd_name = f"{rig.name}.urdf"
                    else:
                        launch_filename = "gazebo.launch.py"
                        lsd_name = f"{robot_name_from_file}.urdf"
                    launch_content = f"""import os
from launch import LaunchDescription
from launch.actions import IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch_ros.actions import Node
from ament_index_python.packages import get_package_share_directory

def generate_launch_description():
    lsd_file = '{os.path.join(base_dir, lsd_name)}'
    with open(lsd_file, 'r') as infp:
        robot_desc = infp.read()
    return LaunchDescription([
        IncludeLaunchDescription(
            PythonLaunchDescriptionSource([os.path.join(
                get_package_share_directory('gazebo_ros'), 'launch', 'gazebo.launch.py')]),
        ),
        Node(
            package='robot_state_publisher',
            executable='robot_state_publisher',
            name='robot_state_publisher',
            output='screen',
            parameters=[{{'robot_description': robot_desc, 'use_sim_time': True}}]
        ),
        Node(
            package='gazebo_ros',
            executable='spawn_entity.py',
            arguments=['-topic', 'robot_description', '-entity', '{rig.name}'],
            output='screen'
        )
    ])"""
                    # Note: The launch file above uses a hardcoded path for simplicity in this context,
                    # but in a real installed package it would use get_package_share_directory.
                    # For a generated export, we provide a basic template.
                    with open(os.path.join(launch_dir, launch_filename), 'w') as f:
                        f.write(launch_content)
            meshes_to_export: Set[bpy.types.Object] = set()
            for rig in rigs_to_export:
                for bone in rig.pose.bones:
                    visual_meshes = get_all_children_objects(bone, context)
                    meshes_to_export.update(visual_meshes)
                    collision_props = bone.lsd_pg_kinematic_props.collision
                    if collision_props.shape == 'MESH':
                        collision_obj = collision_props.collision_object
                        if collision_obj and collision_obj.type == 'MESH':
                            meshes_to_export.add(collision_obj)
            if meshes_to_export and scene.lsd_export_check_meshes:
                meshes_dir = os.path.join(base_dir, "meshes")
                textures_dir = os.path.join(base_dir, "textures")
                os.makedirs(meshes_dir, exist_ok=True)
                os.makedirs(textures_dir, exist_ok=True)
                original_selection = context.selected_objects
                original_active = context.view_layer.objects.active
                for o in context.view_layer.objects:
                    o.select_set(False)
                wm = context.window_manager
                wm.progress_begin(0, len(meshes_to_export))
                for i, obj in enumerate(meshes_to_export):
                    wm.progress_update(i)
                    context.view_layer.objects.active = obj
                    obj.select_set(True)
                    try:
                        if mesh_format == 'STL':
                            mesh_filepath = os.path.join(meshes_dir, f"{obj.name}.stl")
                            bpy.ops.export_mesh.stl(filepath=mesh_filepath, use_selection=True, global_scale=1.0, use_mesh_modifiers=True)
                        elif mesh_format == 'DAE':
                            mesh_filepath = os.path.join(meshes_dir, f"{obj.name}.dae")
                            bpy.ops.wm.collada_export(filepath=mesh_filepath, selected=True, apply_modifiers=True)
                        elif mesh_format == 'GLTF':
                            mesh_filepath = os.path.join(meshes_dir, f"{obj.name}.glb")
                            bpy.ops.export_scene.gltf(filepath=mesh_filepath, use_selection=True, export_format='GLB', export_apply=True, export_yup=True)
                    except Exception as e:
                        self.report({'WARNING'}, f"Could not export mesh '{obj.name}' for URDF: {e}")
                    obj.select_set(False)
                    if mesh_format in ['DAE', 'GLTF'] and scene.lsd_export_check_textures:
                        if obj.active_material and obj.active_material.use_nodes:
                            for node in obj.active_material.node_tree.nodes:
                                if node.type == 'TEX_IMAGE' and node.image:
                                    try:
                                        texture_filepath = os.path.join(textures_dir, node.image.name)
                                        if not os.path.exists(texture_filepath) and node.image.has_data:
                                            node.image.save(filepath=texture_filepath)
                                    except Exception as e:
                                        self.report({'WARNING'}, f"Could not save texture '{node.image.name}': {e}")
                wm.progress_end()
                for o in context.view_layer.objects:
                    o.select_set(False)
                for obj in original_selection:
                    obj.select_set(True)
                context.view_layer.objects.active = original_active
            self.report({'INFO'}, f"Exported URDF package to {base_dir}")
        # --- glTF Export Logic ---
        if scene.lsd_export_as_gltf:
            gltf_filepath = os.path.join(base_dir, f"{robot_name_from_file}.glb")
            try:
                # Select rig and all its children for export
                # AI Editor Note: Select all rigs if multiple are present
                bpy.ops.object.select_all(action='DESELECT')
                for rig in rigs_to_export:
                    rig.select_set(True)
                    for child in rig.children_recursive:
                        child.select_set(True)
                bpy.ops.export_scene.gltf(
                    filepath=gltf_filepath,
                    use_selection=True,
                    export_format='GLB',
                    export_apply=True,
                    export_textures=scene.lsd_export_gltf_textures,
                    export_normals=scene.lsd_export_gltf_normals,
                    export_animations=scene.lsd_export_gltf_animations,
                    export_yup=True
                )
                self.report({'INFO'}, f"Exported scene as glTF to {gltf_filepath}")
            except Exception as e:
                self.report({'ERROR'}, f"Failed to export glTF: {e}")
        if not scene.lsd_export_as_urdf and not scene.lsd_export_as_gltf:
            self.report({'WARNING'}, "No export format selected.")
            return {'CANCELLED'}
        return {'FINISHED'}
class LSD_OT_ExportSelected(bpy.types.Operator, ExportHelper):
    """Export selected objects to a specific format with geometry and texture data"""
    bl_idname = "lsd.export_selected"
    bl_label = "Export Selected Mesh"
    bl_description = "Export selected objects to USD, glTF, OBJ, etc. for external software (Rhino, etc.)"
    bl_options = {'REGISTER', 'UNDO'}
    filename_ext: bpy.props.StringProperty(default="", options={'HIDDEN'})
    filter_glob: bpy.props.StringProperty(
        default="*.usdc;*.usda;*.glb;*.gltf;*.obj;*.fbx;*.stl",
        options={'HIDDEN'},
    )
    def invoke(self, context, event):
        fmt = context.scene.lsd_quick_export_format
        if fmt == 'USD': self.filename_ext = ".usdc"
        elif fmt == 'GLTF': self.filename_ext = ".glb"
        elif fmt == 'OBJ': self.filename_ext = ".obj"
        elif fmt == 'FBX': self.filename_ext = ".fbx"
        elif fmt == 'STL': self.filename_ext = ".stl"
        return ExportHelper.invoke(self, context, event)
    def execute(self, context):
        fmt = context.scene.lsd_quick_export_format
        fpath = self.filepath
        if not fpath.lower().endswith(self.filename_ext):
            fpath += self.filename_ext
        try:
            if fmt == 'USD':
                bpy.ops.wm.usd_export(filepath=fpath, selected_objects_only=True, export_materials=True, export_textures=True, export_uvmaps=True, export_normals=True)
            elif fmt == 'GLTF':
                bpy.ops.export_scene.gltf(filepath=fpath, use_selection=True, export_format='GLB', export_apply=True, export_texcoords=True, export_normals=True)
            elif fmt == 'OBJ':
                bpy.ops.wm.obj_export(filepath=fpath, export_selected_objects=True, export_materials=True, export_uv=True, export_normals=True, apply_modifiers=True)
            elif fmt == 'FBX':
                bpy.ops.export_scene.fbx(filepath=fpath, use_selection=True, use_mesh_modifiers=True, mesh_smooth_type='FACE', path_mode='COPY', embed_textures=True)
            elif fmt == 'STL':
                bpy.ops.export_mesh.stl(filepath=fpath, use_selection=True, use_mesh_modifiers=True)
            self.report({'INFO'}, f"Exported selected to {fpath}")
            return {'FINISHED'}
        except Exception as e:
            self.report({'ERROR'}, f"Export failed: {e}")
            return {'CANCELLED'}
class LSD_OT_TogglePlacement(bpy.types.Operator):
    bl_idname = "lsd.toggle_placement"
    bl_label = "Toggle Joint Placement Mode"
    def execute(self, context: bpy.types.Context) -> Set[str]:
        rig = context.scene.lsd_active_rig
        if not rig:
            self.report({'WARNING'}, "No active rig selected.")
            return {'CANCELLED'}
        is_entering_placement = not context.scene.lsd_placement_mode
        if is_entering_placement:
            context.scene.lsd_placement_mode = True
            if rig.animation_data:
                for d in rig.animation_data.drivers:
                    d.mute = True
            # --- AI Editor Note: Robust Context Management ---
            # To safely switch to Pose Mode, we must first ensure that the target
            # armature ('rig') is the active object in the view layer.
            # The following lines guarantee the correct context before calling the mode_set operator.
            if context.mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT') # Go to object mode first to be safe
            bpy.ops.object.select_all(action='DESELECT')
            context.view_layer.objects.active = rig
            rig.select_set(True)
            # Now that the rig is active, we can safely switch to Pose Mode.
            bpy.ops.object.mode_set(mode='POSE')
            # This call will unlock the bones because lsd_placement_mode is now True
            for pb in rig.pose.bones:
                apply_native_constraints(pb)
            self.report({'INFO'}, "Joint placement mode enabled.")
        else: # Exiting placement mode
            context.scene.lsd_placement_mode = False
            # --- AI Editor Note: Robust Context Management ---
            # Ensure the rig is active before performing mode changes or pose operations.
            if context.view_layer.objects.active != rig or rig.mode != 'POSE':
                # Go to object mode first to be safe, e.g. if in edit mode on another object
                if context.mode != 'OBJECT':
                    bpy.ops.object.mode_set(mode='OBJECT')
                bpy.ops.object.select_all(action='DESELECT')
                context.view_layer.objects.active = rig
                rig.select_set(True)
                bpy.ops.object.mode_set(mode='POSE')
            # Apply the current pose as the new rest pose for the armature.
            bpy.ops.pose.armature_apply(selected=False)
            # --- AI Editor Note: Update drivers AFTER applying pose to prevent snapping ---
            # When the rest pose is applied, the offsets in existing drivers become invalid.
            # We must iterate through all mimic relationships and re-apply the driver logic
            # to recalculate the offsets based on the new rest pose.
            for pb in rig.pose.bones:
                if hasattr(pb, 'lsd_pg_kinematic_props') and pb.lsd_pg_kinematic_props.mimic_drivers:
                    for mimic in pb.lsd_pg_kinematic_props.mimic_drivers:
                        # Re-run the driver setup to update the expression with the new offset.
                        # The 'invert' setting is read from the bone's single 'ratio_invert' property,
                        # consistent with how new mimic drivers are created.
                        core.add_native_driver_relation(pb, mimic.target_bone, mimic.ratio, pb.lsd_pg_kinematic_props.ratio_invert)
            if rig.animation_data:
                for d in rig.animation_data.drivers:
                    d.mute = False
            # --- RESTORE CONSTRAINTS ---
            # Now that placement mode is off, re-apply the constraints.
            for pb in rig.pose.bones:
                apply_native_constraints(pb)
            self.report({'INFO'}, "Joint placement mode disabled.")
        context.view_layer.update()
        return {'FINISHED'}
class LSD_OT_CreateRig(bpy.types.Operator):
    bl_idname = "lsd.create_rig"
    bl_label = "Create New Robot"
    def execute(self, context: bpy.types.Context) -> Set[str]:
        base_name = "New_Kinematics"
        name = base_name
        count = 2
        while name in context.scene.objects:
            name = f"{count}_Kinematics"
            count += 1
        armature_data = bpy.data.armatures.new(name + "_Data")
        rig = bpy.data.objects.new(name, armature_data)
        rig.location = context.scene.cursor.location # AI Editor Note: Create rig at cursor location
        context.view_layer.active_layer_collection.collection.objects.link(rig)
        rig.show_in_front = True
        rig.display_type = 'WIRE'
        armature_data.display_type = 'STICK'
        context.scene.lsd_active_rig = rig
        for o in context.selected_objects: o.select_set(False)
        rig.select_set(True)
        context.view_layer.objects.active = rig
        return {'FINISHED'}
class LSD_OT_MergeArmatures(bpy.types.Operator):
    """Merge selected armatures into a new armature workspace"""
    bl_idname = "lsd.merge_armatures"
    bl_label = "Merge Armatures"
    bl_options = {'REGISTER', 'UNDO'}
    @classmethod
    def poll(cls, context):
        return (context.mode == 'OBJECT' and
                context.active_object and
                context.active_object.type == 'ARMATURE' and
                len([o for o in context.selected_objects if o.type == 'ARMATURE']) > 1)
    def execute(self, context: bpy.types.Context) -> Set[str]:
        old_target_rig = context.active_object
        # --- AI Editor Note: ReferenceError Fix ---
        # Store the name of the original rig before it gets deleted. The old_target_rig
        # object reference becomes invalid after the original objects are removed.
        old_target_rig_name = old_target_rig.name
        old_armatures = [o for o in context.selected_objects if o.type == 'ARMATURE']
        # 1. Collect all objects to copy (Armatures + Recursive Children)
        # We use an iterative approach to find all descendants to ensure we get the full hierarchy.
        objects_to_copy = set(old_armatures)
        all_scene_objects = set(context.scene.objects)
        while True:
            children = {o for o in all_scene_objects if o.parent in objects_to_copy and o not in objects_to_copy}
            if not children:
                break
            objects_to_copy.update(children)
        # 2. Duplicate into New Workspace
        bpy.ops.object.select_all(action='DESELECT')
        objects_to_delete = [] # Keep track of objects we successfully select for duplication
        for obj in objects_to_copy:
            # --- AI Editor Note: Fix for disappearing objects ---
            # Blender's duplicate operator ignores hidden objects. We must ensure
            # all parts of the hierarchy are visible and selectable before duplication.
            # Since the originals will be deleted, we don't need to restore their state.
            obj.hide_viewport = False
            obj.hide_set(False)
            obj.hide_select = False
            try:
                obj.select_set(True)
                objects_to_delete.append(obj)
            except RuntimeError:
                # Object is likely not in the current View Layer (e.g. disabled collection).
                # We cannot duplicate it, so we must NOT delete it later to prevent data loss.
                continue
        # Ensure the main rig is active so its copy becomes the new main rig
        context.view_layer.objects.active = old_target_rig
        bpy.ops.object.duplicate(linked=False)
        # The selection now contains the new copies.
        new_target_rig = context.active_object
        new_objects = context.selected_objects
        # Identify the new armatures to be joined (excluding the target)
        new_armatures_to_join = [o for o in new_objects if o.type == 'ARMATURE' and o != new_target_rig]
        # --- AI Editor Note: Identify armatures for joining and remapping ---
        # This set is created before the join operation, so it holds valid references
        # to the armature copies that will be consumed.
        new_armatures_to_join_set = set(new_armatures_to_join)
        # --- AI Editor Note: Pre-Join Bone Renaming ---
        # We must ensure unique bone names BEFORE joining to preserve Vertex Groups and parent relationships.
        # Blender's join operator renames colliding bones (e.g. "Bone" -> "Bone.001") but does NOT
        # update the Vertex Groups or parent_bone references on child meshes, causing them to detach or deform incorrectly.
        existing_bone_names = {b.name for b in new_target_rig.data.bones}
        for rig in new_armatures_to_join:
            # Snapshot of current rig's bone names to avoid collision within itself during renaming
            current_rig_bone_names = {b.name for b in rig.data.bones}
            # Find dependent objects for this rig
            dependent_objects = []
            for obj in new_objects:
                if obj.type == 'MESH':
                    if obj.parent == rig:
                        dependent_objects.append(obj)
                    elif any(m.type == 'ARMATURE' and m.object == rig for m in obj.modifiers):
                        dependent_objects.append(obj)
            for bone in rig.data.bones:
                if bone.name in existing_bone_names:
                    old_name = bone.name
                    new_name = old_name
                    counter = 1
                    while new_name in existing_bone_names or new_name in current_rig_bone_names:
                        new_name = f"{old_name}.{counter:03d}"
                        counter += 1
                    bone.name = new_name
                    existing_bone_names.add(new_name)
                    # Update references in dependent objects
                    for obj in dependent_objects:
                        # Update Vertex Groups
                        vg = obj.vertex_groups.get(old_name)
                        if vg: vg.name = new_name
                        # Update Parent Bone
                        if obj.parent == rig and obj.parent_type == 'BONE' and obj.parent_bone == old_name:
                            obj.parent_bone = new_name
                    # Update Constraints on any new object targeting this rig/bone
                    for obj in new_objects:
                        for con in obj.constraints:
                            if hasattr(con, 'target') and con.target == rig and hasattr(con, 'subtarget') and con.subtarget == old_name:
                                con.subtarget = new_name
                    # Update Drivers
                    for obj in new_objects:
                        if obj.animation_data and obj.animation_data.drivers:
                            for d in obj.animation_data.drivers:
                                for v in d.driver.variables:
                                    for t in v.targets:
                                        if t.id == rig:
                                            if hasattr(t, 'bone_target') and t.bone_target == old_name:
                                                t.bone_target = new_name
                                            if hasattr(t, 'data_path') and f'["{old_name}"]' in t.data_path:
                                                t.data_path = t.data_path.replace(f'["{old_name}"]', f'["{new_name}"]')
                else:
                    existing_bone_names.add(bone.name)
        # 3. Join Armatures
        # Select only the armatures for joining
        bpy.ops.object.select_all(action='DESELECT')
        new_target_rig.select_set(True)
        context.view_layer.objects.active = new_target_rig
        for rig_to_join in new_armatures_to_join:
            rig_to_join.select_set(True)
        bpy.ops.object.join()
        # --- AI Editor Note: Post-Join Pointer Remapping ---
        # After joining, any Drivers or Geometry Nodes on the duplicated objects
        # that pointed to the now-deleted armatures are broken. This loop
        # iterates through all newly created objects and redirects those pointers
        # to the final merged armature, ensuring all procedural setups continue to function.
        for obj in new_objects:
            try:
                # This check is the most robust way to see if the object reference
                # from our list is still valid after the join operation.
                if obj.name not in bpy.data.objects:
                    continue
            except ReferenceError:
                # This handles the case where the object reference itself is invalid.
                continue
            # A. Remap Drivers
            if obj.animation_data and obj.animation_data.drivers:
                for d in obj.animation_data.drivers:
                    for v in d.driver.variables:
                        for t in v.targets:
                            try:
                                # Check if the driver target was one of the armatures we just joined.
                                if t.id in new_armatures_to_join_set:
                                    t.id = new_target_rig
                            except ReferenceError:
                                # The target ID might be an invalid reference itself. Safe to ignore.
                                pass
            # B. Remap Geometry Node Inputs
            for mod in obj.modifiers:
                if mod.type == 'NODES' and mod.node_group:
                    # Iterate through the modifier's interface sockets.
                    for item in mod.node_group.interface.items_tree:
                        if item.item_type == 'SOCKET' and item.socket_type == 'NodeSocketObject':
                            try:
                                # Get the object currently assigned to this input.
                                input_obj = mod[item.identifier]
                                # If it was one of the joined armatures, re-point it.
                                if input_obj in new_armatures_to_join_set:
                                    mod[item.identifier] = new_target_rig
                            except (KeyError, ReferenceError):
                                # This can happen if the input isn't set or the object is gone.
                                # It's safe to ignore these cases.
                                pass
            # C. Remap Constraints
            # Constraints on the new objects might point to the armature copies that are about to be joined/deleted.
            # We redirect them to the new merged rig to prevent broken links.
            for con in obj.constraints:
                if hasattr(con, 'target') and con.target in new_armatures_to_join_set:
                    con.target = new_target_rig
            # D. Remap Modifiers
            # Armature modifiers (and others like Hook, SimpleDeform) might point to the consumed armatures.
            # If these links break, the mesh might jump or disappear.
            for mod in obj.modifiers:
                if hasattr(mod, 'object') and mod.object in new_armatures_to_join_set:
                    mod.object = new_target_rig
                if mod.type == 'ARRAY' and mod.use_object_offset and mod.offset_object in new_armatures_to_join_set:
                    mod.offset_object = new_target_rig
        # 5. Delete Old Data
        # Remove the original objects to complete the "move to workspace" effect
        for obj in objects_to_delete:
            if obj.name in context.scene.objects:
                 bpy.data.objects.remove(obj, do_unlink=True)
        # 6. Finalize
        new_target_rig.name = f"{old_target_rig_name}_Merged"
        context.scene.lsd_active_rig = new_target_rig
        # AI Editor Note: Force a full update of gizmos and constraints on the new rig.
        # This ensures that the merged result is clean and fully functional.
        update_all_gizmos(context.scene, context)
        self.report({'INFO'}, f"Merged into new workspace '{new_target_rig.name}'")
        return {'FINISHED'}
class LSD_OT_PurgeBones(bpy.types.Operator):
    """Removes selected bones and unparents their associated meshes, cleaning up the rig."""
    bl_idname = "lsd.purge_bones"
    bl_label = "Purge Selected Bones / Joints"
    bl_options = {'REGISTER', 'UNDO'}
    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        if context.mode == 'OBJECT':
            return len(context.selected_objects) > 0
        elif context.mode == 'POSE':
            return len(context.selected_pose_bones) > 0
        return False
    def execute(self, context: bpy.types.Context) -> Set[str]:
        rig = context.scene.lsd_active_rig
        bones_to_remove = set()
        objects_to_unparent = []
        # 1. Identify targets based on mode
        if context.mode == 'OBJECT':
            if not rig:
                self.report({'WARNING'}, "No active rig set.")
                return {'CANCELLED'}
            for obj in context.selected_objects:
                if obj.type == 'ARMATURE':
                    # If the armature itself is selected, look for bones selected in its data
                    for b in obj.data.bones:
                        if b.select:
                            bones_to_remove.add(b.name)
                if obj.parent == rig and obj.parent_type == 'BONE' and obj.parent_bone:
                    bones_to_remove.add(obj.parent_bone)
                    objects_to_unparent.append(obj)
        elif context.mode == 'POSE':
            rig = context.object
            for pb in context.selected_pose_bones:
                bones_to_remove.add(pb.name)
                # Find children to unparent so they don't jump
                children = get_all_children_objects(pb, context)
                objects_to_unparent.extend(children)
        if not bones_to_remove:
            self.report({'WARNING'}, "No bones linked to selection found.")
            return {'CANCELLED'}
        # 2. Deletion Process (Simplified: No smart preservation)
        previous_mode = context.mode
        # Switch to Object Mode to ensure clean selection handling
        if context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.select_all(action='DESELECT')
        context.view_layer.objects.active = rig
        rig.select_set(True)
        context.view_layer.objects.active = rig
        rig.select_set(True)
        bpy.ops.object.mode_set(mode='EDIT')
        armature = rig.data
        count = 0
        for bone_name in bones_to_remove:
            eb = armature.edit_bones.get(bone_name)
            if eb:
                armature.edit_bones.remove(eb)
                count += 1
        # 4. Restore context
        if previous_mode == 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
            # Reselect the objects that were unparented
            for obj in objects_to_unparent:
                obj.select_set(True)
            if objects_to_unparent:
                context.view_layer.objects.active = objects_to_unparent[0]
        else:
            bpy.ops.object.mode_set(mode='POSE')
        # --- 5. Cleanup unused gizmos ---
        # AI Editor Note: Garbage collect unused widget objects to keep the scene clean.
        # This ensures that when bones are purged, their visual widgets don't linger if unused.
        used_widgets = set()
        for obj in bpy.data.objects:
            if obj.type == 'ARMATURE':
                for pb in obj.pose.bones:
                    if pb.custom_shape:
                        used_widgets.add(pb.custom_shape)
        widgets_coll = bpy.data.collections.get(WIDGETS_COLLECTION_NAME)
        if widgets_coll:
            widgets_to_remove = [w for w in widgets_coll.objects if w not in used_widgets]
            for widget in widgets_to_remove:
                bpy.data.objects.remove(widget, do_unlink=True)
        self.report({'INFO'}, f"Purged {count} bones and unparented {len(objects_to_unparent)} objects.")
        return {'FINISHED'}
class LSD_OT_ParentToActive(bpy.types.Operator):
    bl_idname = "lsd.parent_to_active"
    bl_label = "Parent Selected to Active"
    bl_options = {'REGISTER', 'UNDO'}
    def execute(self, context: bpy.types.Context) -> Set[str]:
        active_obj = context.active_object
        if not active_obj:
            return {'CANCELLED'}
        rig = None
        parent_bone_name = None
        # Case 1: The active object is the armature itself. The active pose bone is the target parent.
        if active_obj.type == 'ARMATURE':
            rig = active_obj
            parent_bone_name = context.active_pose_bone.name if context.active_pose_bone else None
        # Case 2: The active object is a mesh, likely a custom bone shape.
        # We need to find which bone in which armature is using this mesh.
        elif active_obj.type == 'MESH':
            # Iterate through all armatures in the scene
            for r in [o for o in context.scene.objects if o.type == 'ARMATURE']:
                for bone in r.pose.bones:
                    if bone.custom_shape == active_obj:
                        rig = r
                        parent_bone_name = bone.name
                        break # Found bone, stop searching in this armature
                if rig:
                    break # Found armature, stop searching other armatures
        if not rig or not parent_bone_name:
            return {'CANCELLED'}
        # AI Editor Note: Support both bone-to-bone and mesh-to-bone parenting.
        # Find all selected/sub-selected bones and meshes to parent.
        selected_bones = []
        selected_meshes = set()
        def gather_recursive(o):
            if o.type == 'MESH' and o != rig:
                selected_meshes.add(o)
            for child in o.children:
                gather_recursive(child)
        for obj in context.selected_objects:
            if obj.type == 'ARMATURE' and obj == rig:
                # If armature is selected, use pose bone selection
                for pbone in rig.pose.bones:
                    if pbone.bone.select and pbone.name != parent_bone_name:
                        selected_bones.append(pbone.name)
            else:
                gather_recursive(obj)
        if not selected_bones and not selected_meshes:
            return {'CANCELLED'}
        # 1. Mesh Parenting
        if selected_meshes:
            for mesh in selected_meshes:
                mesh.parent = rig
                mesh.parent_type = 'BONE'
                mesh.parent_bone = parent_bone_name
        # 2. Bone Parenting (requires Edit Mode)
        if selected_bones:
            context.view_layer.objects.active = rig
            bpy.ops.object.mode_set(mode='EDIT')
            eb_parent = rig.data.edit_bones.get(parent_bone_name)
            for name in selected_bones:
                eb_child = rig.data.edit_bones.get(name)
                if eb_child:
                    eb_child.use_connect = False
                    eb_child.parent = eb_parent
            bpy.ops.object.mode_set(mode='POSE')
        self.report({'INFO'}, f"Parented {len(selected_meshes)} meshes and {len(selected_bones)} bones to {parent_bone_name}")
        return {'FINISHED'}
class LSD_OT_EnterPoseMode(bpy.types.Operator):
    bl_idname = "lsd.enter_pose_mode"
    bl_label = "Pose Mode to Set Gizmos"
    def execute(self, context: bpy.types.Context) -> Set[str]:
        # --- AI Editor Note: Context-Sensitive Rig Update ---
        # If the user has selected an armature (or a part of one), update the
        # active rig setting to match. This ensures the tool works on the
        # object the user is currently interacting with.
        target_rig = None
        active_obj = context.active_object
        if active_obj:
            if active_obj.type == 'ARMATURE':
                target_rig = active_obj
            elif active_obj.parent and active_obj.parent.type == 'ARMATURE':
                target_rig = active_obj.parent
        if not target_rig:
             for obj in context.selected_objects:
                if obj.type == 'ARMATURE':
                    target_rig = obj
                    break
        if target_rig:
            context.scene.lsd_active_rig = target_rig
        # --- AI Editor Note: AttributeError Fix ---
        # Ensure a rig exists before proceeding. This prevents 'rig' from being None if no
        # armature has been created or set as active yet.
        rig = ensure_default_rig(context)
        if not rig:
            self.report({'ERROR'}, "Could not find or create an active rig.")
            return {'CANCELLED'}
        # AI Editor Note: Smart bone selection.
        # If the active object is a mesh parented to a bone, select that bone upon entering Pose Mode.
        target_bone_name = None
        if active_obj and active_obj.parent == rig and active_obj.parent_type == 'BONE':
            target_bone_name = active_obj.parent_bone
        # AI Editor Note: Use manual deselection to avoid operator context errors.
        for o in context.view_layer.objects:
            o.select_set(False)
        context.view_layer.objects.active = rig
        rig.select_set(True)
        bpy.ops.object.mode_set(mode='POSE')
        if target_bone_name:
            pbone = rig.pose.bones.get(target_bone_name)
            if pbone:
                rig.data.bones.active = pbone.bone
                pbone.bone.select = True
        return {'FINISHED'}
class LSD_OT_EnterObjectMode(bpy.types.Operator):
    bl_idname = "lsd.enter_object_mode"
    bl_label = "Object Mode"
    def execute(self, context: bpy.types.Context) -> Set[str]:
        bpy.ops.object.mode_set(mode='OBJECT')
        return {'FINISHED'}
def _calculate_bone_geometry(objs: List[bpy.types.Object], axis_orient: str, reference_obj: bpy.types.Object = None, unit_scale: float = 1.0) -> Tuple[mathutils.Vector, mathutils.Vector, mathutils.Vector, float]:
    """
    Calculates the head, tail, and roll for a bone based on one or more mesh objects.
    AI Editor Note: Support for multiple objects ensures that complex links made of
    several meshes (sub-selections) are correctly encompassed by a single bone.
    """
    if not isinstance(objs, (list, tuple, set)):
        objs = [objs]
    if not reference_obj:
        reference_obj = objs[0]
    mat = reference_obj.matrix_world
    inv_mat = mat.inverted()
    # 1. Build a combined bounding box in the reference object's local space
    all_points_local = []
    for o in objs:
        o_mat = o.matrix_world
        # Include all bbox corners if available (Meshes)
        if hasattr(o, 'bound_box') and o.bound_box:
            for b in o.bound_box:
                v_world = o_mat @ mathutils.Vector(b)
                all_points_local.append(inv_mat @ v_world)
        else:
            # Fallback to origin for objects without geometry (Empties)
            all_points_local.append(inv_mat @ o_mat.translation)
    if not all_points_local:
        return mat.translation, mat.translation + mathutils.Vector((0, 0.1, 0)), mathutils.Vector((0, 0, 1)), 0.1
    # 2. Extract head and orientation logic
    head_world = mat.translation
    tail_world: mathutils.Vector
    roll_vec_world: mathutils.Vector
    if axis_orient == 'AUTO':
        # --- Logic for Local Z-Axis Alignment ---
        y_vec_local = mathutils.Vector((0, 0, 1.0))
        z_vec_local = mathutils.Vector((1.0, 0, 0)) # Roll around local X
        # Calculate initial tail and roll in world space
        max_dist = max((p.dot(y_vec_local) for p in all_points_local), default=0.1)
        if max_dist < 0.001: max_dist = 0.1
        tail_local = y_vec_local * max_dist
        initial_tail_world = mat @ tail_local
        initial_roll_vec_world = mat.to_3x3() @ z_vec_local
        # Get the bone's initial orientation vectors in world space.
        y_vec_world = (initial_tail_world - head_world).normalized()
        z_vec_world = initial_roll_vec_world.normalized()
        x_vec_world = y_vec_world.cross(z_vec_world).normalized()
        # Initial orientation matrix (local to world)
        orient_mat = mathutils.Matrix((x_vec_world, y_vec_world, z_vec_world)).transposed()
        final_orient_mat = orient_mat
        # Decompose the final matrix to get the new world-space direction and roll vectors.
        new_y_vec_world = final_orient_mat.col[1]
        new_z_vec_world = final_orient_mat.col[2]
        # Recalculate the tail position and roll vector based on the new orientation.
        bone_length = (initial_tail_world - head_world).length
        tail_world = head_world + bone_length * new_y_vec_world
        roll_vec_world = new_z_vec_world
    else:
        # --- Logic for Local X, Y, Z Alignment ---
        is_neg = axis_orient.startswith("-")
        axis_char = axis_orient.replace("-", "")
        # Define bone's primary axis (Y) and roll axis (Z) in the REFERENCE's LOCAL space.
        if axis_char == 'X':
            y_vec_local = mathutils.Vector((1, 0, 0))
            roll_vec_local = mathutils.Vector((0, 0, 1))
        elif axis_char == 'Y':
            y_vec_local = mathutils.Vector((0, 1, 0))
            roll_vec_local = mathutils.Vector((1, 0, 0))
        else: # 'Z'
            y_vec_local = mathutils.Vector((0, 0, 1))
            roll_vec_local = mathutils.Vector((0, 1, 0))
        if is_neg:
            y_vec_local *= -1.0
        # Transform these local direction vectors into world space
        obj_rot_mat = mat.to_3x3()
        y_vec_world = (obj_rot_mat @ y_vec_local).normalized()
        roll_vec_world = (obj_rot_mat @ roll_vec_local).normalized()
        # 3. Calculate length from combined points projected onto the axis
        # Project all world points onto the bone's direction vector
        origin_proj = head_world.dot(y_vec_world)
        projections = [(mat @ p).dot(y_vec_world) for p in all_points_local]
        max_extent = max(p - origin_proj for p in projections)
        if max_extent < 0.001:
            length = max(projections) - min(projections)
        else:
            length = max_extent
        if length < 0.001: length = 0.01
        tail_world = head_world + length * y_vec_world
    # --- Final Radius Calculation (common for all modes) ---
    bone_axis_world = (tail_world - head_world).normalized()
    max_radius = 0.0
    for p_local in all_points_local:
        v_world = mat @ p_local
        vec_to_point = v_world - head_world
        proj_len = vec_to_point.dot(bone_axis_world)
        proj_vec = proj_len * bone_axis_world
        dist = (vec_to_point - proj_vec).length
        if dist > max_radius:
            max_radius = dist
    if max_radius < 0.001:
        max_radius = 0.05 / (unit_scale if unit_scale > 0 else 1.0) # Ensure 0.05m fallback
    return head_world, tail_world, roll_vec_world, max_radius * unit_scale
def _process_bones_in_edit_mode(rig: bpy.types.Object, bones_to_create: List[Tuple[str, mathutils.Vector, mathutils.Vector, mathutils.Vector]]) -> None:
    """
    Creates or updates a batch of bones in a single Edit Mode session.
    :param rig: The armature object.
    :param bones_to_create: A list of tuples, each containing
                            (bone_name, head_world, tail_world, roll_vec_world).
    """
    bpy.ops.object.mode_set(mode='EDIT')
    rig_mat_inv = rig.matrix_world.inverted()
    for b_name, head, tail, roll_vec in bones_to_create:
        eb = rig.data.edit_bones.get(b_name) or rig.data.edit_bones.new(b_name)
        eb.head = rig_mat_inv @ head
        eb.tail = rig_mat_inv @ tail
        # --- FIX: The roll vector must be in the armature's local space ---
        # The incoming roll_vec is in world space. We must transform it into the
        # armature's local coordinate system before passing it to align_roll.
        roll_vec_armature = rig_mat_inv.to_3x3() @ roll_vec
        eb.align_roll(roll_vec_armature)
        # AI Editor Note: Removed the forced -90 degree Z-rotation.
        # The user requested that the bone generation should reference the target object's
        # local axis orientation. The previous rotation was offsetting the bone
        # from the calculated alignment (which matches the object's local axes).
        # By removing this, the bone's Y-axis will correctly align with the
        # object's local axis determined in `_calculate_bone_geometry`.
def _process_bones_in_pose_mode(rig: bpy.types.Object, bones_to_process: list, context: bpy.types.Context) -> None:
    """
    Sets properties and parents meshes in a single Pose Mode session.
    :param rig: The armature object.
    :param bones_to_process: A list of dicts, each containing 'name', 'objs_data', 'radius', 'axis'.
                             'objs_data' is a list of (obj, original_matrix).
    :param context: The current Blender context.
    """
    bpy.ops.object.mode_set(mode='POSE')
    for b_info in bones_to_process:
        b_name = b_info['name']
        axis = b_info['axis']
        radius = b_info['radius']
        objs_data = b_info['objs_data']
        pbone = rig.pose.bones.get(b_name)
        if pbone:
            # AI Editor Note: Set the bone's axis property to match the alignment axis.
            pbone.lsd_pg_kinematic_props.axis_alignment = 'Z' if axis == 'AUTO' else axis
            pbone.lsd_pg_kinematic_props.joint_type = 'none'
            pbone.lsd_pg_kinematic_props.joint_radius = radius
            update_single_bone_gizmo(pbone, context.scene.lsd_viz_gizmos, context.scene.lsd_gizmo_style)
            # Parent all meshes in the group to the bone while keeping their world transforms
            for obj, original_matrix in objs_data:
                obj.parent = rig
                obj.parent_type = 'BONE'
                obj.parent_bone = b_name
                obj.matrix_world = original_matrix
class LSD_OT_AddBone(bpy.types.Operator):
    bl_idname = "lsd.add_bone"
    bl_label = "Set / Align Bones"
    bl_options = {'REGISTER', 'UNDO'}
    def execute(self, context: bpy.types.Context) -> Set[str]:
        """
        Creates or updates bones based on selected mesh objects.
        AI Editor Note:
        This operator demonstrates a best-practice pattern for writing robust
        Blender operators that perform complex actions. It avoids common errors
        related to changing context (e.g., switching modes) while iterating.
        The process is broken into clear, safe stages:
        1.  **Gather Data**: All necessary information (object geometry, desired
            bone names, etc.) is collected in Object Mode *before* any changes
            are made. This avoids issues with accessing data that might become
            invalid after a mode change.
        2.  **Batch Edit Mode Operations**: The operator switches to Edit Mode
            *once*, creates or updates all bones in a single batch, and then
            switches out.
        3.  **Batch Pose Mode Operations**: It then switches to Pose Mode *once*
            to set all properties, constraints, and parenting relationships.
        4.  **Restore Context**: Finally, it restores the user's original selection
            and active object, providing a seamless user experience.
        """
        # Collect base targets from selection (excluding armatures)
        base_targets = [obj for obj in context.selected_objects if obj.type != 'ARMATURE']
        # Helper to gather all descendants recursively
        def gather_recursive(o, obj_set):
            obj_set.add(o)
            for child in o.children:
                gather_recursive(child, obj_set)
        rig = context.scene.lsd_active_rig
        # Sub-selection Support: Check for meshes parented to selected pose bones if we are in Pose Mode.
        bone_to_mesh_map = {}
        if context.mode == 'POSE' and context.selected_pose_bones:
            for pbone in context.selected_pose_bones:
                meshes = get_all_children_objects(pbone, context)
                if meshes:
                    bone_to_mesh_map[pbone.name] = meshes
        if not base_targets and not bone_to_mesh_map:
            self.report({'WARNING'}, "Select at least one object (or bone with meshes) and set an active rig.")
            return {'CANCELLED'}
        if not rig or rig.name not in context.view_layer.objects:
            # Try to get rig from active object if scene property is not set or invalid
            if context.active_object and context.active_object.type == 'ARMATURE':
                rig = context.active_object
            elif context.mode == 'POSE' and context.object and context.object.type == 'ARMATURE':
                rig = context.object
            # If still not found, search selected objects for an armature
            if not rig or rig.name not in context.view_layer.objects:
                for obj in context.selected_objects:
                    if obj.type == 'ARMATURE':
                        rig = obj
                        break
            # If still not found, search children of selected targets for their parent rig
            if not rig or rig.name not in context.view_layer.objects:
                for obj in base_targets:
                    if obj.parent and obj.parent.type == 'ARMATURE' and obj.parent.name in context.view_layer.objects:
                        rig = obj.parent
                        break
            if not rig or rig.name not in context.view_layer.objects:
                rig_name = rig.name if rig else "None"
                self.report({'ERROR'}, f"Active rig '{rig_name}' not found in current ViewLayer. Select an armature or set it in the Kinematics panel.")
                return {'CANCELLED'}
            # Update the scene property to the valid one we found
            context.scene.lsd_active_rig = rig
        # AI Editor Note: Force update to ensure all object matrices are fresh before calculation.
        context.view_layer.update()
        # --- 1. Gather Geometry Data ---
        bones_to_process = []
        axis_for_alignment = context.scene.lsd_bone_axis
        unit_scale = context.scene.unit_settings.scale_length
        if base_targets:
            mode = context.scene.lsd_bone_mode
            groups = [] # List of tuples: (reference_obj, [all_objs_in_group])
            if mode == 'SINGLE':
                # All selected objects and their descendants belong to one single group
                all_objects = set()
                for obj in base_targets:
                    gather_recursive(obj, all_objects)
                active_obj = context.active_object
                reference = active_obj if active_obj in all_objects else list(all_objects)[0]
                groups.append((reference, list(all_objects)))
            else: # 'INDIVIDUAL'
                # Find top-level selected objects (ignore a selected object if its parent is also selected)
                top_level_targets = []
                for obj in base_targets:
                    curr = obj.parent
                    is_sub_selected = False
                    while curr:
                        if curr in base_targets:
                            is_sub_selected = True
                            break
                        curr = curr.parent
                    if not is_sub_selected:
                        top_level_targets.append(obj)
                # Each top-level selected object forms a group along with its full hierarchy
                for root_obj in top_level_targets:
                    group_objs = set()
                    gather_recursive(root_obj, group_objs)
                    groups.append((root_obj, list(group_objs)))
            for reference, group_objs in groups:
                head, tail, roll, radius = _calculate_bone_geometry(group_objs, axis_for_alignment, reference, unit_scale)
                # Reuse bone name if already parented
                b_name = reference.parent_bone if reference.parent == rig and reference.parent_type == 'BONE' else f"Bone_{reference.name.replace('.', '_')}"
                objs_data = [(o, o.matrix_world.copy()) for o in group_objs]
                bones_to_process.append({
                    'name': b_name,
                    'head': head,
                    'tail': tail,
                    'roll': roll,
                    'objs_data': objs_data,
                    'radius': radius,
                    'axis': axis_for_alignment
                })
        if bone_to_mesh_map:
            # Handle bone sub-selection: Align each selected bone to its respective meshes
            for b_name, meshes in bone_to_mesh_map.items():
                if any(b['name'] == b_name for b in bones_to_process):
                    continue
                primary_obj = meshes[0]
                head, tail, roll, radius = _calculate_bone_geometry(meshes, axis_for_alignment, primary_obj, unit_scale)
                objs_data = [(o, o.matrix_world.copy()) for o in meshes]
                bones_to_process.append({
                    'name': b_name,
                    'head': head,
                    'tail': tail,
                    'roll': roll,
                    'objs_data': objs_data,
                    'radius': radius,
                    'axis': axis_for_alignment
                })
        if not bones_to_process:
            return {'CANCELLED'}
        # --- 3. Perform Operations in Batches ---
        original_mode = rig.mode
        original_active = context.view_layer.objects.active
        # AI Editor Note: Use manual de-selection to avoid operator 'poll' failures in certain contexts.
        for obj in context.view_layer.objects:
            obj.select_set(False)
        context.view_layer.objects.active = rig
        rig.select_set(True)
        # Batch 1: Edit Mode
        edit_mode_data = [(b['name'], b['head'], b['tail'], b['roll']) for b in bones_to_process]
        _process_bones_in_edit_mode(rig, edit_mode_data)
        # Batch 2: Pose Mode
        # AI Editor Note: Force a mode switch to OBJECT and an update first.
        bpy.ops.object.mode_set(mode='OBJECT')
        context.view_layer.update()
        _process_bones_in_pose_mode(rig, bones_to_process, context)
        # --- 4. Switch to Pose Mode and sync selection ---
        context.view_layer.objects.active = rig
        bpy.ops.object.mode_set(mode='POSE')
        # AI Editor Note: Ensure the last bone added is both active and selected.
        # This allows the following read_joint_settings call to succeed and populate the UI.
        if bones_to_process:
            last_b_name = bones_to_process[-1]['name']
            pb = rig.pose.bones.get(last_b_name)
            if pb:
                rig.data.bones.active = pb.bone
                pb.bone.select = True
        # AI Editor Note: Sync tool settings with the newly created active bone.
        # This solves the 'initial radius not recognized' bug by ensuring the sidebar
        # immediately reflects the physical dimensions calculated from geometry.
        bpy.ops.lsd.read_joint_settings()
        self.report({'INFO'}, f"Processed {len(bones_to_process)} bones.")
        return {'FINISHED'}
class LSD_OT_ApplyRestPose(bpy.types.Operator):
    """Applies the current pose as the new rest pose for the entire armature, updating all drivers to maintain relationships."""
    bl_idname = "lsd.apply_rest_pose"
    bl_label = "Apply as Rest Pose"
    bl_description = "Apply current pose as rest pose (Armature) and apply Scale/Rotation (Meshes)"
    bl_options = {'REGISTER', 'UNDO'}
    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return context.scene.lsd_active_rig is not None or len(context.selected_objects) > 0
    def execute(self, context: bpy.types.Context) -> Set[str]:
        # 1. Apply Scale & Rotation to selected Mesh objects (including sub-selected hierarchies)
        all_meshes = set()
        def gather_meshes(o):
            if o.type == 'MESH':
                all_meshes.add(o)
            for child in o.children:
                gather_meshes(child)
        for obj in context.selected_objects:
            gather_meshes(obj)
        selected_meshes = list(all_meshes)
        if selected_meshes:
            if context.mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT')
            bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
            self.report({'INFO'}, f"Applied transforms to {len(selected_meshes)} meshes.")
        # 2. Apply Rest Pose to Active Rig
        rig = context.scene.lsd_active_rig
        if rig and rig.name in context.view_layer.objects:
            prev_mode = context.mode
            prev_active = context.view_layer.objects.active
            if context.mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT')
            bpy.ops.object.select_all(action='DESELECT')
            context.view_layer.objects.active = rig
            rig.select_set(True)
            bpy.ops.object.mode_set(mode='POSE')
            bpy.ops.pose.armature_apply(selected=False)
            for bone in rig.pose.bones:
                if hasattr(bone, 'lsd_pg_kinematic_props') and bone.lsd_pg_kinematic_props.mimic_drivers:
                    for mimic in bone.lsd_pg_kinematic_props.mimic_drivers:
                        core.add_native_driver_relation(bone, mimic.target_bone, mimic.ratio, bone.lsd_pg_kinematic_props.ratio_invert)
            bpy.ops.object.mode_set(mode='OBJECT')
            if prev_active and prev_active.name in context.view_layer.objects:
                context.view_layer.objects.active = prev_active
                prev_active.select_set(True)
            for m in selected_meshes:
                m.select_set(True)
            if prev_mode == 'POSE' and prev_active == rig:
                bpy.ops.object.mode_set(mode='POSE')
            self.report({'INFO'}, "Applied Rest Pose to Armature.")
        return {'FINISHED'}
# ------------------------------------------------------------------------

class LSD_OT_LightTarget(bpy.types.Operator):
    """
    Constrains selected lights to point specifically at the last selected object center.
    AI Editor Note: This uses a Damped Track constraint to ensure the light follows
    the target dynamically as it moves.
    """
    bl_idname = "lsd.light_target"
    bl_label = "Target Active Object"
    bl_options = {'REGISTER', 'UNDO'}
    @classmethod
    def poll(cls, context):
        props = context.scene.lsd_pg_lighting_props
        # Enabled if eyedropper has target OR anything is selected in viewport
        return (props.selected_light_target is not None or
                len(context.selected_objects) >= 1)
    def execute(self, context):
        props = context.scene.lsd_pg_lighting_props
        # Priority 1: Eyedropper target
        # Priority 2: Active object
        target = props.selected_light_target or context.active_object
        if not target:
            self.report({'ERROR'}, "Select a target via eyedropper or pick one in the viewport.")
            return {'CANCELLED'}
        count = 0
        for obj in context.selected_objects:
            if obj == target or obj.type != 'LIGHT':
                continue
            # Remove existing tracking if any
            for c in obj.constraints:
                if c.type in ['DAMPED_TRACK', 'TRACK_TO']:
                    obj.constraints.remove(c)
            # Add Damped Track
            # -Z is the standard direction for Blender lights (Spot, Area, Sun)
            dt = obj.constraints.new('DAMPED_TRACK')
            dt.target = target
            dt.track_axis = 'TRACK_NEGATIVE_Z'
            count += 1
        self.report({'INFO'}, f"Targeted {count} lights to {target.name}")
        return {'FINISHED'}
class LSD_OT_ApplyToonShader(bpy.types.Operator):
    """
    Applies a 'Shader to RGB' node setup to selected objects to remove light gradients.
    Note: Requires EEVEE renderer to work properly.
    """
    bl_idname = "lsd.apply_toon_shader"
    bl_label = "Apply Toon Shader"
    bl_options = {'REGISTER', 'UNDO'}
    @classmethod
    def poll(cls, context):
        return any(o.type == 'MESH' for o in context.selected_objects)
    def execute(self, context):
        count = 0
        for obj in context.selected_objects:
            if obj.type != 'MESH': continue
            # Ensure at least one material slot exists
            if not obj.material_slots:
                obj.data.materials.append(bpy.data.materials.new(name="Toon_Material"))
            for slot in obj.material_slots:
                mat = slot.material
                if not mat:
                    mat = bpy.data.materials.new(name="Toon_Material")
                    slot.material = mat
                mat.use_nodes = True
                nodes = mat.node_tree.nodes
                links = mat.node_tree.links
                # Avoid duplicates
                if any(n.name == "Toon_ColorRamp" for n in nodes):
                    count += 1
                    continue
                # Get Output or create one
                output_node = next((n for n in nodes if n.type == 'OUTPUT_MATERIAL'), None)
                if not output_node:
                    output_node = nodes.new('ShaderNodeOutputMaterial')
                    output_node.location = (400, 0)
                # Find a shader to work with - find existing link or create Principled BSDF
                source_socket = None
                surface_input = output_node.inputs['Surface']
                if surface_input.links:
                    source_socket = surface_input.links[0].from_socket
                else:
                    shader_node = next((n for n in nodes if n.type in {'BSDF_PRINCIPLED', 'BSDF_DIFFUSE'}), None)
                    if not shader_node:
                        shader_node = nodes.new('ShaderNodeBsdfPrincipled')
                        shader_node.location = (-800, 0)
                    source_socket = shader_node.outputs[0]
                if not source_socket: continue
                # --- TOON MASTER SETUP ---
                # 1. Shader to RGB (Captures Light/Shadow Data)
                s_to_rgb = nodes.new('ShaderNodeShaderToRGB')
                s_to_rgb.location = (output_node.location.x - 700, output_node.location.y)
                # 2. Color Ramp (The Hard Terminator with 30% shadow floor)
                ramp = nodes.new('ShaderNodeValToRGB')
                ramp.name = "Toon_ColorRamp"
                ramp.label = "Toon Mask"
                ramp.location = (output_node.location.x - 450, output_node.location.y)
                ramp.color_ramp.interpolation = 'CONSTANT'
                # First stop: 30% visibility for ambient detail
                ramp.color_ramp.elements[0].color = (0.3, 0.3, 0.3, 1.0)
                ramp.color_ramp.elements[0].position = 0.5
                # Second stop: 100% visibility for lit areas
                ramp.color_ramp.elements[1].color = (1.0, 1.0, 1.0, 1.0)
                # 3. Mix (Multiply Light Mask with Base Color)
                mix = nodes.new('ShaderNodeMix')
                mix.data_type = 'RGBA'
                mix.blend_type = 'MULTIPLY'
                mix.location = (output_node.location.x - 200, output_node.location.y)
                mix.inputs[0].default_value = 1.0 # Factor 100%
                # Check for existing color nodes to plug into A
                base_color_node = next((n for n in nodes if n.type == 'RGB' or n.name == "Base Color"), None)
                if base_color_node:
                    links.new(base_color_node.outputs[0], mix.inputs[6])
                else:
                    mix.inputs[6].default_value = (1.0, 1.0, 1.0, 1.0)
                # --- Link the chain ---
                links.new(source_socket, s_to_rgb.inputs[0])
                links.new(s_to_rgb.outputs[0], ramp.inputs[0])
                links.new(ramp.outputs[0], mix.inputs[7]) # Slot B
                links.new(mix.outputs[2], surface_input)
                count += 1
        self.report({'INFO'}, f"Applied Ambient-Friendly Toon setup to {count} materials.")
        return {'FINISHED'}
class LSD_OT_GlobalToonSharpness(bpy.types.Operator):
    """
    Sets all lights in the scene to sharp shadows and zero specular for tooning.
    """
    bl_idname = "lsd.global_toon_sharpness"
    bl_label = "Set Global Sharpness"
    bl_options = {'REGISTER', 'UNDO'}
    mode: bpy.props.EnumProperty(
        items=[('TOON', "Toon", ""), ('REALISTIC', "Realistic", "")]
    )
    def execute(self, context):
        count = 0
        for light in bpy.data.lights:
            if self.mode == 'TOON':
                if hasattr(light, 'shadow_soft_size'): light.shadow_soft_size = 0.0
                if hasattr(light, 'radius'): light.radius = 0.0
                if hasattr(light, 'angle'): light.angle = 0.0
                if hasattr(light, 'specular_factor'): light.specular_factor = 0.0
                if hasattr(light, 'shadow_buffer_bias'): light.shadow_buffer_bias = 0.1
                if hasattr(light, 'use_contact_shadow'): light.use_contact_shadow = True
            else:
                if hasattr(light, 'shadow_soft_size'): light.shadow_soft_size = 0.1
                if hasattr(light, 'radius'): light.radius = 0.2
                if hasattr(light, 'angle'): light.angle = 0.1
                if hasattr(light, 'specular_factor'): light.specular_factor = 1.0
                if hasattr(light, 'shadow_buffer_bias'): light.shadow_buffer_bias = 0.05
                if hasattr(light, 'use_contact_shadow'): light.use_contact_shadow = False
            count += 1
        self.report({'INFO'}, f"Updated {count} lights to {self.mode.lower()} mode.")
        return {'FINISHED'}
class LSD_OT_ToonifySelectedLights(bpy.types.Operator):
    """
    Sets selected light sources to sharp 'Toon' style lighting.
    """
    bl_idname = "lsd.toonify_selected_lights"
    bl_label = "Toonify Selected Lights"
    bl_options = {'REGISTER', 'UNDO'}
    def execute(self, context):
        count = 0
        for obj in context.selected_objects:
            if obj.type == 'LIGHT':
                light = obj.data
                if hasattr(light, 'shadow_soft_size'): light.shadow_soft_size = 0.0
                if hasattr(light, 'radius'): light.radius = 0.0
                if hasattr(light, 'specular_factor'): light.specular_factor = 0.0
                if hasattr(light, 'shadow_buffer_bias'): light.shadow_buffer_bias = 0.1
                if hasattr(light, 'use_contact_shadow'): light.use_contact_shadow = True
                if hasattr(light, 'spot_blend'): light.spot_blend = 0.0
                count += 1
        self.report({'INFO'}, f"Toonified {count} light sources.")
        return {'FINISHED'}
# ------------------------------------------------------------------------

#   OPERATORS: CAMERA CINEMATOGRAPHY

# ------------------------------------------------------------------------

class LSD_OT_CreateCamera(bpy.types.Operator):
    """Creates a new Blueprint-ready camera based on selected preset."""
    bl_idname = "lsd.create_camera"
    bl_label = "Spawn Camera"
    bl_options = {'REGISTER', 'UNDO'}
    def execute(self, context):
        scene = context.scene
        preset = scene.lsd_camera_preset
        # Create camera data
        cam_data = bpy.data.cameras.new(name="LSD_Camera")
        # Apply Preset
        if preset == '70MM':
            cam_data.lens = 70.0
        elif preset == '16MM':
            cam_data.lens = 16.0
        elif preset == '8MM':
            cam_data.lens = 8.0
        else: # 35MM
            cam_data.lens = 35.0
        # Create object
        cam_obj = bpy.data.objects.new(name="Camera_Blueprint", object_data=cam_data)
        context.collection.objects.link(cam_obj)
        # Position at cursor
        cam_obj.location = scene.cursor.location
        cam_obj.rotation_euler = (math.radians(90), 0, 0) # Level horizon
        # Select and Activate
        for o in context.view_layer.objects.selected:
            o.select_set(False)
        cam_obj.select_set(True)
        context.view_layer.objects.active = cam_obj
        # Initialize LSD properties
        cam_obj.lsd_pg_mech_props.is_part = True
        cam_obj.lsd_pg_mech_props.category = 'NONE' # It's a tool
        # Synchronize LSD mirror props to camera data
        cam_obj.lsd_pg_mech_props.camera_focal_length = cam_data.lens
        # Switch to the new camera immediately
        scene.camera = cam_obj
        self.report({'INFO'}, f"Spawned {preset} Camera at cursor.")
        return {'FINISHED'}

class LSD_OT_Camera_Setup(bpy.types.Operator):
    """Binds camera to targets and paths for high-fidelity simulation recording."""
    bl_label = "Apply Camera Setup"
    bl_idname = "lsd.camera_setup"
    bl_options = {'REGISTER', 'UNDO'}
    def execute(self, context):
        cam_obj = context.active_object
        if not cam_obj or cam_obj.type != 'CAMERA':
             self.report({'ERROR'}, "Invalid Camera Object Selection")
             return {'CANCELLED'}
        props = cam_obj.lsd_pg_mech_props
        # 1. Lens Properties Synchronization
        cam_obj.data.lens = props.camera_focal_length
        cam_obj.data.dof.use_dof = props.camera_dof_enabled
        if props.camera_dof_enabled:
            cam_obj.data.dof.aperture_fstop = props.camera_fstop
            if props.camera_target:
                cam_obj.data.dof.focus_object = props.camera_target
        # 2. Path Kinematics (Follow Path)
        if props.camera_path:
             c_path = next((c for c in cam_obj.constraints if c.type == 'FOLLOW_PATH'), None)
             if not c_path:
                  c_path = cam_obj.constraints.new('FOLLOW_PATH')
             c_path.target = props.camera_path
             c_path.use_fixed_location = True # Essential for absolute path driving
             # Driver Implementation: Connect custom offset to internal constraint factor
             if not c_path.driver_add("offset_factor").driver:
                  fcurve = c_path.driver_add("offset_factor")
                  driver = fcurve.driver
                  driver.type = 'AVERAGE'
                  var = driver.variables.new()
                  var.name = "offset"
                  var.type = 'SINGLE_PROP'
                  var.targets[0].id = cam_obj
                  var.targets[0].data_path = "lsd_pg_mech_props.camera_path_offset"
                  driver.expression = "offset"
        # 3. Track To / Lock View (Look At Target)
        if props.camera_target:
             c_look = next((c for c in cam_obj.constraints if c.type == 'TRACK_TO'), None)
             if not c_look:
                  c_look = cam_obj.constraints.new('TRACK_TO')
             c_look.target = props.camera_target
             c_look.track_axis = 'TRACK_NEGATIVE_Z'
             c_look.up_axis = 'UP_Y'
             c_look.target_space = 'WORLD'
             c_look.owner_space = 'WORLD'
        return {'FINISHED'}
class LSD_OT_Camera_Look_Through(bpy.types.Operator):
    """Makes the active camera the primary scene viewport."""
    bl_label = "View Through Camera"
    bl_idname = "lsd.camera_look_through"
    def execute(self, context):
        if context.active_object and context.active_object.type == 'CAMERA':
            context.scene.camera = context.active_object
            # Switch the viewport perspective immediately
            for area in context.screen.areas:
                if area.type == 'VIEW_3D':
                    area.spaces.active.region_3d.view_perspective = 'CAMERA'
        return {'FINISHED'}
class LSD_OT_AccurateScale(bpy.types.Operator):
    """Accurately set the scale of the selected object on the chosen axes."""
    bl_idname = "lsd.accurate_scale"
    bl_label = "Apply Scale"
    bl_options = {'REGISTER', 'UNDO'}
    def execute(self, context):
        import mathutils
        scene = context.scene
        axes = scene.lsd_scale_axes
        target_dim = scene.lsd_scale_value
        scale_mode = getattr(scene, 'lsd_scale_mode', 'INDIVIDUAL')
        pivot_mode = getattr(scene, 'lsd_scale_pivot', 'ORIGIN')
        # Determine Center Point in World Space
        pivot_world = mathutils.Vector((0,0,0))
        if pivot_mode == 'CURSOR':
            pivot_world = scene.cursor.location
        if context.mode == 'EDIT_MESH':
            # --- EDIT MODE DRAFTING ---
            import bmesh
            for obj in context.objects_in_mode:
                if obj.type != 'MESH': continue
                bm = bmesh.from_edit_mesh(obj.data)
                verts = [v for v in bm.verts if v.select]
                if not verts: continue
                # Calculate Bounding Box of selected vertices in World Space
                min_v = mathutils.Vector((float('inf'),)*3)
                max_v = mathutils.Vector((float('-inf'),)*3)
                for v in verts:
                    world_co = obj.matrix_world @ v.co
                    for i in range(3):
                        min_v[i] = min(min_v[i], world_co[i])
                        max_v[i] = max(max_v[i], world_co[i])
                current_dims = max_v - min_v
                center_world = (min_v + max_v) / 2.0
                # Define Scale Center based on Pivot Mode
                active_pivot = center_world if pivot_mode == 'ORIGIN' else pivot_world
                local_pivot = obj.matrix_world.inverted() @ active_pivot
                scale_vec = mathutils.Vector((1.0, 1.0, 1.0))
                for i in range(3):
                    if axes[i] and current_dims[i] > 1e-8:
                        scale_vec[i] = target_dim / current_dims[i]
                # Apply transformation around the local pivot
                bmesh.ops.scale(bm, verts=verts, vec=scale_vec, space=mathutils.Matrix.Translation(-local_pivot))
                bmesh.update_edit_mesh(obj.data)
        else:
            # --- OBJECT MODE DRAFTING ---
            targets = context.selected_objects
            if not targets:
                return {'CANCELLED'}
            if scale_mode == 'GROUP' and len(targets) > 0:
                # Scale entire selection as a single mechatronic unit
                min_v = mathutils.Vector((float('inf'),)*3)
                max_v = mathutils.Vector((float('-inf'),)*3)
                for o in targets:
                    for box_corner in o.bound_box:
                        world_corner = o.matrix_world @ mathutils.Vector(box_corner)
                        for i in range(3):
                            min_v[i] = min(min_v[i], world_corner[i])
                            max_v[i] = max(max_v[i], world_corner[i])
                total_dims = max_v - min_v
                group_center = (min_v + max_v) / 2.0
                active_pivot = group_center if pivot_mode == 'ORIGIN' else pivot_world
                scale_vec = mathutils.Vector((1.0, 1.0, 1.0))
                for i in range(3):
                    if axes[i] and total_dims[i] > 1e-8:
                        scale_vec[i] = target_dim / total_dims[i]
                for o in targets:
                    # Translation relative to the pivot
                    rel_pos = o.matrix_world.translation - active_pivot
                    rel_pos.x *= scale_vec.x
                    rel_pos.y *= scale_vec.y
                    rel_pos.z *= scale_vec.z
                    o.matrix_world.translation = active_pivot + rel_pos
                    # Scale
                    o.scale.x *= scale_vec.x
                    o.scale.y *= scale_vec.y
                    o.scale.z *= scale_vec.z
            else:
                # Individual scaling
                for obj in targets:
                    # Individual scaling usually happens on own origin if pivot is ORIGIN
                    # or on cursor if pivot is CURSOR.
                    if pivot_mode == 'CURSOR':
                        # Need to move origin too
                        rel_pos = obj.matrix_world.translation - pivot_world
                        sv = mathutils.Vector((1,1,1))
                        if obj.dimensions.x > 0.00000001 and axes[0]: sv.x = target_dim / obj.dimensions.x
                        if obj.dimensions.y > 0.00000001 and axes[1]: sv.y = target_dim / obj.dimensions.y
                        if obj.dimensions.z > 0.00000001 and axes[2]: sv.z = target_dim / obj.dimensions.z
                        rel_pos.x *= sv.x; rel_pos.y *= sv.y; rel_pos.z *= sv.z
                        obj.matrix_world.translation = pivot_world + rel_pos
                        obj.scale.x *= sv.x; obj.scale.y *= sv.y; obj.scale.z *= sv.z
                    else:
                        # Pure origin-based scaling
                        if obj.dimensions.x > 0.00000001 and axes[0]: obj.scale.x *= (target_dim / obj.dimensions.x)
                        if obj.dimensions.y > 0.00000001 and axes[1]: obj.scale.y *= (target_dim / obj.dimensions.y)
                        if obj.dimensions.z > 0.00000001 and axes[2]: obj.scale.z *= (target_dim / obj.dimensions.z)
            # Bake to 1.0 (Applied Scale)
            bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
        return {'FINISHED'}
class LSD_OT_Dimension_AutoScale(bpy.types.Operator):
    """Automatically scale arrowheads, text, line thickness and offset based on dimension length."""
    bl_idname = "lsd.dimension_auto_scale"
    bl_label = "Auto Size Components"
    bl_options = {'REGISTER', 'UNDO'}
    @classmethod
    def poll(cls, context):
        from . import core
        return core.get_dimension_host(context.active_object) is not None
    def execute(self, context):
        from . import core
        host = core.get_dimension_host(context.active_object)
        if not host: return {'CANCELLED'}
        dim_props = host.lsd_pg_dim_props
        length = dim_props.length
        scene = context.scene
        dim_props.arrow_scale = length * scene.lsd_dim_ratio_arrow
        dim_props.text_scale = length * scene.lsd_dim_ratio_text
        dim_props.line_thickness = length * scene.lsd_dim_ratio_thick
        dim_props.text_offset = length * scene.lsd_dim_ratio_text_off
        dim_props.offset = length * scene.lsd_dim_ratio_offset
        # Ensure minimum visibility guards
        if dim_props.arrow_scale < 0.001: dim_props.arrow_scale = 0.001
        if dim_props.text_scale < 0.001: dim_props.text_scale = 0.001
        core.update_arrow_settings(host)
        return {'FINISHED'}
class LSD_OT_Register_Default_Proportions(bpy.types.Operator):
    """Saves the current dimension's ratios as the new session defaults."""
    bl_idname = "lsd.register_default_proportions"
    bl_label = "Register Custom Proportions"
    bl_description = "Saves the current proportions as the new baseline for Auto-Size"
    bl_options = {'REGISTER', 'UNDO'}
    @classmethod
    def poll(cls, context):
        from . import core
        return core.get_dimension_host(context.active_object) is not None
    def execute(self, context):
        from . import core
        scene = context.scene
        host = core.get_dimension_host(context.active_object)
        dim_props = host.lsd_pg_dim_props
        length = dim_props.length
        if length < 0.0001: return {'CANCELLED'}
        # Calculate and Save Ratios
        scene.lsd_dim_ratio_arrow = dim_props.arrow_scale / length
        scene.lsd_dim_ratio_text = dim_props.text_scale / length
        scene.lsd_dim_ratio_thick = dim_props.line_thickness / length
        scene.lsd_dim_ratio_text_off = dim_props.text_offset / length
        scene.lsd_dim_ratio_offset = dim_props.offset / length
        self.report({'INFO'}, "Registered custom drafting proportions as defaults")
        return {'FINISHED'}
class LSD_OT_Dimension_Auto_Calculate_Global(bpy.types.Operator):
    """Calculates optimal drafting sizes based on selection distance (for new assemblies)."""
    bl_idname = "lsd.dimension_auto_calculate_global"
    bl_label = "Auto Calculate"
    bl_description = "Inputs optimal dimension properties scale based on selection distance."
    bl_options = {'REGISTER', 'UNDO'}
    def execute(self, context):
        scene = context.scene
        import mathutils
        dist = 0.0
        # 1. Harvesting reference points
        points = []
        if context.mode == 'EDIT_MESH':
            import bmesh
            # We must ensure we're looking at the right mesh data
            obj = context.active_object
            if obj.type == 'MESH':
                bm = bmesh.from_edit_mesh(obj.data)
                points = [obj.matrix_world @ v.co.copy() for v in bm.verts if v.select]
        else:
            points = [o.matrix_world.translation.copy() for o in context.selected_objects]
        if len(points) >= 2:
            dist = (points[0] - points[1]).length
        elif len(points) == 1:
            dist = (points[0] - scene.cursor.location).length
        else:
            self.report({'WARNING'}, "Select at least 2 points to calculate distance.")
            return {'CANCELLED'}
        if dist < 0.0001: dist = 1.0 # Absolute Minimal Fallback
        # 2. Update Global Scale Priorities (Scene Settings)
        # These use the established drafting ratios (set by Register Custom Proportions)
        scene.lsd_dim_arrow_scale = dist * scene.lsd_dim_ratio_arrow
        scene.lsd_dim_text_scale = dist * scene.lsd_dim_ratio_text
        scene.lsd_dim_line_thickness = dist * scene.lsd_dim_ratio_thick
        scene.lsd_dim_text_offset = dist * scene.lsd_dim_ratio_text_off
        scene.lsd_dim_offset = dist * scene.lsd_dim_ratio_offset
        self.report({'INFO'}, f"Pre-calculated preferences for distance: {dist:.3f}")
        return {'FINISHED'}
class LSD_OT_Apply_SDF_Booleans(bpy.types.Operator):
    """Sets up a Geometry Nodes modifier for SDF-based smooth booleans."""
    bl_idname = "lsd.apply_sdf_booleans"
    bl_label = "Setup SDF Booleans"
    bl_description = "Creates/Updates Geometry Nodes SDF setup for smooth, seamless booleans."
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        from . import generators
        obj = context.active_object
        if not obj:
            self.report({'WARNING'}, "No active object")
            return {'CANCELLED'}
        generators.setup_sdf_booleans(obj)
        self.report({'INFO'}, "Applied SDF boolean setup.")
        return {'FINISHED'}
class LSD_OT_Quick_SDF_Boolean(bpy.types.Operator):
    """Automatically assigns the cutter and executes the boolean."""
    bl_idname = "lsd.quick_sdf_boolean"
    bl_label = "Quick SDF Boolean"
    bl_options = {'REGISTER', 'UNDO'}
    
    operation: bpy.props.StringProperty(name="Operation", default='UNION')
    
    def execute(self, context):
        active = context.active_object
        selected = [o for o in context.selected_objects if o != active and o.type == 'MESH']
        if not active or not selected:
            self.report({'WARNING'}, "Please select active mesh and at least one cutter mesh")
            return {'CANCELLED'}
        cutter = selected[0]
        
        props = getattr(active, "lsd_pg_sdf_props", None)
        if props:
            props.target_object = cutter
            props.boolean_operation = self.operation
            
        from . import generators
        generators.apply_boolean_pro(
            obj=active,
            cutter=cutter,
            operation=self.operation,
            solver=props.boolean_solver if props else 'MANIFOLD',
            transfer_normals=props.transfer_normals if props else True,
            outset=props.outset_thickness if props else 0.0,
            inset=props.inset_thickness if props else 0.0,
            weld_enabled=props.weld_enabled if props else False,
            weld_distance=props.weld_distance if props else 0.0001,
            materials_mode=props.materials_mode if props else 'INDEX_BASED',
            materials_slot_index=props.materials_slot_index if props else 0,
            texture_blur=props.texture_blur if props else 0.0,
            bevel_weld_radius=props.bevel_weld_radius if props else 0.0
        )
        self.report({'INFO'}, f"Applied Boolean ({self.operation}) with {cutter.name}")
        return {'FINISHED'}

class LSD_OT_Toggle_Cutter_Visibility(bpy.types.Operator):
    """Toggles the viewport display type of the cutter object"""
    bl_idname = "lsd.toggle_cutter_visibility"
    bl_label = "Toggle Cutter Visibility"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        obj = context.active_object
        if obj and obj.lsd_pg_sdf_props.target_object:
            cutter = obj.lsd_pg_sdf_props.target_object
            if cutter.display_type == 'BOUNDS':
                cutter.display_type = 'TEXTURED'
            else:
                cutter.display_type = 'BOUNDS'
        return {'FINISHED'}

class LSD_OT_Directional_Translate(bpy.types.Operator):
    """Directional Translate based on mouse vector"""
    bl_idname = "lsd.directional_translate"
    bl_label = "Directional Translate"
    bl_options = {'REGISTER', 'UNDO', 'BLOCKING', 'GRAB_CURSOR'}
    
    @classmethod
    def poll(cls, context):
        return getattr(context.scene, "lsd_enable_directional_translate", False)
    
    def invoke(self, context, event):
        self.is_edit = context.mode == 'EDIT_MESH'
        import mathutils
        
        if self.is_edit:
            self.active_obj = context.active_object
            if not self.active_obj or self.active_obj.type != 'MESH':
                return {'CANCELLED'}
                
            import bmesh
            self.bm = bmesh.from_edit_mesh(self.active_obj.data)
            self.verts = [v for v in self.bm.verts if v.select]
            if not self.verts:
                return {'CANCELLED'}
                
            self.init_vert_locs = {v: v.co.copy() for v in self.verts}
            center = mathutils.Vector((0,0,0))
            for v in self.verts:
                center += self.active_obj.matrix_world @ v.co
            self.origin_world = center / len(self.verts)
            self.init_world_matrix = self.active_obj.matrix_world.copy()
            self.inv_world_matrix = self.active_obj.matrix_world.inverted()
        else:
            if not context.selected_objects:
                return {'CANCELLED'}
            self.active_obj = context.active_object
            self.initial_locs = {obj.name: obj.location.copy() for obj in context.selected_objects}
            self.initial_world_locs = {obj.name: obj.matrix_world.translation.copy() for obj in context.selected_objects}
            self.origin_world = self.active_obj.matrix_world.translation.copy()
            
        self.init_mouse_x = event.mouse_region_x
        self.init_mouse_y = event.mouse_region_y
        self.mouse_x = event.mouse_region_x
        self.mouse_y = event.mouse_region_y
        self.input_value = ""
        self.last_valid_direction = None
        
        self.lock_axis = None
        self.lock_mode = 'GLOBAL'
        self.is_middle_mouse_down = False
        self.mm_start_pos = None
        self.mm_dragged = False
        
        # Ensure we are in a 3D view to prevent region_data NoneType errors
        if context.area.type != 'VIEW_3D' or context.region_data is None:
            self.report({'WARNING'}, "Directional Translate must be used in the 3D Viewport (Assign it to a shortcut like G).")
            return {'CANCELLED'}
        
        context.window_manager.modal_handler_add(self)
        context.area.header_text_set("Directional Translate: move mouse and type distance. Esc/RMB to cancel, Enter/LMB to confirm.")
        return {'RUNNING_MODAL'}
        
    def modal(self, context, event):
        context.area.header_text_set(f"Directional Translate | Dist: {self.input_value if self.input_value else 'Freestyle'} | Enter to confirm")
        
        if event.type == 'MOUSEMOVE':
            self.mouse_x = event.mouse_region_x
            self.mouse_y = event.mouse_region_y
            
            if self.is_middle_mouse_down and self.mm_start_pos:
                dx = self.mouse_x - self.mm_start_pos[0]
                dy = self.mouse_y - self.mm_start_pos[1]
                if (dx*dx + dy*dy) > 64: # ~8 pixels movement to confirm lock
                    self.mm_dragged = True
                    self.lock_axis = self._get_best_screen_axis(context, dx, dy)
                    self.lock_mode = 'GLOBAL'
                    
            self.update_transform(context)
            
        elif event.value == 'PRESS' and hasattr(event, "unicode") and event.unicode and event.type not in {'X', 'Y', 'Z', 'G', 'O'}:
            char = event.unicode
            # If math input is enabled, we allow math characters. Otherwise just numbers and periods.
            allow_math = getattr(context.scene, "lsd_enable_math_input", False)
            valid_chars = "0123456789.-"
            if allow_math:
                valid_chars += "+*/()="
                
            if char in valid_chars:
                if char == '=' and not allow_math:
                    pass
                else:
                    self.input_value += char
                    self.update_transform(context)
                    
        elif event.type == 'BACK_SPACE' and event.value == 'PRESS':
            if self.input_value:
                self.input_value = self.input_value[:-1]
                self.update_transform(context)
            
        elif event.type == 'MIDDLEMOUSE':
            if event.value == 'PRESS':
                self.is_middle_mouse_down = True
                self.mm_start_pos = (event.mouse_region_x, event.mouse_region_y)
                self.mm_dragged = False
            elif event.value == 'RELEASE':
                if not self.mm_dragged:
                    # Quick click, remove lock
                    self.lock_axis = None
                    self.lock_mode = 'GLOBAL'
                    self.update_transform(context)
                self.is_middle_mouse_down = False
                self.mm_start_pos = None
                
        elif event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            context.area.header_text_set(None)
            return {'FINISHED'}
            
        elif event.type in {'RIGHTMOUSE', 'ESC'} and event.value == 'PRESS':
            self._revert_transform(context)
            context.area.header_text_set(None)
            return {'CANCELLED'}
            
        elif event.type in {'RET', 'NUMPAD_ENTER'}:
            if event.value == 'PRESS':
                context.area.header_text_set(None)
                return {'FINISHED'}
                
        elif event.type in {'X', 'Y', 'Z'} and event.value == 'PRESS':
            if self.lock_axis == event.type:
                if self.lock_mode == 'GLOBAL':
                    self.lock_mode = 'LOCAL'
                else:
                    self.lock_axis = None
                    self.lock_mode = 'GLOBAL'
            else:
                self.lock_axis = event.type
                self.lock_mode = 'GLOBAL'
            self.update_transform(context)
                
        # Smart Handoff for specific tools (Edge Slide, Prop Editing)
        elif event.value == 'PRESS' and event.type in {'G', 'O', 'B'}:
            self._revert_transform(context)
            context.area.header_text_set(None)
            if event.type == 'G':
                if self.is_edit:
                    if 'EDGE' in self.bm.select_mode:
                        bpy.ops.transform.edge_slide('INVOKE_DEFAULT')
                    else:
                        bpy.ops.transform.vert_slide('INVOKE_DEFAULT')
                else:
                    bpy.ops.transform.translate('INVOKE_DEFAULT')
            else:
                bpy.ops.transform.translate('INVOKE_DEFAULT')
            return {'CANCELLED'}
            
        return {'RUNNING_MODAL'}
        
    def _revert_transform(self, context):
        if self.is_edit:
            import bmesh
            for v in self.verts:
                v.co = self.init_vert_locs[v]
            bmesh.update_edit_mesh(self.active_obj.data)
        else:
            for obj in context.selected_objects:
                obj.location = self.initial_locs[obj.name]
        context.area.tag_redraw()
        
    def _get_best_screen_axis(self, context, dx, dy):
        from bpy_extras import view3d_utils
        import mathutils
        region = context.region
        rv3d = context.region_data
        origin_2d = view3d_utils.location_3d_to_region_2d(region, rv3d, self.origin_world)
        if not origin_2d: return 'X'
        axes = {'X': mathutils.Vector((1,0,0)), 'Y': mathutils.Vector((0,1,0)), 'Z': mathutils.Vector((0,0,1))}
        best_axis = 'X'
        max_dot = -1.0
        mouse_vec = mathutils.Vector((dx, dy)).normalized()
        for name, vec in axes.items():
            p3d = self.origin_world + vec
            p2d = view3d_utils.location_3d_to_region_2d(region, rv3d, p3d)
            if p2d:
                screen_vec = (p2d - origin_2d).normalized()
                d = abs(screen_vec.dot(mouse_vec))
                if d > max_dot:
                    max_dot = d
                    best_axis = name
        return best_axis
        
    def update_transform(self, context):
        import mathutils
        from bpy_extras import view3d_utils
        region = context.region
        rv3d = context.region_data
        
        if not self.active_obj: return
        
        view_vec = view3d_utils.region_2d_to_vector_3d(region, rv3d, (self.mouse_x, self.mouse_y))
        ray_origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, (self.mouse_x, self.mouse_y))
        
        plane_normal = rv3d.view_matrix.row[2].xyz
        
        isect = mathutils.geometry.intersect_line_plane(ray_origin, ray_origin + view_vec, self.origin_world, plane_normal)
        
        if isect is None: return
        
        displacement = isect - self.origin_world
        
        # Apply Axis Lock
        if self.lock_axis:
            if self.lock_mode == 'LOCAL' and not self.is_edit:
                ori = self.active_obj.matrix_world.to_3x3()
                if self.lock_axis == 'X': axis_vec = ori @ mathutils.Vector((1,0,0))
                elif self.lock_axis == 'Y': axis_vec = ori @ mathutils.Vector((0,1,0))
                else: axis_vec = ori @ mathutils.Vector((0,0,1))
                axis_vec.normalize()
                displacement = axis_vec * displacement.dot(axis_vec)
            else:
                dx, dy, dz = displacement.x, displacement.y, displacement.z
                if self.lock_axis == 'X': displacement = mathutils.Vector((dx, 0, 0))
                elif self.lock_axis == 'Y': displacement = mathutils.Vector((0, dy, 0))
                elif self.lock_axis == 'Z': displacement = mathutils.Vector((0, 0, dz))
        
        if self.input_value:
            dist_str = self.input_value
            if dist_str.startswith('='):
                dist_str = dist_str[1:]
                # Safe math evaluation
                try:
                    # Sanitize input: only allow math chars
                    if all(c in "0123456789.+-*/() " for c in dist_str) and len(dist_str) > 0:
                        import re, warnings
                        # Convert implicit multiplication e.g. 5(2) to 5*(2) and )( to )*(
                        dist_str = re.sub(r'(\d|\))\s*\(', r'\1*(', dist_str)
                        with warnings.catch_warnings():
                            warnings.simplefilter("ignore")
                            dist = float(eval(dist_str))
                    else:
                        dist = 0.0
                except:
                    dist = 0.0
            else:
                try:
                    dist = float(dist_str)
                except ValueError:
                    dist = 0.0
            
            if displacement.length > 0.0001:
                self.last_valid_direction = displacement.normalized()
            
            if self.last_valid_direction:
                displacement = self.last_valid_direction * dist
            else:
                displacement = mathutils.Vector((0, 0, 0))
                
        if self.is_edit:
            import bmesh
            local_disp = self.inv_world_matrix.to_3x3() @ displacement
            for v in self.verts:
                v.co = self.init_vert_locs[v] + local_disp
            bmesh.update_edit_mesh(self.active_obj.data)
        else:
            for obj in context.selected_objects:
                if obj.parent:
                    obj.matrix_world.translation = self.initial_world_locs[obj.name] + displacement
                else:
                    obj.location = self.initial_locs[obj.name] + displacement
                
        # Force a viewport redraw to ensure the user sees the update instantly
        context.area.tag_redraw()

class LSD_OT_Asset_Edit_External(bpy.types.Operator):
    """Directly saves the name and catalog edits to the external .blend file"""
    bl_idname = "lsd.asset_edit_external"
    bl_label = "Edit External Asset"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        asset = None
        asset_window = None
        asset_area = None
        for window in context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == 'FILE_BROWSER' and getattr(area, 'ui_type', '') == 'ASSETS':
                    with context.temp_override(window=window, area=area):
                        if hasattr(context, 'selected_asset_files') and context.selected_asset_files:
                            asset = context.selected_asset_files[0]
                            asset_window, asset_area = window, area
                        elif hasattr(context, 'selected_assets') and context.selected_assets:
                            asset = context.selected_assets[0]
                            asset_window, asset_area = window, area
                    if asset: break
            if asset: break
            
        if not asset:
            self.report({'WARNING'}, "No asset selected.")
            return {'CANCELLED'}
            
        is_local = getattr(asset, 'local_id', None) is not None
        if is_local:
            self.report({'WARNING'}, "This asset is local. Edit it directly above.")
            return {'CANCELLED'}
            
        filepath = getattr(asset, 'full_library_path', '')
        if not filepath and hasattr(asset, 'relative_path'):
            import os
            # Build absolute path from active library
            prefs = context.preferences
            area = next((a for w in context.window_manager.windows for a in w.screen.areas if a.type == 'FILE_BROWSER' and getattr(a, 'ui_type', '') == 'ASSETS'), None)
            lib_id = None
            if area:
                space = area.spaces.active
                if space and hasattr(space, 'params'):
                    lib_id = getattr(space.params, 'asset_library_ref', None)
            
            base_dir = ""
            if lib_id in ('ALL', 'LOCAL', 'CUSTOM'):
                # Heuristic search
                pass
            else:
                for lib in prefs.filepaths.asset_libraries:
                    if lib.name == lib_id:
                        base_dir = lib.path
                        break
            if not base_dir:
                for lib in prefs.filepaths.asset_libraries:
                    path = os.path.join(lib.path, asset.relative_path)
                    if os.path.exists(path):
                        base_dir = lib.path
                        break
                        
            if base_dir:
                filepath = os.path.join(base_dir, asset.relative_path)
                
        if not filepath or not __import__('os').path.exists(filepath):
            self.report({'ERROR'}, "Could not resolve external file path.")
            return {'CANCELLED'}
            
        new_name = context.scene.lsd_asset_new_name
        target_cat = context.scene.lsd_asset_catalog
        if target_cat in ('UNAVAILABLE', 'NONE'):
            target_cat = ""
            
        # Write temporary script
        import tempfile
        import os
        import subprocess
        
        script_content = f'''import bpy

try:
    obj_name = {repr(asset.name)}
    new_name = {repr(new_name)}
    target_cat = {repr(target_cat)}
    
    found = False
    for attr in ('objects', 'collections', 'materials', 'node_groups'):
        data_coll = getattr(bpy.data, attr)
        for item in data_coll:
            if item.name == obj_name and item.asset_data:
                if new_name:
                    item.name = new_name
                if target_cat is not None:
                    item.asset_data.catalog_id = target_cat
                found = True
                break
        if found: break
        
    if found:
        bpy.ops.wm.save_mainfile()
        
    bpy.ops.wm.quit_blender()
except Exception as e:
    print("Error:", e)
    bpy.ops.wm.quit_blender()
'''
        fd, script_path = tempfile.mkstemp(suffix=".py")
        with os.fdopen(fd, 'w') as f:
            f.write(script_content)
            
        creation_flags = 0x08000000 # CREATE_NO_WINDOW
        
        self.report({'INFO'}, "Directly editing external file...")
        
        try:
            res = subprocess.run([
                bpy.app.binary_path,
                "-b",
                filepath,
                "--factory-startup",
                "-P",
                script_path
            ], creationflags=creation_flags, capture_output=True, timeout=15)
            
            if res.returncode == 0:
                self.report({'INFO'}, "Successfully saved asset metadata directly.")
                # Force UI reload
                if asset_window and asset_area:
                    with context.temp_override(window=asset_window, area=asset_area):
                        bpy.ops.asset.library_refresh()
            else:
                self.report({'ERROR'}, "Failed to find or edit asset in external file.")
        except Exception as e:
            self.report({'ERROR'}, f"Edit failed: {e}")
        finally:
            if os.path.exists(script_path):
                os.remove(script_path)
                
        return {'FINISHED'}


class LSD_OT_Asset_Clear(bpy.types.Operator):
    """Removes the asset metadata from the currently selected asset."""
    bl_idname = "lsd.asset_clear"
    bl_label = "Unmark Asset"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        asset = None
        asset_area = None
        for window in context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == 'FILE_BROWSER' and area.ui_type == 'ASSETS':
                    with context.temp_override(window=window, area=area):
                        if hasattr(context, 'selected_asset_files') and context.selected_asset_files:
                            asset = context.selected_asset_files[0]
                            asset_area = area
                            break
                        if hasattr(context, 'selected_assets') and context.selected_assets:
                            asset = context.selected_assets[0]
                            asset_area = area
                            break
            if asset: break
            
        if not asset:
            self.report({'WARNING'}, "No asset selected in the Asset Browser")
            return {'CANCELLED'}
            
        is_local = getattr(asset, 'local_id', None) is not None
        
        if is_local:
            asset.local_id.asset_clear()
            self.report({'INFO'}, "Cleared local asset metadata.")
        else:
            self.report({'WARNING'}, "Cannot edit external assets directly.")
            return {'CANCELLED'}
            
        return {'FINISHED'}



class LSD_OT_Sync_Active_Layer(bpy.types.Operator):
    bl_idname = "lsd.sync_active_layer"
    bl_label = "Sync Layers"
    bl_options = {'INTERNAL'}
    
    _is_syncing = False
    _timer = None
    _state = 0
    _original_area_type = 'VIEW_3D'
    _target_area = None

    def finish(self, context):
        LSD_OT_Sync_Active_Layer._is_syncing = False
        if getattr(self, '_timer', None):
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None
        if hasattr(self, "_original_area_type") and getattr(self, "_target_area", None):
            try:
                self._target_area.type = self._original_area_type
            except: pass

    def modal(self, context, event):
        from . import anim_core
        if event.type == 'TIMER':

            if self._state == 0:
                LSD_OT_Sync_Active_Layer._is_syncing = True
                
                # Step 1: We MUST exit Tweak Mode first, or Blender ignores track selection!
                obj = anim_core.get_active_object(context)
                if obj and obj.animation_data and obj.animation_data.use_tweak_mode:
                    if not getattr(self, '_target_area', None):
                        for window in context.window_manager.windows:
                            for area in window.screen.areas:
                                if area.type == 'NLA_EDITOR':
                                    self._target_window = window
                                    self._target_area = area
                                    self._original_area_type = area.type
                                    break
                            if getattr(self, '_target_area', None):
                                break
                                
                        if not getattr(self, '_target_area', None):
                            for window in context.window_manager.windows:
                                for area in window.screen.areas:
                                    if area != context.area and area.type != 'PROPERTIES':
                                        self._target_window = window
                                        self._target_area = area
                                        self._original_area_type = area.type
                                        area.type = 'NLA_EDITOR'
                                        break
                                if getattr(self, '_target_area', None):
                                    break
                    
                    if getattr(self, '_target_area', None):
                        self._ticks = 0
                        self._state = 1
                        return {'PASS_THROUGH'}
                else:
                    self._state = 2
                    return {'PASS_THROUGH'}
                    
            elif self._state == 1:
                if getattr(self, '_target_area', None):
                    self._ticks += 1
                    if self._ticks < 3:
                        return {'PASS_THROUGH'}
                        
                    if getattr(self._target_area.spaces.active, 'type', None) != 'NLA_EDITOR':
                        return {'PASS_THROUGH'}
                        
                    self._target_region = None
                    for region in self._target_area.regions:
                        if region.type == 'WINDOW':
                            self._target_region = region
                            break
                            
                    if self._target_region:
                        try:
                            with context.temp_override(window=self._target_window, area=self._target_area, region=self._target_region):
                                bpy.ops.nla.tweakmode_exit()
                        except Exception as e:
                            print("Tweakmode exit failed:", e)
                
                self._state = 2
                return {'PASS_THROUGH'}

            elif self._state == 2:
                # Step 2: Now that Tweak Mode is off, we can safely sync the tracks!
                obj = anim_core.get_active_object(context)
                if obj and obj.animation_data:
                    try:
                        obj.animation_data.action = None  # CRITICAL: Prevent old layer action from leaking!
                    except Exception:
                        pass
                anim_core.execute_sync_logic(context)
                self._state = 3
                return {'PASS_THROUGH'}

            elif self._state == 3:
                # Step 3: Enter Tweak Mode for the newly selected layer!
                if not getattr(self, '_target_area', None):
                    for window in context.window_manager.windows:
                        for area in window.screen.areas:
                            if area.type == 'NLA_EDITOR':
                                self._target_window = window
                                self._target_area = area
                                self._original_area_type = area.type
                                break
                        if getattr(self, '_target_area', None):
                            break
                            
                    if not getattr(self, '_target_area', None):
                        for window in context.window_manager.windows:
                            for area in window.screen.areas:
                                if area != context.area and area.type != 'PROPERTIES':
                                    self._target_window = window
                                    self._target_area = area
                                    self._original_area_type = area.type
                                    area.type = 'NLA_EDITOR'
                                    break
                            if getattr(self, '_target_area', None):
                                break
                    if getattr(self, '_target_area', None):
                        self._ticks = 0
                        self._state = 4
                        return {'PASS_THROUGH'}
                else:
                    self._state = 4
                    return {'PASS_THROUGH'}
            
            elif self._state == 4:
                if getattr(self, '_target_area', None):
                    self._ticks += 1
                    if self._ticks < 3:
                        return {'PASS_THROUGH'}
                        
                    if getattr(self._target_area.spaces.active, 'type', None) != 'NLA_EDITOR':
                        return {'PASS_THROUGH'}
                        
                    self._target_region = None
                    for region in self._target_area.regions:
                        if region.type == 'WINDOW':
                            self._target_region = region
                            break
                            
                    if self._target_region:
                        try:
                            obj = anim_core.get_active_object(context)
                            active_layer = None
                            if obj and hasattr(obj, 'lsd_anim_layers_data'):
                                ldata = obj.lsd_anim_layers_data
                                if 0 <= ldata.active_layer_index < len(ldata.layers):
                                    active_layer = ldata.layers[ldata.active_layer_index]
                            
                            if active_layer and (active_layer.is_locked or active_layer.is_muted):
                                self.finish(context)
                                return {'FINISHED'}
                                
                            with context.temp_override(window=self._target_window, area=self._target_area, region=self._target_region):
                                bpy.ops.nla.tweakmode_enter()
                        except Exception as e:
                            print("Tweakmode enter failed:", e)
                
                self.finish(context)
                return {'FINISHED'}

        return {'PASS_THROUGH'}

    def invoke(self, context, event):
        wm = context.window_manager
        self._timer = wm.event_timer_add(0.01, window=context.window)
        wm.modal_handler_add(self)
        return {'RUNNING_MODAL'}


class LSD_OT_Anim_Refresh_Sync(bpy.types.Operator):
    bl_idname = "lsd.anim_refresh_sync"
    bl_label = "Refresh Layer Sync"
    bl_description = "Register new animations and refresh the timeline."
    
    def execute(self, context):
        from . import anim_core
        anim_core.invisible_tweakmode_swap(context, exit_first=True, enter_second=True)
        self.report({'INFO'}, "Animation Layers Synchronized")
        return {'FINISHED'}

class LSD_OT_Anim_Keyframe_Entire_Pose(bpy.types.Operator):
    bl_idname = "lsd.anim_keyframe_entire_pose"
    bl_label = "Keyframe All Transforms for Selected"
    bl_description = "Keyframe all transforms for the selected bones (or objects) into the current Action"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        return context.active_object is not None
        
    def execute(self, context):
        from . import anim_core
        obj = context.active_object
        frame = context.scene.frame_current
        
        # Determine targets
        targets = []
        if context.mode == 'POSE' and obj.type == 'ARMATURE':
            targets = context.selected_pose_bones
            if not targets:
                self.report({'WARNING'}, "No bones selected")
                return {'CANCELLED'}
        elif context.mode == 'OBJECT':
            targets = context.selected_objects
            if not targets:
                self.report({'WARNING'}, "No objects selected")
                return {'CANCELLED'}
        else:
            self.report({'WARNING'}, "Must be in Pose or Object mode with a selection")
            return {'CANCELLED'}
            
        kf_options = set()
        
        # Group targets by animation holder (Armature for pose bones, object for objects)
        targets_by_holder = {}
        for target in targets:
            anim_holder = obj if (context.mode == 'POSE' and obj.type == 'ARMATURE') else target
            if anim_holder not in targets_by_holder:
                targets_by_holder[anim_holder] = []
            targets_by_holder[anim_holder].append(target)
            
        for anim_holder, holder_targets in targets_by_holder.items():
            # Ensure animation data
            if not anim_holder.animation_data:
                anim_holder.animation_data_create()
                
            # Locate active layer and strip
            track_strip = None
            active_layer = None
            active_layer_idx = -1
            ldata = getattr(anim_holder, 'lsd_anim_layers_data', None)
            if ldata and ldata.layers:
                if 0 <= ldata.active_layer_index < len(ldata.layers):
                    active_layer_idx = ldata.active_layer_index
                    active_layer = ldata.layers[active_layer_idx]
                    for tr in anim_holder.animation_data.nla_tracks:
                        if (tr.name == active_layer.name or tr.name == active_layer.track_name) and tr.strips:
                            track_strip = tr.strips[0]
                            break
                            
            if track_strip:
                action = track_strip.action
                try:
                    if anim_holder.animation_data.action != action:
                        anim_holder.animation_data.action = action
                except Exception:
                    pass
            elif anim_holder.animation_data.action:
                action = anim_holder.animation_data.action
            else:
                action = bpy.data.actions.new(name=anim_holder.name + "Action")
                try:
                    anim_holder.animation_data.action = action
                except Exception:
                    pass

            slot = anim_core.ensure_action_slot(anim_holder, action)
            if slot and hasattr(anim_holder.animation_data, 'action_slot'):
                try: anim_holder.animation_data.action_slot = slot
                except Exception: pass
            if track_strip:
                anim_core.bind_strip_slot(anim_holder, track_strip, slot)

            # Calculate the precise float frame where Blender's native keyframer will insert
            exact_float = float(frame)
            if track_strip:
                exact_float = track_strip.action_frame_start + (frame - track_strip.frame_start) / track_strip.scale

            is_combine_layer = (
                active_layer is not None and 
                active_layer.blend_type == 'COMBINE' and 
                active_layer_idx > 0 and 
                "Base" not in active_layer.name
            )

            if is_combine_layer:
                # --- TRUE DELTA CALCULATION ENGINE ---
                # 1. Capture desired visual transforms from current viewport pose
                desired_transforms = {}
                for target in holder_targets:
                    rot_mode = getattr(target, "rotation_mode", "QUATERNION")
                    desired_transforms[target] = {
                        "location": target.location.copy(),
                        "rotation_mode": rot_mode,
                        "rotation_quaternion": target.rotation_quaternion.copy() if rot_mode == 'QUATERNION' else None,
                        "rotation_axis_angle": list(target.rotation_axis_angle) if rot_mode == 'AXIS_ANGLE' else None,
                        "rotation_euler": target.rotation_euler.copy() if rot_mode not in {'QUATERNION', 'AXIS_ANGLE'} else None,
                        "scale": target.scale.copy(),
                    }

                # 2. Temporarily mute active and higher tracks and clear floating action to sample base evaluation
                orig_track_mutes = {}
                for idx in range(active_layer_idx, len(ldata.layers)):
                    lyr = ldata.layers[idx]
                    for tr in anim_holder.animation_data.nla_tracks:
                        if tr.name == lyr.name or tr.name == lyr.track_name:
                            orig_track_mutes[tr] = tr.mute
                            tr.mute = True

                orig_holder_action = anim_holder.animation_data.action
                try: anim_holder.animation_data.action = None
                except Exception: pass
                context.view_layer.update()

                # 3. Read base transforms evaluated from lower stack
                base_transforms = {}
                for target in holder_targets:
                    rot_mode = desired_transforms[target]["rotation_mode"]
                    base_transforms[target] = {
                        "location": target.location.copy(),
                        "rotation_quaternion": target.rotation_quaternion.copy() if rot_mode == 'QUATERNION' else None,
                        "rotation_axis_angle": list(target.rotation_axis_angle) if rot_mode == 'AXIS_ANGLE' else None,
                        "rotation_euler": target.rotation_euler.copy() if rot_mode not in {'QUATERNION', 'AXIS_ANGLE'} else None,
                        "scale": target.scale.copy(),
                    }

                # 4. Restore track mutes and active action
                for tr, m in orig_track_mutes.items():
                    tr.mute = m
                try:
                    if orig_holder_action:
                        anim_holder.animation_data.action = orig_holder_action
                except Exception: pass

                # 5. Compute true deltas, anchor frame 1.0 at identity if needed, and insert keyframes
                for target in holder_targets:
                    rot_mode = desired_transforms[target]["rotation_mode"]
                    des = desired_transforms[target]
                    base = base_transforms[target]

                    # Translation delta
                    d_loc = des["location"] - base["location"]

                    # Rotation delta
                    d_quat = None
                    d_euler = None
                    d_axis_angle = None
                    if rot_mode == 'QUATERNION':
                        base_q = base["rotation_quaternion"].normalized()
                        des_q = des["rotation_quaternion"].normalized()
                        d_quat = base_q.inverted() @ des_q
                        if d_quat.w < 0:
                            d_quat = -d_quat
                    elif rot_mode == 'AXIS_ANGLE':
                        b_aa = base["rotation_axis_angle"]
                        d_aa = des["rotation_axis_angle"]
                        b_q = mathutils.Quaternion(mathutils.Vector((b_aa[1], b_aa[2], b_aa[3])), b_aa[0])
                        d_q = mathutils.Quaternion(mathutils.Vector((d_aa[1], d_aa[2], d_aa[3])), d_aa[0])
                        delta_q = b_q.inverted() @ d_q
                        if delta_q.w < 0: delta_q = -delta_q
                        axis, angle = delta_q.to_axis_angle()
                        d_axis_angle = [angle, axis.x, axis.y, axis.z]
                    else:
                        des_e = des["rotation_euler"]
                        base_e = base["rotation_euler"]
                        d_euler = mathutils.Euler((des_e.x - base_e.x, des_e.y - base_e.y, des_e.z - base_e.z), des_e.order)

                    # Scale delta
                    des_s = des["scale"]
                    base_s = base["scale"]
                    d_scale = mathutils.Vector((
                        des_s.x / base_s.x if abs(base_s.x) > 1e-6 else 1.0,
                        des_s.y / base_s.y if abs(base_s.y) > 1e-6 else 1.0,
                        des_s.z / base_s.z if abs(base_s.z) > 1e-6 else 1.0,
                    ))

                    bone_path = f'pose.bones["{target.name}"]' if (context.mode == 'POSE' and obj.type == 'ARMATURE') else ""
                    prefix_path = f'{bone_path}.' if bone_path else ""

                    # Anti-backtracking anchor: If exact_float > 1.0 and no earlier key exists, anchor frame 1.0 to identity zero-delta
                    if exact_float > 1.0:
                        has_early_key = False
                        action_fcurves = anim_core.get_action_fcurves(anim_holder, action)
                        for fc in action_fcurves:
                            if (not prefix_path or fc.data_path.startswith(prefix_path)):
                                for kp in fc.keyframe_points:
                                    if kp.co.x <= 1.0 + 1e-4:
                                        has_early_key = True
                                        break
                                if has_early_key:
                                    break
                        if not has_early_key:
                            target.location = mathutils.Vector((0.0, 0.0, 0.0))
                            if rot_mode == 'QUATERNION':
                                target.rotation_quaternion = mathutils.Quaternion((1.0, 0.0, 0.0, 0.0))
                            elif rot_mode == 'AXIS_ANGLE':
                                target.rotation_axis_angle = [0.0, 0.0, 1.0, 0.0]
                            else:
                                target.rotation_euler = mathutils.Euler((0.0, 0.0, 0.0), rot_mode)
                            target.scale = mathutils.Vector((1.0, 1.0, 1.0))

                            try:
                                if bone_path:
                                    obj.keyframe_insert(data_path=f'{bone_path}.location', frame=1.0, options=kf_options)
                                    if rot_mode == 'QUATERNION': obj.keyframe_insert(data_path=f'{bone_path}.rotation_quaternion', frame=1.0, options=kf_options)
                                    elif rot_mode == 'AXIS_ANGLE': obj.keyframe_insert(data_path=f'{bone_path}.rotation_axis_angle', frame=1.0, options=kf_options)
                                    else: obj.keyframe_insert(data_path=f'{bone_path}.rotation_euler', frame=1.0, options=kf_options)
                                    obj.keyframe_insert(data_path=f'{bone_path}.scale', frame=1.0, options=kf_options)
                                else:
                                    target.keyframe_insert(data_path="location", frame=1.0, options=kf_options)
                                    if rot_mode == 'QUATERNION': target.keyframe_insert(data_path="rotation_quaternion", frame=1.0, options=kf_options)
                                    elif rot_mode == 'AXIS_ANGLE': target.keyframe_insert(data_path="rotation_axis_angle", frame=1.0, options=kf_options)
                                    else: target.keyframe_insert(data_path="rotation_euler", frame=1.0, options=kf_options)
                                    target.keyframe_insert(data_path="scale", frame=1.0, options=kf_options)
                            except Exception: pass

                    # Insert True Delta transforms at exact_float
                    target.location = d_loc
                    if rot_mode == 'QUATERNION': target.rotation_quaternion = d_quat
                    elif rot_mode == 'AXIS_ANGLE': target.rotation_axis_angle = d_axis_angle
                    else: target.rotation_euler = d_euler
                    target.scale = d_scale

                    try:
                        if bone_path:
                            obj.keyframe_insert(data_path=f'{bone_path}.location', frame=exact_float, options=kf_options)
                            if rot_mode == 'QUATERNION': obj.keyframe_insert(data_path=f'{bone_path}.rotation_quaternion', frame=exact_float, options=kf_options)
                            elif rot_mode == 'AXIS_ANGLE': obj.keyframe_insert(data_path=f'{bone_path}.rotation_axis_angle', frame=exact_float, options=kf_options)
                            else: obj.keyframe_insert(data_path=f'{bone_path}.rotation_euler', frame=exact_float, options=kf_options)
                            obj.keyframe_insert(data_path=f'{bone_path}.scale', frame=exact_float, options=kf_options)
                            for prop in target.keys():
                                if prop not in '_RNA_UI':
                                    try: obj.keyframe_insert(data_path=f'{bone_path}["{prop}"]', frame=exact_float, options=kf_options)
                                    except: pass
                        else:
                            target.keyframe_insert(data_path="location", frame=exact_float, options=kf_options)
                            if rot_mode == 'QUATERNION': target.keyframe_insert(data_path="rotation_quaternion", frame=exact_float, options=kf_options)
                            elif rot_mode == 'AXIS_ANGLE': target.keyframe_insert(data_path="rotation_axis_angle", frame=exact_float, options=kf_options)
                            else: target.keyframe_insert(data_path="rotation_euler", frame=exact_float, options=kf_options)
                            target.keyframe_insert(data_path="scale", frame=exact_float, options=kf_options)
                            for prop in target.keys():
                                if prop not in '_RNA_UI':
                                    try: target.keyframe_insert(data_path=f'["{prop}"]', frame=exact_float, options=kf_options)
                                    except: pass
                    except Exception as e:
                        self.report({'WARNING'}, f"Failed to keyframe delta for {target.name}: {e}")

                    # Restore target viewport transform
                    target.location = des["location"]
                    if rot_mode == 'QUATERNION': target.rotation_quaternion = des["rotation_quaternion"]
                    elif rot_mode == 'AXIS_ANGLE': target.rotation_axis_angle = des["rotation_axis_angle"]
                    else: target.rotation_euler = des["rotation_euler"]
                    target.scale = des["scale"]

                    # Clamp handles and set CONSTANT extrapolation to eliminate dips and drift
                    for fc in anim_core.get_action_fcurves(anim_holder, action):
                        if not prefix_path or fc.data_path.startswith(prefix_path):
                            fc.extrapolation = 'CONSTANT'
                            for kp in fc.keyframe_points:
                                kp.handle_left_type = 'AUTO_CLAMPED'
                                kp.handle_right_type = 'AUTO_CLAMPED'

            else:
                # Standard Direct Keyframing for Base Layer or REPLACE mode layers
                for target in holder_targets:
                    bone_path = f'pose.bones["{target.name}"]' if (context.mode == 'POSE' and obj.type == 'ARMATURE') else ""
                    prefix_path = f'{bone_path}.' if bone_path else ""
                    try:
                        if bone_path:
                            obj.keyframe_insert(data_path=f'{bone_path}.location', frame=exact_float, options=kf_options)
                            if getattr(target, "rotation_mode", "QUATERNION") == 'QUATERNION':
                                obj.keyframe_insert(data_path=f'{bone_path}.rotation_quaternion', frame=exact_float, options=kf_options)
                            elif target.rotation_mode == 'AXIS_ANGLE':
                                obj.keyframe_insert(data_path=f'{bone_path}.rotation_axis_angle', frame=exact_float, options=kf_options)
                            else:
                                obj.keyframe_insert(data_path=f'{bone_path}.rotation_euler', frame=exact_float, options=kf_options)
                            obj.keyframe_insert(data_path=f'{bone_path}.scale', frame=exact_float, options=kf_options)
                            for prop in target.keys():
                                if prop not in '_RNA_UI':
                                    try: obj.keyframe_insert(data_path=f'{bone_path}["{prop}"]', frame=exact_float, options=kf_options)
                                    except: pass
                        else:
                            target.keyframe_insert(data_path="location", frame=exact_float, options=kf_options)
                            if getattr(target, "rotation_mode", "QUATERNION") == 'QUATERNION':
                                target.keyframe_insert(data_path="rotation_quaternion", frame=exact_float, options=kf_options)
                            elif target.rotation_mode == 'AXIS_ANGLE':
                                target.keyframe_insert(data_path="rotation_axis_angle", frame=exact_float, options=kf_options)
                            else:
                                target.keyframe_insert(data_path="rotation_euler", frame=exact_float, options=kf_options)
                            target.keyframe_insert(data_path="scale", frame=exact_float, options=kf_options)
                            for prop in target.keys():
                                if prop not in '_RNA_UI':
                                    try: target.keyframe_insert(data_path=f'["{prop}"]', frame=exact_float, options=kf_options)
                                    except: pass
                    except Exception as e:
                        self.report({'WARNING'}, f"Failed to natively keyframe {target.name}: {e}")

                    for fc in anim_core.get_action_fcurves(anim_holder, action):
                        if not prefix_path or fc.data_path.startswith(prefix_path):
                            fc.extrapolation = 'CONSTANT'
                            for kp in fc.keyframe_points:
                                kp.handle_left_type = 'AUTO_CLAMPED'
                                kp.handle_right_type = 'AUTO_CLAMPED'

            if track_strip is not None:
                if hasattr(track_strip, 'use_sync_length'):
                    track_strip.use_sync_length = False
                if exact_float > track_strip.action_frame_end:
                    track_strip.action_frame_end = exact_float
                if exact_float < track_strip.action_frame_start:
                    track_strip.action_frame_start = exact_float
                track_strip.scale = 1.0
                track_strip.frame_end = track_strip.frame_start + (track_strip.action_frame_end - track_strip.action_frame_start)
                track_strip.extrapolation = 'HOLD' if is_combine_layer and track_strip.frame_start <= 1.0 else ('HOLD_FORWARD' if is_combine_layer else 'HOLD')

            # Anti-inversion check: If active layer is not topmost and Tweak Mode is not active,
            # clear animation_data.action so NLA stack does not invert
            is_topmost = (active_layer_idx >= len(ldata.layers) - 1) if (ldata and active_layer_idx >= 0) else True
            if not is_topmost and not getattr(anim_holder.animation_data, 'use_tweak_mode', False):
                try: anim_holder.animation_data.action = None
                except Exception: pass
                if track_strip and active_layer:
                    for tr in anim_holder.animation_data.nla_tracks:
                        if tr.name == active_layer.name or tr.name == active_layer.track_name:
                            tr.mute = active_layer.is_muted

        context.view_layer.update()
        anim_core.ensure_timeline_frame_display(context)
        try:
            bpy.ops.lsd.calculate_onion_skin('INVOKE_DEFAULT')
        except: pass
        
        self.report({'INFO'}, f"Keyframed selected items at frame {frame}")
        return {'FINISHED'}


class LSD_OT_Anim_Layer_Add(bpy.types.Operator):
    bl_idname = "lsd.anim_layer_add"
    bl_label = "Add Animation Layer"
    bl_description = "Create a new Animation Layer to isolate and stack animation tweaks"
    
    def execute(self, context):
        from . import anim_core
        settings = context.scene.lsd_anim_settings
        obj = anim_core.get_active_object(context)
        
        # Step 1: Force exit tweak mode invisibly so the old layer's action is safely deposited
        if obj and obj.animation_data and obj.animation_data.use_tweak_mode:
            anim_core.invisible_tweakmode_swap(context, exit_first=True, enter_second=False)
            
        if obj and obj.animation_data:
            try:
                obj.animation_data.action = None  # Prevent old layer action from leaking!
            except: pass
            
        # Step 2: Safe to generate new tracks
        layer_data = obj.lsd_anim_layers_data
        scene_frame_end = max(float(context.scene.frame_end), 250.0) if context and context.scene else 250.0
        
        if len(layer_data.layers) == 0:
            if not obj.animation_data:
                obj.animation_data_create()
                
            if not obj.animation_data or getattr(obj.animation_data, 'action', None) is None:
                base_action = bpy.data.actions.new(name=f"{obj.name}_Base_Layer")
                try:
                    obj.animation_data.action = base_action
                except: pass
                anchor_frame = 1
                
                # Slotted Action support in Blender 4.3+ / 5.x
                if hasattr(base_action, 'slots'):
                    slot_name = obj.name
                    slot = base_action.slots.get(slot_name)
                    if not slot:
                        try: slot = base_action.slots.new(name=slot_name, id_type=getattr(obj, 'id_type', 'OBJECT'))
                        except Exception:
                            try: slot = base_action.slots.new(name=slot_name)
                            except Exception: pass
                    if slot:
                        try: obj.animation_data.action_slot = slot
                        except Exception: pass

                # Anchor the rest/current pose into the Base Layer
                try:
                    obj.keyframe_insert(data_path="location", frame=anchor_frame)
                    if getattr(obj, "rotation_mode", "QUATERNION") == 'QUATERNION':
                        obj.keyframe_insert(data_path="rotation_quaternion", frame=anchor_frame)
                    elif obj.rotation_mode == 'AXIS_ANGLE':
                        obj.keyframe_insert(data_path="rotation_axis_angle", frame=anchor_frame)
                    else:
                        obj.keyframe_insert(data_path="rotation_euler", frame=anchor_frame)
                    obj.keyframe_insert(data_path="scale", frame=anchor_frame)
                    if obj.type == 'ARMATURE' and obj.pose:
                        for pbone in obj.pose.bones:
                            bone_path = f'pose.bones["{pbone.name}"]'
                            obj.keyframe_insert(data_path=f'{bone_path}.location', frame=anchor_frame)
                            if getattr(pbone, "rotation_mode", "QUATERNION") == 'QUATERNION':
                                obj.keyframe_insert(data_path=f'{bone_path}.rotation_quaternion', frame=anchor_frame)
                            elif pbone.rotation_mode == 'AXIS_ANGLE':
                                obj.keyframe_insert(data_path=f'{bone_path}.rotation_axis_angle', frame=anchor_frame)
                            else:
                                obj.keyframe_insert(data_path=f'{bone_path}.rotation_euler', frame=anchor_frame)
                            obj.keyframe_insert(data_path=f'{bone_path}.scale', frame=anchor_frame)
                except Exception as e:
                    print(f"Error anchoring base pose: {e}")
            else:
                base_action = obj.animation_data.action
                if getattr(base_action, 'fcurves', None):
                    anchor_frame = int(base_action.frame_range[0])
                elif hasattr(base_action, 'frame_range') and (base_action.frame_range[0] != 0.0 or base_action.frame_range[1] != 0.0):
                    anchor_frame = int(base_action.frame_range[0])
                else:
                    anchor_frame = 1
                    
            base_layer = layer_data.layers.add()
            base_layer.name = "Base Layer"
            base_layer.blend_type = 'REPLACE'
            base_layer.is_muted = False
            base_layer.is_locked = True
            
            base_track = obj.animation_data.nla_tracks.new()
            base_track.name = base_layer.name
            base_layer.track_name = base_track.name
            
            base_strip = base_track.strips.new(name=base_layer.name, start=anchor_frame, action=base_action)
            base_strip.blend_type = 'REPLACE'
            base_strip.extrapolation = 'HOLD'
            if hasattr(base_strip, 'use_sync_length'):
                base_strip.use_sync_length = False
            base_strip.action_frame_start = float(anchor_frame)
            if hasattr(base_action, 'frame_range') and base_action.frame_range[1] > base_action.frame_range[0]:
                base_strip.action_frame_end = float(base_action.frame_range[1])
                base_strip.frame_end = float(anchor_frame) + (base_strip.action_frame_end - float(base_action.frame_range[0]))
            else:
                base_strip.action_frame_end = scene_frame_end
                base_strip.frame_end = scene_frame_end
            base_strip.frame_start = float(anchor_frame)
            base_strip.scale = 1.0
            anim_core.bind_strip_slot(obj, base_strip)
        
        layer = layer_data.layers.add()
        
        layer_count = sum(1 for l in layer_data.layers if l.name != "Base Layer")
        layer.name = f"Layer {layer_count}"
        
        if obj:
            if not obj.animation_data:
                obj.animation_data_create()
            track = obj.animation_data.nla_tracks.new()
            track.name = layer.name
            layer.track_name = track.name
            
            new_action = bpy.data.actions.new(name=f"{obj.name}_{layer.name}")
            slot = anim_core.ensure_action_slot(obj, new_action)
            if slot and hasattr(obj.animation_data, 'action_slot'):
                try: obj.animation_data.action_slot = slot
                except Exception: pass
                    
            strip = track.strips.new(name=layer.name, start=1, action=new_action)
            strip.blend_type = 'COMBINE'
            strip.extrapolation = 'HOLD'
            if hasattr(strip, 'use_sync_length'):
                strip.use_sync_length = False
            strip.action_frame_start = 1.0
            strip.action_frame_end = scene_frame_end
            strip.frame_start = 1.0
            strip.frame_end = scene_frame_end
            strip.scale = 1.0
            strip.blend_in = 0.0
            strip.blend_out = 0.0
            anim_core.bind_strip_slot(obj, strip, slot)
                
        layer_data.active_layer_index = len(layer_data.layers) - 1
        
        if context.view_layer:
            context.view_layer.update()
            
        anim_core.update_active_layer(settings, context)
        return {'FINISHED'}

class LSD_OT_Anim_Layer_Remove(bpy.types.Operator):
    bl_idname = "lsd.anim_layer_remove"
    bl_label = "Remove Animation Layer"
    bl_description = "Delete the active Animation Layer from the timeline stack"
    def execute(self, context):
        from . import anim_core
        obj = anim_core.get_active_object(context)
        if not obj or not hasattr(obj, 'lsd_anim_layers_data'):
            return {'CANCELLED'}
        layer_data = obj.lsd_anim_layers_data
        
        if layer_data.active_layer_index >= 0 and layer_data.active_layer_index < len(layer_data.layers):
            layer = layer_data.layers[layer_data.active_layer_index]
            
            # Step 1: Safely exit tweak mode before modifying tracks
            try:
                anim_core.invisible_tweakmode_swap(context, exit_first=True, enter_second=False)
            except: pass
            
            # Step 2: Remove the physical NLA Track and its Action
            if obj and obj.animation_data:
                if obj.animation_data.action:
                    try:
                        obj.animation_data.action = None
                    except Exception:
                        pass
                    
                track = obj.animation_data.nla_tracks.get(layer.track_name)
                if track:
                    if obj.animation_data.nla_tracks.active == track:
                        obj.animation_data.nla_tracks.active = None
                    track.select = False
                    track.mute = True
                    track.lock = True
                    
                    for strip in track.strips:
                        strip.select = False
                        strip.mute = True
                        
                    import time
                    del_name = f"__DEL_{track.name}_{time.time()}__"
                    track.name = del_name
                    
                    def safe_remove_track(obj_name, track_name):
                        try:
                            import bpy
                            o = bpy.data.objects.get(obj_name)
                            if o and o.animation_data:
                                t = o.animation_data.nla_tracks.get(track_name)
                                if t:
                                    for s in list(t.strips):
                                        t.strips.remove(s)
                                    o.animation_data.nla_tracks.remove(t)
                        except: pass
                        return None
                        
                    bpy.app.timers.register(lambda obj_name=obj.name, track_name=del_name: safe_remove_track(obj_name, track_name), first_interval=0.1)
                    
            if context.view_layer:
                context.view_layer.update()
            
            # Step 3: Remove from UI list
            layer_data.layers.remove(layer_data.active_layer_index)
            layer_data.active_layer_index = max(0, layer_data.active_layer_index - 1)
            
            # Re-sync active layer
            try:
                anim_core.update_active_layer(context.scene.lsd_anim_settings, context)
            except: pass
        return {'FINISHED'}

class LSD_OT_Anim_Layer_Move(bpy.types.Operator):
    bl_idname = "lsd.anim_layer_move"
    bl_label = "Move Animation Layer"
    direction: bpy.props.EnumProperty(items=[('UP', "Up", ""), ('DOWN', "Down", "")])
    def execute(self, context):
        from . import anim_core
        obj = anim_core.get_active_object(context)
        if not obj or not hasattr(obj, 'lsd_anim_layers_data'):
            return {'CANCELLED'}
        layer_data = obj.lsd_anim_layers_data
        idx = layer_data.active_layer_index
        if self.direction == 'UP' and idx > 0:
            layer_data.layers.move(idx, idx - 1)
            layer_data.active_layer_index -= 1
        elif self.direction == 'DOWN' and idx < len(layer_data.layers) - 1:
            layer_data.layers.move(idx, idx + 1)
            layer_data.active_layer_index += 1
        try:
            anim_core.update_active_layer(context.scene.lsd_anim_settings, context)
        except: pass
        return {'FINISHED'}

class LSD_OT_Anim_Merge_Bake(bpy.types.Operator):
    bl_idname = "lsd.anim_merge_bake"
    bl_label = "Merge / Bake"
    def execute(self, context): return {'FINISHED'}

class LSD_OT_Anim_Duplicate_Layer(bpy.types.Operator):
    bl_idname = "lsd.anim_duplicate_layer"
    bl_label = "Duplicate Layer"
    def execute(self, context): return {'FINISHED'}

class LSD_OT_Anim_Extract_Bones(bpy.types.Operator):
    bl_idname = "lsd.anim_extract_bones"
    bl_label = "Extract Selected Bones"
    def execute(self, context): return {'FINISHED'}

class LSD_OT_Anim_Extract_Keys(bpy.types.Operator):
    bl_idname = "lsd.anim_extract_keys"
    bl_label = "Extract Marked Keyframes"
    def execute(self, context): return {'FINISHED'}

class LSD_OT_Anim_Import_Selected_Keyframes(bpy.types.Operator):
    bl_idname = "lsd.anim_import_selected_keyframes"
    bl_label = "Import Selected Keyframes to Layer"
    def execute(self, context): return {'FINISHED'}

class LSD_OT_Anim_Bake_Layers_To_Keyframes(bpy.types.Operator):
    bl_idname = "lsd.anim_bake_layers_to_keyframes"
    bl_label = "Bake Layers to Keyframes"
    def execute(self, context): return {'FINISHED'}

class LSD_OT_Anim_Select_Bones(bpy.types.Operator):
    bl_idname = "lsd.anim_select_bones"
    bl_label = "Select Bones in Layer"
    def execute(self, context): return {'FINISHED'}

class LSD_OT_Anim_Reset_Key_Layer(bpy.types.Operator):
    bl_idname = "lsd.anim_reset_key_layer"
    bl_label = "Reset Key Layer"
    def execute(self, context): return {'FINISHED'}

class LSD_OT_Anim_Multikey_Edit(bpy.types.Operator):
    bl_idname = "lsd.anim_multikey_edit"
    bl_label = "Edit Selected Keyframes"
    def execute(self, context): return {'FINISHED'}

class LSD_OT_Anim_Cyclic_Fcurves(bpy.types.Operator):
    bl_idname = "lsd.anim_cyclic_fcurves"
    bl_label = "Cyclic Fcurves"
    def execute(self, context): return {'FINISHED'}

class LSD_OT_Anim_Remove_Fcurves(bpy.types.Operator):
    bl_idname = "lsd.anim_remove_fcurves"
    bl_label = "Remove Fcurves"
    def execute(self, context): return {'FINISHED'}

class LSD_OT_Anim_Custom_Frame_Range(bpy.types.Operator):
    bl_idname = "lsd.anim_custom_frame_range"
    bl_label = "Custom Frame Range"
    def execute(self, context): return {'FINISHED'}

class LSD_OT_Anim_Sync_To_Action(bpy.types.Operator):
    bl_idname = "lsd.anim_sync_to_action"
    bl_label = "Sync to Action"
    def execute(self, context): return {'FINISHED'}

# --- Paint Layers Operators ---
class LSD_OT_Paint_Layer_Add(bpy.types.Operator):
    bl_idname = "lsd.paint_layer_add"
    bl_label = "Add Paint Layer"
    def execute(self, context):
        layers = context.scene.lsd_paint_layers
        new_layer = layers.add()
        new_layer.name = f"Layer {len(layers)}"
        context.scene.lsd_active_paint_layer_index = len(layers) - 1
        return {'FINISHED'}

class LSD_OT_Paint_Layer_Remove(bpy.types.Operator):
    bl_idname = "lsd.paint_layer_remove"
    bl_label = "Remove Paint Layer"
    def execute(self, context):
        layers = context.scene.lsd_paint_layers
        idx = context.scene.lsd_active_paint_layer_index
        if len(layers) > 0 and 0 <= idx < len(layers):
            layers.remove(idx)
            if context.scene.lsd_active_paint_layer_index >= len(layers):
                context.scene.lsd_active_paint_layer_index = max(0, len(layers) - 1)
        return {'FINISHED'}

class LSD_OT_Paint_Layer_Move(bpy.types.Operator):
    bl_idname = "lsd.paint_layer_move"
    bl_label = "Move Paint Layer"
    direction: bpy.props.EnumProperty(items=[('UP', 'Up', ''), ('DOWN', 'Down', '')])
    
    def execute(self, context):
        layers = context.scene.lsd_paint_layers
        idx = context.scene.lsd_active_paint_layer_index
        if self.direction == 'UP' and idx > 0:
            layers.move(idx, idx - 1)
            context.scene.lsd_active_paint_layer_index -= 1
        elif self.direction == 'DOWN' and idx < len(layers) - 1:
            layers.move(idx, idx + 1)
            context.scene.lsd_active_paint_layer_index += 1
        return {'FINISHED'}
class LSD_OT_Paint_Layer_Select(bpy.types.Operator):
    bl_idname = "lsd.paint_layer_select"
    bl_label = "Select Paint Layer"
    def execute(self, context): return {'FINISHED'}

class LSD_OT_Anim_Layer_Remove(bpy.types.Operator):
    bl_idname = "lsd.anim_layer_remove"
    bl_label = "Remove Animation Layer"
    def execute(self, context):
        settings = context.scene.lsd_anim_settings
        
        from . import anim_core
        obj = anim_core.get_active_object(context)
        if not obj or not hasattr(obj, 'lsd_anim_layers_data'):
            return {'CANCELLED'}
            
        layer_data = obj.lsd_anim_layers_data
        idx = layer_data.active_layer_index
        if idx >= 0 and idx < len(layer_data.layers):
            layer_to_remove = layer_data.layers[idx]
            layer_name = layer_to_remove.name
            track_name = layer_to_remove.track_name if hasattr(layer_to_remove, 'track_name') and layer_to_remove.track_name else layer_name
            
            # Remove NLA tracks and their Actions from the active object ONLY
            if obj.animation_data:
                track = obj.animation_data.nla_tracks.get(track_name)
                if not track:
                    track = obj.animation_data.nla_tracks.get(layer_name)
                    
                if track:
                    # Collect and delete all actions associated with this track's strips
                    actions_to_delete = []
                    for strip in track.strips:
                        if strip.action:
                            actions_to_delete.append(strip.action)
                            try:
                                if obj.animation_data.action == strip.action:
                                    obj.animation_data.action = None
                            except: pass
                                
                    # Safely detach the track logically without ripping it from memory instantly
                    if obj.animation_data.nla_tracks.active == track:
                        obj.animation_data.nla_tracks.active = None
                        
                    track.name = track.name + "_DELETING"
                    track.mute = True
                    for strip in track.strips:
                        strip.mute = True
                    
                    # We MUST defer the actual memory deletion by 2.0s to prevent EXCEPTION_ACCESS_VIOLATION crashes
                    # when the user rapidly deletes multiple layers while Blender is actively evaluating them.
                    def _delayed_remove_layer(obj_name=obj.name, t_name=track.name, action_names=[a.name for a in actions_to_delete]):
                        try:
                            import bpy
                            o = bpy.data.objects.get(obj_name)
                            if o and o.animation_data:
                                t = o.animation_data.nla_tracks.get(t_name)
                                if t:
                                    o.animation_data.nla_tracks.remove(t)
                                    
                            for aname in action_names:
                                a = bpy.data.actions.get(aname)
                                if a and a.users == 0:
                                    bpy.data.actions.remove(a)
                        except: pass
                        return None
                    import bpy
                    bpy.app.timers.register(_delayed_remove_layer, first_interval=2.0)
                        
            # Remove the layer from the object's UI list
            layer_data.layers.remove(idx)
            layer_data.active_layer_index = max(0, idx - 1)
            
            # Sync tweak mode
            anim_core.update_active_layer(settings, context)
            
        return {'FINISHED'}

class LSD_OT_Anim_Layer_Move(bpy.types.Operator):
    bl_idname = "lsd.anim_layer_move"
    bl_label = "Move Animation Layer"
    direction: bpy.props.EnumProperty(items=[('UP', "Up", ""), ('DOWN', "Down", "")])
    def execute(self, context):
        from . import anim_core
        obj = anim_core.get_active_object(context)
        if not obj or not hasattr(obj, 'lsd_anim_layers_data'): return {'CANCELLED'}
        
        layer_data = obj.lsd_anim_layers_data
        idx = layer_data.active_layer_index
        if self.direction == 'UP' and idx > 0:
            layer_data.layers.move(idx, idx - 1)
            layer_data.active_layer_index -= 1
        elif self.direction == 'DOWN' and idx < len(layer_data.layers) - 1:
            layer_data.layers.move(idx, idx + 1)
            layer_data.active_layer_index += 1
        return {'FINISHED'}

class LSD_OT_Anim_Merge_Bake(bpy.types.Operator):
    bl_idname = "lsd.anim_merge_bake"
    bl_label = "Merge / Bake"
    def execute(self, context): return {'FINISHED'}

class LSD_OT_Anim_Duplicate_Layer(bpy.types.Operator):
    bl_idname = "lsd.anim_duplicate_layer"
    bl_label = "Duplicate Layer"
    def execute(self, context): return {'FINISHED'}

class LSD_OT_Anim_Extract_Bones(bpy.types.Operator):
    bl_idname = "lsd.anim_extract_bones"
    bl_label = "Extract Selected Bones"
    def execute(self, context): return {'FINISHED'}

class LSD_OT_Anim_Extract_Keys(bpy.types.Operator):
    bl_idname = "lsd.anim_extract_keys"
    bl_label = "Extract Marked Keyframes"
    def execute(self, context): return {'FINISHED'}

class LSD_OT_Anim_Import_Selected_Keyframes(bpy.types.Operator):
    bl_idname = "lsd.anim_import_selected_keyframes"
    bl_label = "Import Selected Keyframes to Layer"
    def execute(self, context): return {'FINISHED'}

class LSD_OT_Anim_Bake_Layers_To_Keyframes(bpy.types.Operator):
    bl_idname = "lsd.anim_bake_layers_to_keyframes"
    bl_label = "Bake Layers to Keyframes"
    def execute(self, context): return {'FINISHED'}

class LSD_OT_Anim_Select_Bones(bpy.types.Operator):
    bl_idname = "lsd.anim_select_bones"
    bl_label = "Select Bones in Layer"
    def execute(self, context): return {'FINISHED'}

class LSD_OT_Anim_Reset_Key_Layer(bpy.types.Operator):
    bl_idname = "lsd.anim_reset_key_layer"
    bl_label = "Reset Key Layer"
    def execute(self, context): return {'FINISHED'}

class LSD_OT_Anim_Multikey_Edit(bpy.types.Operator):
    bl_idname = "lsd.anim_multikey_edit"
    bl_label = "Edit Selected Keyframes"
    def execute(self, context): return {'FINISHED'}

class LSD_OT_Anim_Cyclic_Fcurves(bpy.types.Operator):
    bl_idname = "lsd.anim_cyclic_fcurves"
    bl_label = "Cyclic Fcurves"
    def execute(self, context): return {'FINISHED'}

class LSD_OT_Anim_Remove_Fcurves(bpy.types.Operator):
    bl_idname = "lsd.anim_remove_fcurves"
    bl_label = "Remove Fcurves"
    def execute(self, context): return {'FINISHED'}

class LSD_OT_Anim_Custom_Frame_Range(bpy.types.Operator):
    bl_idname = "lsd.anim_custom_frame_range"
    bl_label = "Custom Frame Range"
    def execute(self, context): return {'FINISHED'}

class LSD_OT_Anim_Sync_To_Action(bpy.types.Operator):
    bl_idname = "lsd.anim_sync_to_action"
    bl_label = "Sync to Action"
    def execute(self, context): return {'FINISHED'}

class LSD_OT_NM_Pick_Cutter_From_Selection(bpy.types.Operator):
    """Sets the other selected mesh object as the Cutter Object"""
    bl_idname = "lsd.nm_pick_cutter_from_selection"
    bl_label = "Pick Cutter from Selection"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        active = context.active_object
        return active and active.type == 'MESH' and any(o for o in context.selected_objects if o != active and o.type == 'MESH')
        
    def execute(self, context):
        active = context.active_object
        sel = [o for o in context.selected_objects if o != active and o.type == 'MESH']
        if not sel:
            self.report({'WARNING'}, "No other mesh object selected")
            return {'CANCELLED'}
        cutter = sel[0]
        props = getattr(active, "lsd_pg_sdf_props", None)
        if props:
            props.target_object = cutter
            cutter.display_type = 'BOUNDS'
            self.report({'INFO'}, f"Set '{cutter.name}' as cutter")
        return {'FINISHED'}

class LSD_OT_NM_Quick_Boolean(bpy.types.Operator):
    """Executes a direct NormalMagic Boolean operation with the selected cutter"""
    bl_idname = "lsd.nm_quick_boolean"
    bl_label = "Quick Boolean"
    bl_options = {'REGISTER', 'UNDO'}
    
    operation: bpy.props.EnumProperty(
        items=[
            ('DIFFERENCE', "Difference", ""),
            ('UNION', "Union", ""),
            ('INTERSECT', "Intersection", ""),
            ('SLICE', "Slice", "")
        ],
        default='DIFFERENCE'
    )
    
    @classmethod
    def poll(cls, context):
        return context.active_object and context.active_object.type == 'MESH'
        
    def execute(self, context):
        active = context.active_object
        props = getattr(active, "lsd_pg_sdf_props", None)
        cutter = props.target_object if props else None
        
        if not cutter:
            sel = [o for o in context.selected_objects if o != active and o.type == 'MESH']
            if sel:
                cutter = sel[0]
                if props: props.target_object = cutter
                
        if not cutter or cutter == active:
            self.report({'WARNING'}, "Please select a cutter object or select two mesh objects in the viewport")
            return {'CANCELLED'}
            
        if props:
            props.boolean_operation = self.operation
            
        from . import generators
        generators.apply_boolean_pro(
            obj=active,
            cutter=cutter,
            operation=self.operation,
            solver=props.boolean_solver if props else 'MANIFOLD',
            transfer_normals=props.transfer_normals if props else True,
            outset=props.outset_thickness if props else 0.0,
            inset=props.inset_thickness if props else 0.0,
            weld_enabled=props.weld_enabled if props else False,
            weld_distance=props.weld_distance if props else 0.0001,
            materials_mode=props.materials_mode if props else 'INDEX_BASED',
            materials_slot_index=props.materials_slot_index if props else 0,
            texture_blur=props.texture_blur if props else 0.0,
            bevel_weld_radius=props.bevel_weld_radius if props else 0.0
        )
        self.report({'INFO'}, f"Applied Boolean ({self.operation}) with {cutter.name}")
        return {'FINISHED'}

class LSD_OT_NM_Boolean_Pro(bpy.types.Operator):
    """Apply or remove Boolean Pro modifier stack using NormalMagic workflow"""
    bl_idname = "lsd.nm_boolean_pro"
    bl_label = "Boolean"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        return context.active_object and context.active_object.type == 'MESH'
        
    def execute(self, context):
        obj = context.active_object
        props = getattr(obj, "lsd_pg_sdf_props", None)
        if not props:
            self.report({'WARNING'}, "No Boolean Pro properties found")
            return {'CANCELLED'}
            
        cutter = props.target_object
        has_bool = "NM_Boolean" in obj.modifiers
        
        if has_bool:
            for m in list(obj.modifiers):
                if m.name in {"NM_Boolean", "NM_Weld", "NM_Bevel_Weld", "NM_Texture_Blur", "NM_Normal_Transfer"}:
                    obj.modifiers.remove(m)
            if cutter:
                for m in list(cutter.modifiers):
                    if m.name in {"NM_Outset", "NM_Inset"}:
                        cutter.modifiers.remove(m)
                cutter.display_type = 'TEXTURED'
            self.report({'INFO'}, "Removed Boolean Pro modifiers")
        else:
            if not cutter:
                sel = [o for o in context.selected_objects if o != obj and o.type == 'MESH']
                if sel:
                    cutter = sel[0]
                    props.target_object = cutter
            
            if not cutter or cutter == obj:
                self.report({'WARNING'}, "Please select a Cutter Object or select 2 mesh objects in the viewport")
                return {'CANCELLED'}
                
            from . import generators
            generators.apply_boolean_pro(
                obj=obj,
                cutter=cutter,
                operation=props.boolean_operation,
                solver=props.boolean_solver,
                transfer_normals=props.transfer_normals,
                outset=props.outset_thickness,
                inset=props.inset_thickness,
                weld_enabled=props.weld_enabled,
                weld_distance=props.weld_distance,
                materials_mode=props.materials_mode,
                materials_slot_index=props.materials_slot_index,
                texture_blur=props.texture_blur,
                bevel_weld_radius=props.bevel_weld_radius
            )
            self.report({'INFO'}, f"Applied Boolean Pro ({props.boolean_operation}) with {cutter.name}")
            
        return {'FINISHED'}

class LSD_OT_NM_Surface_Project(bpy.types.Operator):
    """Project geometry and normals onto a target surface"""
    bl_idname = "lsd.nm_surface_project"
    bl_label = "Surface Project"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        return context.active_object is not None
        
    def execute(self, context):
        obj = context.active_object
        props = getattr(obj, "lsd_pg_sdf_props", None)
        target = props.target_object if props else None
        
        if not target:
            sel = [o for o in context.selected_objects if o != obj and o.type == 'MESH']
            if sel:
                target = sel[0]
                if props: props.target_object = target
                
        has_proj = "NM_Surface_Project" in obj.modifiers
        if has_proj:
            mod = obj.modifiers.get("NM_Surface_Project")
            if mod: obj.modifiers.remove(mod)
            obj.display_type = 'TEXTURED'
            self.report({'INFO'}, "Removed Surface Project")
        else:
            if not target:
                self.report({'WARNING'}, "Please specify a Target Object")
                return {'CANCELLED'}
            from . import generators
            generators.apply_surface_project(obj, target)
            self.report({'INFO'}, "Applied Surface Project")
        return {'FINISHED'}

class LSD_OT_NM_Surface_Insert(bpy.types.Operator):
    """Insert and blend a projected cutter mesh into target surface"""
    bl_idname = "lsd.nm_surface_insert"
    bl_label = "Surface Insert"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        return context.active_object is not None
        
    def execute(self, context):
        obj = context.active_object
        props = getattr(obj, "lsd_pg_sdf_props", None)
        target = props.target_object if props else None
        
        if not target:
            sel = [o for o in context.selected_objects if o != obj and o.type == 'MESH']
            if sel:
                target = sel[0]
                if props: props.target_object = target
                
        has_ins = "NM_Surface_Insert" in obj.modifiers
        if has_ins:
            mod = obj.modifiers.get("NM_Surface_Insert")
            if mod: obj.modifiers.remove(mod)
            if target:
                mod_cut = target.modifiers.get("NM_Insert_Cut")
                if mod_cut: target.modifiers.remove(mod_cut)
            obj.display_type = 'TEXTURED'
            self.report({'INFO'}, "Removed Surface Insert")
        else:
            if not target:
                self.report({'WARNING'}, "Please specify a Target Object")
                return {'CANCELLED'}
            from . import generators
            generators.apply_surface_insert(obj, target)
            self.report({'INFO'}, "Applied Surface Insert")
        return {'FINISHED'}

class LSD_OT_NM_Normal_Weighted(bpy.types.Operator):
    """Adds or removes weighted normals for clean face shading"""
    bl_idname = "lsd.nm_normal_weighted"
    bl_label = "Weighted Normals"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        return context.active_object and context.active_object.type == 'MESH'
        
    def execute(self, context):
        obj = context.active_object
        has_wn = "NM_Weighted_Normal" in obj.modifiers
        if has_wn:
            mod = obj.modifiers.get("NM_Weighted_Normal")
            if mod: obj.modifiers.remove(mod)
            self.report({'INFO'}, "Removed Weighted Normals")
        else:
            from . import generators
            generators.apply_weighted_normal(obj)
            self.report({'INFO'}, "Applied Weighted Normals")
        return {'FINISHED'}

class LSD_OT_NM_Smooth_Normals(bpy.types.Operator):
    """Smooths and flattens normals for toon or stylized shading"""
    bl_idname = "lsd.nm_smooth_normals"
    bl_label = "Smooth Normals"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        return context.active_object and context.active_object.type == 'MESH'
        
    def execute(self, context):
        obj = context.active_object
        has_mod = "NM_Smooth_Normals" in obj.modifiers
        if has_mod:
            mod = obj.modifiers.get("NM_Smooth_Normals")
            if mod: obj.modifiers.remove(mod)
            self.report({'INFO'}, "Removed Smooth Normals")
        else:
            from . import generators
            generators.apply_smooth_normals(obj)
            self.report({'INFO'}, "Applied Smooth Normals")
        return {'FINISHED'}

class LSD_OT_NM_Normal_Transfer(bpy.types.Operator):
    """Transfers normals from target mesh to active mesh"""
    bl_idname = "lsd.nm_normal_transfer"
    bl_label = "Normal Transfer"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        return context.active_object and context.active_object.type == 'MESH'
        
    def execute(self, context):
        obj = context.active_object
        props = getattr(obj, "lsd_pg_sdf_props", None)
        target = props.target_object if props else None
        
        has_mod = "NM_Normal_Transfer" in obj.modifiers
        if has_mod:
            mod = obj.modifiers.get("NM_Normal_Transfer")
            if mod: obj.modifiers.remove(mod)
            self.report({'INFO'}, "Removed Normal Transfer")
        else:
            if not target:
                self.report({'WARNING'}, "Please specify a Target Object")
                return {'CANCELLED'}
            from . import generators
            generators.apply_normal_transfer(obj, target)
            self.report({'INFO'}, "Applied Normal Transfer")
        return {'FINISHED'}

class LSD_OT_NM_Repair_Boolean_Normals(bpy.types.Operator):
    """Destructively recalculates and cleans custom normals on a boolean result"""
    bl_idname = "lsd.nm_repair_boolean_normals"
    bl_label = "Repair Boolean Normals"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        return context.active_object and context.active_object.type == 'MESH'
        
    def execute(self, context):
        obj = context.active_object
        from . import generators
        generators.repair_boolean_normals(obj)
        self.report({'INFO'}, "Repaired boolean normals")
        return {'FINISHED'}

class LSD_OT_NM_Repair_Bevel_Normals(bpy.types.Operator):
    """Repairs bevel normal shading on active object"""
    bl_idname = "lsd.nm_repair_bevel_normals"
    bl_label = "Repair Bevel Normals"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        return context.active_object and context.active_object.type == 'MESH'
        
    def execute(self, context):
        obj = context.active_object
        from . import generators
        generators.repair_bevel_normals(obj)
        self.report({'INFO'}, "Repaired bevel normals")
        return {'FINISHED'}

class LSD_OT_NM_Boolean_Extrude(bpy.types.Operator):
    """Extrudes selected faces along normal in Edit Mode"""
    bl_idname = "lsd.nm_boolean_extrude"
    bl_label = "Boolean Extrude"
    bl_options = {'REGISTER', 'UNDO'}
    
    distance: bpy.props.FloatProperty(name="Distance", default=0.05, unit='LENGTH')
    
    @classmethod
    def poll(cls, context):
        return context.active_object and context.active_object.type == 'MESH' and context.mode == 'EDIT_MESH'
        
    def execute(self, context):
        obj = context.active_object
        from . import generators
        generators.apply_boolean_extrude(obj, self.distance)
        self.report({'INFO'}, "Extruded faces")
        return {'FINISHED'}

class LSD_OT_NM_Cut_Groove(bpy.types.Operator):
    """Adds a procedural cut groove bevel modifier"""
    bl_idname = "lsd.nm_cut_groove"
    bl_label = "Cut Groove"
    bl_options = {'REGISTER', 'UNDO'}
    
    depth: bpy.props.FloatProperty(name="Depth", default=0.01, unit='LENGTH')
    width: bpy.props.FloatProperty(name="Width", default=0.005, unit='LENGTH')
    
    @classmethod
    def poll(cls, context):
        return context.active_object and context.active_object.type == 'MESH'
        
    def execute(self, context):
        obj = context.active_object
        from . import generators
        generators.apply_cut_groove(obj, self.depth, self.width)
        self.report({'INFO'}, "Applied Cut Groove modifier")
        return {'FINISHED'}

class LSD_OT_NM_Boolean_Trim(bpy.types.Operator):
    """Trims geometry by applying a planar difference boolean"""
    bl_idname = "lsd.nm_boolean_trim"
    bl_label = "Boolean Trim"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        return context.active_object and context.active_object.type == 'MESH'
        
    def execute(self, context):
        obj = context.active_object
        from . import generators
        generators.apply_boolean_trim(obj, context)
        self.report({'INFO'}, "Applied Boolean Trim")
        return {'FINISHED'}

class LSD_OT_NM_View_Normals(bpy.types.Operator):
    """Toggles viewport display of face normals"""
    bl_idname = "lsd.nm_view_normals"
    bl_label = "View Normals"
    
    def execute(self, context):
        space = context.space_data
        if space and hasattr(space, 'overlay'):
            space.overlay.show_face_normals = not space.overlay.show_face_normals
            state = "ON" if space.overlay.show_face_normals else "OFF"
            self.report({'INFO'}, f"Face normals overlay: {state}")
        return {'FINISHED'}

class LSD_OT_NM_View_Sharp(bpy.types.Operator):
    """Toggles viewport display of sharp edges"""
    bl_idname = "lsd.nm_view_sharp"
    bl_label = "View Sharp"
    
    def execute(self, context):
        space = context.space_data
        if space and hasattr(space, 'overlay'):
            space.overlay.show_edge_sharp = not space.overlay.show_edge_sharp
            state = "ON" if space.overlay.show_edge_sharp else "OFF"
            self.report({'INFO'}, f"Sharp edges overlay: {state}")
        return {'FINISHED'}

class LSD_OT_NM_Mark_Sharp(bpy.types.Operator):
    """Marks selected edges as sharp in Edit Mode"""
    bl_idname = "lsd.nm_mark_sharp"
    bl_label = "Mark Sharp"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        return context.mode == 'EDIT_MESH'
        
    def execute(self, context):
        bpy.ops.mesh.mark_sharp()
        self.report({'INFO'}, "Marked selected edges as sharp")
        return {'FINISHED'}

class LSD_OT_NM_Apply_Modifiers(bpy.types.Operator):
    """Applies all modifiers on the active object sequentially"""
    bl_idname = "lsd.nm_apply_modifiers"
    bl_label = "Apply All Modifiers"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        return context.active_object and context.active_object.type == 'MESH'
        
    def execute(self, context):
        obj = context.active_object
        initial_mode = context.mode
        if initial_mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
            
        applied_count = 0
        try:
            for mod in list(obj.modifiers):
                try:
                    bpy.ops.object.modifier_apply(modifier=mod.name)
                    applied_count += 1
                except Exception as e:
                    print(f"Could not apply modifier {mod.name}: {e}")
        finally:
            if initial_mode != 'OBJECT' and initial_mode in {'EDIT_MESH', 'EDIT'}:
                try: bpy.ops.object.mode_set(mode='EDIT')
                except: pass
                
        self.report({'INFO'}, f"Applied {applied_count} modifier(s)")
        return {'FINISHED'}

# --- Paint Layers Operators ---
class LSD_OT_Paint_Layer_Add(bpy.types.Operator):
    bl_idname = "lsd.paint_layer_add"
    bl_label = "Add Paint Layer"
    def execute(self, context):
        layers = context.scene.lsd_paint_layers
        new_layer = layers.add()
        new_layer.name = f"Layer {len(layers)}"
        context.scene.lsd_active_paint_layer_index = len(layers) - 1
        return {'FINISHED'}

class LSD_OT_Paint_Layer_Remove(bpy.types.Operator):
    bl_idname = "lsd.paint_layer_remove"
    bl_label = "Remove Paint Layer"
    def execute(self, context):
        layers = context.scene.lsd_paint_layers
        idx = context.scene.lsd_active_paint_layer_index
        if len(layers) > 0 and 0 <= idx < len(layers):
            layers.remove(idx)
            if context.scene.lsd_active_paint_layer_index >= len(layers):
                context.scene.lsd_active_paint_layer_index = max(0, len(layers) - 1)
        return {'FINISHED'}

class LSD_OT_Paint_Layer_Move(bpy.types.Operator):
    bl_idname = "lsd.paint_layer_move"
    bl_label = "Move Paint Layer"
    direction: bpy.props.EnumProperty(items=[('UP', 'Up', ''), ('DOWN', 'Down', '')])
    
    def execute(self, context):
        layers = context.scene.lsd_paint_layers
        idx = context.scene.lsd_active_paint_layer_index
        if self.direction == 'UP' and idx > 0:
            layers.move(idx, idx - 1)
            context.scene.lsd_active_paint_layer_index -= 1
        elif self.direction == 'DOWN' and idx < len(layers) - 1:
            layers.move(idx, idx + 1)
            context.scene.lsd_active_paint_layer_index += 1
        return {'FINISHED'}

class LSD_OT_Paint_Layer_Select(bpy.types.Operator):
    bl_idname = "lsd.paint_layer_select"
    bl_label = "Select Paint Layer"
    index: bpy.props.IntProperty()
    
    def execute(self, context):
        context.scene.lsd_active_paint_layer_index = self.index
        return {'FINISHED'}

class LSD_OT_SnapToNearestKeyframe(bpy.types.Operator):
    """Snap to the nearest frame with keyframes on the timeline"""
    bl_idname = "lsd.snap_to_nearest_keyframe"
    bl_label = "Snap to Nearest Keyframe"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        return context.active_object and context.active_object.animation_data and context.active_object.animation_data.action

    def execute(self, context):
        obj = context.active_object
        action = obj.animation_data.action
        curr_frame = context.scene.frame_current
        
        keyframes = set()
        for fcurve in action.fcurves:
            for key in fcurve.keyframe_points:
                keyframes.add(int(key.co.x))
                
        if not keyframes:
            self.report({'WARNING'}, "No keyframes found")
            return {'CANCELLED'}
            
        closest_frame = None
        min_dist = float('inf')
        
        for kf in keyframes:
            if kf == curr_frame:
                continue
            dist = abs(kf - curr_frame)
            if dist < min_dist:
                min_dist = dist
                closest_frame = kf
                
        if closest_frame is not None:
            context.scene.frame_set(closest_frame)
            self.report({'INFO'}, f"Snapped to frame {closest_frame}")
        else:
            self.report({'WARNING'}, "Already on the only keyframe")
            
        return {'FINISHED'}


class LSD_OT_SnapToKeyframe(bpy.types.Operator):
    """Snap to nearest past or future keyframe"""
    bl_idname = "lsd.snap_to_keyframe"
    bl_label = "Snap to Keyframe"
    bl_options = {'REGISTER', 'UNDO'}
    
    direction: bpy.props.EnumProperty(items=[('PAST', 'Past', ''), ('FUTURE', 'Future', '')])
    
    @classmethod
    def poll(cls, context):
        return context.active_object and context.active_object.animation_data
        
    def execute(self, context):
        obj = context.active_object
        curr_frame = context.scene.frame_current
        keyframes = set()
            
        if not keyframes and obj.animation_data:
            target_action = obj.animation_data.action
            strip_frame_start = 0
            strip_action_frame_start = 0
            strip_scale = 1.0

            if not target_action and hasattr(obj, 'lsd_anim_layers_data'):
                ldata = obj.lsd_anim_layers_data
                if 0 <= ldata.active_layer_index < len(ldata.layers):
                    al = ldata.layers[ldata.active_layer_index]
                    for tr in obj.animation_data.nla_tracks:
                        if (tr.name == al.name or tr.name == al.track_name) and tr.strips:
                            st = tr.strips[0]
                            target_action = st.action
                            strip_frame_start = st.frame_start
                            strip_action_frame_start = st.action_frame_start
                            strip_scale = st.scale
                            break

            if getattr(obj.animation_data, 'use_tweak_mode', False) and target_action:
                for track in obj.animation_data.nla_tracks:
                    for strip in track.strips:
                        if strip.action == target_action:
                            strip_frame_start = strip.frame_start
                            strip_action_frame_start = strip.action_frame_start
                            strip_scale = strip.scale
                            break
                            
            if target_action:
                import layouts_systems_draftsman_toolkit.anim_core as anim_core
                fcurves_list = anim_core.get_action_fcurves(obj, target_action)
            
            for fcurve in fcurves_list:
                length = len(fcurve.keyframe_points)
                if length == 0:
                    continue
                
                try:
                    # Fast C-level extraction for massive speedup
                    coords = [0.0] * (length * 2)
                    fcurve.keyframe_points.foreach_get('co', coords)
                    for i in range(length):
                        scene_frame = round(strip_frame_start + (coords[i*2] - strip_action_frame_start) * strip_scale)
                        keyframes.add(scene_frame)
                except:
                    # Fallback for older Blender versions
                    for kp in fcurve.keyframe_points:
                        scene_frame = round(strip_frame_start + (kp.co.x - strip_action_frame_start) * strip_scale)
                        keyframes.add(scene_frame)
                        
        if not keyframes:
            self.report({'WARNING'}, "No keyframes found")
            return {'CANCELLED'}
            
        target_frame = None
        
        if self.direction == 'PAST':
            past_kfs = sorted([k for k in keyframes if k < curr_frame])
            if past_kfs:
                if context.screen.is_animation_playing and len(past_kfs) > 1:
                    target_frame = past_kfs[-2]
                else:
                    target_frame = past_kfs[-1]
        elif self.direction == 'FUTURE':
            future_kfs = [k for k in keyframes if k > curr_frame]
            if future_kfs:
                target_frame = min(future_kfs)
                
        if target_frame is not None:
            was_playing = context.screen.is_animation_playing
            if was_playing:
                try: bpy.ops.screen.animation_cancel(restore_frame=False)
                except: pass
                
            context.scene.frame_set(target_frame)
            
            if was_playing:
                try: bpy.ops.screen.animation_play()
                except: pass
                
            self.report({'INFO'}, f"Snapped to frame {target_frame}")
        else:
            self.report({'WARNING'}, f"No {self.direction.lower()} keyframes found")
            
        return {'FINISHED'}

class LSD_OT_Global_Math_Input(bpy.types.Operator):
    """Global Math Calculator"""
    bl_idname = "lsd.global_math_input"
    bl_label = "Math Input"
    bl_options = {'REGISTER', 'INTERNAL'}
    
    expression: bpy.props.StringProperty(name="Expression", description="Enter math expression")

    @classmethod
    def poll(cls, context):
        return getattr(context.scene, "lsd_enable_math_input", False)

    def execute(self, context):
        try:
            import math, re
            allowed_names = {k: v for k, v in math.__dict__.items() if not k.startswith("__")}
            expr = self.expression
            expr = re.sub(r'(\d|\))\s*\(', r'\1*(', expr)
            result = eval(expr, {"__builtins__": {}}, allowed_names)
            context.window_manager.clipboard = str(result)
            self.report({'INFO'}, f"Result: {result} (Copied to Clipboard)")
        except Exception as e:
            self.report({'ERROR'}, f"Invalid Math: {e}")
            return {'CANCELLED'}
        return {'FINISHED'}

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)


import shutil



from bpy_extras.io_utils import ImportHelper
import shutil

class LSD_OT_SelectStartupBlend(bpy.types.Operator, ImportHelper):
    bl_idname = "lsd.select_startup_blend"
    bl_label = "Select startup.blend File"
    bl_description = "Open the file browser to locate and select a replacement startup.blend file"
    
    filter_glob: bpy.props.StringProperty(default="*.blend", options={'HIDDEN'})
    
    def execute(self, context):
        context.scene.lsd_startup_blend_path = self.filepath
        return {'FINISHED'}

class LSD_OT_SelectUserprefBlend(bpy.types.Operator, ImportHelper):
    bl_idname = "lsd.select_userpref_blend"
    bl_label = "Select userpref.blend File"
    bl_description = "Open the file browser to locate and select a replacement userpref.blend file"
    
    filter_glob: bpy.props.StringProperty(default="*.blend", options={'HIDDEN'})
    
    def execute(self, context):
        context.scene.lsd_userpref_blend_path = self.filepath
        return {'FINISHED'}

class LSD_OT_SetStartupFiles(bpy.types.Operator):
    bl_idname = "lsd.set_startup_files"
    bl_label = "Set as Default Startup Files"
    bl_description = "Copy the selected configuration files directly into the active Blender configuration directory"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return True

    def execute(self, context):
        scene = context.scene
        startup_path = bpy.path.abspath(scene.lsd_startup_blend_path)
        userpref_path = bpy.path.abspath(scene.lsd_userpref_blend_path)
        
        config_dir = bpy.utils.user_resource('CONFIG')
        
        if not os.path.exists(config_dir):
            self.report({'ERROR'}, f"Config directory not found: {config_dir}")
            return {'CANCELLED'}
            
        success_count = 0
        
        if startup_path and os.path.exists(startup_path) and startup_path.endswith('.blend'):
            dest = os.path.join(config_dir, "startup.blend")
            try:
                shutil.copy2(startup_path, dest)
                success_count += 1
            except Exception as e:
                self.report({'ERROR'}, f"Failed to copy startup.blend: {e}")
                
        if userpref_path and os.path.exists(userpref_path) and userpref_path.endswith('.blend'):
            dest = os.path.join(config_dir, "userpref.blend")
            try:
                shutil.copy2(userpref_path, dest)
                success_count += 1
            except Exception as e:
                self.report({'ERROR'}, f"Failed to copy userpref.blend: {e}")
                
        if success_count == 0:
            self.report({'WARNING'}, "No valid files selected or copied.")
        else:
            self.report({'INFO'}, f"Successfully set {success_count} config file(s)! Restarting Blender...")
            import subprocess
            subprocess.Popen([bpy.app.binary_path])
            bpy.ops.wm.quit_blender()
            
        return {'FINISHED'}

def register():
    CLASSES = [
        LSD_OT_SetStartupFiles, LSD_OT_SelectStartupBlend, LSD_OT_SelectUserprefBlend,
        LSD_OT_NM_Pick_Cutter_From_Selection,
        LSD_OT_NM_Quick_Boolean,
        LSD_OT_NM_Boolean_Pro,
        LSD_OT_NM_Surface_Project,
        LSD_OT_NM_Surface_Insert,
        LSD_OT_NM_Normal_Weighted,
        LSD_OT_NM_Smooth_Normals,
        LSD_OT_NM_Normal_Transfer,
        LSD_OT_NM_Repair_Boolean_Normals,
        LSD_OT_NM_Repair_Bevel_Normals,
        LSD_OT_NM_Boolean_Extrude,
        LSD_OT_NM_Cut_Groove,
        LSD_OT_NM_Boolean_Trim,
        LSD_OT_NM_View_Normals,
        LSD_OT_NM_View_Sharp,
        LSD_OT_NM_Mark_Sharp,
        LSD_OT_NM_Apply_Modifiers,

        LSD_OT_Sync_Active_Layer,
        LSD_OT_Anim_Refresh_Sync,
        LSD_OT_Anim_Keyframe_Entire_Pose,
        LSD_OT_Anim_Layer_Add,
        LSD_OT_Anim_Layer_Remove,
        LSD_OT_Anim_Layer_Move,
        LSD_OT_Anim_Merge_Bake,
        LSD_OT_Anim_Duplicate_Layer,
        LSD_OT_Anim_Extract_Bones,
        LSD_OT_Anim_Extract_Keys,
        LSD_OT_Anim_Import_Selected_Keyframes,
        LSD_OT_Anim_Bake_Layers_To_Keyframes,
        LSD_OT_Anim_Select_Bones,
        LSD_OT_Anim_Reset_Key_Layer,
        LSD_OT_Anim_Multikey_Edit,
        LSD_OT_Anim_Cyclic_Fcurves,
        LSD_OT_Anim_Remove_Fcurves,
        LSD_OT_Anim_Custom_Frame_Range,
        LSD_OT_Anim_Sync_To_Action,
        LSD_OT_Quick_SDF_Boolean, LSD_OT_Toggle_Cutter_Visibility,
        LSD_OT_CreateCamera, LSD_OT_Camera_Setup, LSD_OT_Camera_Look_Through,
        LSD_OT_Generate_Collision_Mesh, LSD_OT_Purge_Collision, LSD_OT_Asset_Clear, LSD_OT_Asset_Edit_External,
        LSD_OT_LightTarget, LSD_OT_ApplyToonShader, LSD_OT_GlobalToonSharpness, LSD_OT_ToonifySelectedLights,
        LSD_OT_Execute_AI_Prompt, LSD_OT_SetJointType, LSD_OT_CalculateCenterOfMass,
        LSD_OT_CalculateInertia, LSD_OT_Calculate_All_Physics, LSD_OT_BakeMesh, LSD_OT_ReadJointSettings, LSD_OT_ApplyJointSettings,
        LSD_OT_SetupIK, LSD_OT_SetOriginToCursor, LSD_OT_Apply_Paint_Bucket,
        LSD_OT_Paint_Layer_Add, LSD_OT_Paint_Layer_Remove, LSD_OT_Paint_Layer_Move, LSD_OT_Paint_Layer_Select,
        LSD_UL_SlinkyHooks_List, LSD_OT_Paint_SetupBrush,
        LSD_OT_ExportGazeboWorld, LSD_OT_LinkChainDriver, LSD_OT_AddBoolean, LSD_OT_AddParametricAnchor,
        LSD_OT_AddMarker, LSD_OT_ToggleHookPlacement, LSD_OT_CleanupAnchor, LSD_OT_BakeAnchor,
        LSD_OT_AddTextDescription, LSD_OT_Remove_Dimension, LSD_OT_Add_Dimension, LSD_OT_Manually_Draw_Offset_Line, LSD_OT_AddModifier,
        LSD_OT_Dimension_AutoScale, LSD_OT_Register_Default_Proportions, LSD_OT_Dimension_Auto_Calculate_Global,
        LSD_OT_AddSimplify, LSD_OT_SetupLinearArray, LSD_OT_SetupRadialArray, LSD_OT_CreateCurveForPath,
        LSD_OT_SetupCurveArray, LSD_OT_SmartSmooth, LSD_OT_AddMimic,
        LSD_OT_RemoveMimic, LSD_OT_ClearConfig, LSD_OT_ApplyBoneConstraints, LSD_OT_PickBone,
        LSD_OT_GenerateROS2Workspace, LSD_OT_ExportList_Add,
        LSD_OT_ExportList_Remove, LSD_OT_Export, LSD_OT_ExportSelected, LSD_OT_TogglePlacement,
        LSD_OT_CreateRig, LSD_OT_MergeArmatures, LSD_OT_PurgeBones, LSD_OT_ParentToActive,
        LSD_OT_EnterPoseMode, LSD_OT_EnterObjectMode, LSD_OT_AddBone, LSD_OT_ApplyRestPose,
        LSD_OT_AccurateScale, LSD_OT_CommitPathAlignment,
        LSD_OT_SelectObjectByName, LSD_OT_AddToDimensionMaster, LSD_OT_RemoveFromDimensionMaster, LSD_OT_BakeDimensionsMaster,
        LSD_OT_ImportGroupedDimensionsBack, LSD_OT_ClearGroupedDimensions, LSD_OT_AlignAllSelectedDimensions,
        LSD_OT_NavigateToDimensionGroups,
        LSD_OT_Apply_Smart_Skin_Thickness, LSD_OT_Apply_Smart_Skin_Transition,
        LSD_OT_Init_Smart_Skin_Data, LSD_OT_Setup_Skin_Modifier,
        LSD_OT_Apply_SDF_Booleans, LSD_OT_Directional_Translate,
        LSD_OT_SnapToKeyframe, LSD_OT_Global_Math_Input
    ]
    for cls in CLASSES:
        if hasattr(cls, 'bl_rna'):
            try:
                bpy.utils.register_class(cls)
            except Exception as e:
                print(f"LSD Operator Register Warning: Could not register {cls.__name__}: {e}")

def unregister():
    CLASSES = [
        LSD_OT_SetStartupFiles, LSD_OT_SelectStartupBlend, LSD_OT_SelectUserprefBlend,
        LSD_OT_NM_Pick_Cutter_From_Selection,
        LSD_OT_NM_Quick_Boolean,
        LSD_OT_NM_Boolean_Pro,
        LSD_OT_NM_Surface_Project,
        LSD_OT_NM_Surface_Insert,
        LSD_OT_NM_Normal_Weighted,
        LSD_OT_NM_Smooth_Normals,
        LSD_OT_NM_Normal_Transfer,
        LSD_OT_NM_Repair_Boolean_Normals,
        LSD_OT_NM_Repair_Bevel_Normals,
        LSD_OT_NM_Boolean_Extrude,
        LSD_OT_NM_Cut_Groove,
        LSD_OT_NM_Boolean_Trim,
        LSD_OT_NM_View_Normals,
        LSD_OT_NM_View_Sharp,
        LSD_OT_NM_Mark_Sharp,
        LSD_OT_NM_Apply_Modifiers,

        LSD_OT_Sync_Active_Layer,
        LSD_OT_Anim_Refresh_Sync,
        LSD_OT_Anim_Keyframe_Entire_Pose,
        LSD_OT_Anim_Layer_Add,
        LSD_OT_Anim_Layer_Remove,
        LSD_OT_Anim_Layer_Move,
        LSD_OT_Anim_Merge_Bake,
        LSD_OT_Anim_Duplicate_Layer,
        LSD_OT_Anim_Extract_Bones,
        LSD_OT_Anim_Extract_Keys,
        LSD_OT_Anim_Import_Selected_Keyframes,
        LSD_OT_Anim_Bake_Layers_To_Keyframes,
        LSD_OT_Anim_Select_Bones,
        LSD_OT_Anim_Reset_Key_Layer,
        LSD_OT_Anim_Multikey_Edit,
        LSD_OT_Anim_Cyclic_Fcurves,
        LSD_OT_Anim_Remove_Fcurves,
        LSD_OT_Anim_Custom_Frame_Range,
        LSD_OT_Anim_Sync_To_Action,
        LSD_OT_Quick_SDF_Boolean, LSD_OT_Toggle_Cutter_Visibility,
        LSD_OT_CreateCamera, LSD_OT_Camera_Setup, LSD_OT_Camera_Look_Through,
        LSD_OT_Generate_Collision_Mesh, LSD_OT_Purge_Collision, LSD_OT_Asset_Clear, LSD_OT_Asset_Edit_External,
        LSD_OT_LightTarget, LSD_OT_ApplyToonShader, LSD_OT_GlobalToonSharpness, LSD_OT_ToonifySelectedLights,
        LSD_OT_Execute_AI_Prompt, LSD_OT_SetJointType, LSD_OT_CalculateCenterOfMass,
        LSD_OT_CalculateInertia, LSD_OT_Calculate_All_Physics, LSD_OT_BakeMesh, LSD_OT_ReadJointSettings, LSD_OT_ApplyJointSettings,
        LSD_OT_SetupIK, LSD_OT_SetOriginToCursor, LSD_OT_Apply_Paint_Bucket,
        LSD_OT_Paint_Layer_Add, LSD_OT_Paint_Layer_Remove, LSD_OT_Paint_Layer_Move, LSD_OT_Paint_Layer_Select,
        LSD_UL_SlinkyHooks_List, LSD_OT_Paint_SetupBrush,
        LSD_OT_ExportGazeboWorld, LSD_OT_LinkChainDriver, LSD_OT_AddBoolean, LSD_OT_AddParametricAnchor,
        LSD_OT_AddMarker, LSD_OT_ToggleHookPlacement, LSD_OT_CleanupAnchor, LSD_OT_BakeAnchor,
        LSD_OT_AddTextDescription, LSD_OT_Remove_Dimension, LSD_OT_Add_Dimension, LSD_OT_Manually_Draw_Offset_Line, LSD_OT_AddModifier,
        LSD_OT_Dimension_AutoScale, LSD_OT_Register_Default_Proportions, LSD_OT_Dimension_Auto_Calculate_Global,
        LSD_OT_AddSimplify, LSD_OT_SetupLinearArray, LSD_OT_SetupRadialArray, LSD_OT_CreateCurveForPath,
        LSD_OT_SetupCurveArray, LSD_OT_SmartSmooth, LSD_OT_AddMimic,
        LSD_OT_RemoveMimic, LSD_OT_ClearConfig, LSD_OT_ApplyBoneConstraints, LSD_OT_PickBone,
        LSD_OT_GenerateROS2Workspace, LSD_OT_ExportList_Add,
        LSD_OT_ExportList_Remove, LSD_OT_Export, LSD_OT_ExportSelected, LSD_OT_TogglePlacement,
        LSD_OT_CreateRig, LSD_OT_MergeArmatures, LSD_OT_PurgeBones, LSD_OT_ParentToActive,
        LSD_OT_EnterPoseMode, LSD_OT_EnterObjectMode, LSD_OT_AddBone, LSD_OT_ApplyRestPose,
        LSD_OT_AccurateScale, LSD_OT_CommitPathAlignment,
        LSD_OT_SelectObjectByName, LSD_OT_AddToDimensionMaster, LSD_OT_RemoveFromDimensionMaster, LSD_OT_BakeDimensionsMaster,
        LSD_OT_ImportGroupedDimensionsBack, LSD_OT_ClearGroupedDimensions, LSD_OT_AlignAllSelectedDimensions,
        LSD_OT_NavigateToDimensionGroups,
        LSD_OT_Apply_Smart_Skin_Thickness, LSD_OT_Apply_Smart_Skin_Transition,
        LSD_OT_Init_Smart_Skin_Data, LSD_OT_Setup_Skin_Modifier,
        LSD_OT_Apply_SDF_Booleans, LSD_OT_Directional_Translate,
        LSD_OT_SnapToKeyframe, LSD_OT_Global_Math_Input
    ]
    for cls in reversed(CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass
