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
from typing import List, Tuple, Optional, Set, Any, Dict, Union
from . import config
from .config import *

# ------------------------------------------------------------------------

#   Guard Variables (LSD Internal State)

# ------------------------------------------------------------------------

_prop_update_guard = False

_joint_editor_update_guard = False

_last_active_bone_key = None

_update_gizmo_guard = False

# Per-Item Synchronization Guards (Recursion Prevention)
# AI Editor Note: Transitioned from boolean to set to support batch-editing in grouped lists.
_dim_sync_active_ids = set()
_dim_timer_queued_ids = set()
_dim_pending_batch_sync_ids = set() # Batch update queue for grouped edits

# AI Editor Note: Per-Item guarding (Set of IDs) to prevent recursion on a per-object basis.
# This is critical for chaining support (Dimension 1 moving Dimension 2's Root).
# A global boolean would block Dim 2 from syncing while Dim 1 is updating.
_dim_sync_active_ids = set()

_curve_update_guard = False

_path_align_update_guard = False

def update_panel_collapse(self, context):
    """Callback for all panel visibility properties to support Auto-Collapse"""
    if not context or not context.scene: return
    if not context.scene.lsd_auto_collapse_panels: return
    # Identify which property changed. self is the Scene.
    # We find which one is True and collapse all others.
    # Note: Property name is not passed as arg, so we check which one became True
    # in the context of the current Redraw.
    from .config import LSD_PANEL_PROPS
    # We find which panel was just opened (it will be True)
    # BUT wait, this update runs AFTER the value is set.
    # If multiple are True, we keep only the 'most recent' True? No, we check which one is True.
    # To be safe, we just use the logic from the operator if called via operator.
    # If called via property toggle, we still need to know which one.
    pass
#   PART 1: LOGIC, HELPERS & HANDLERS

# ------------------------------------------------------------------------

class LSD_OT_Core_DisablePanel(bpy.types.Operator):
    """Disables (hides) a panel from the UI. Re-enable it in Preferences > Visible Panels."""
    bl_idname = "lsd.disable_panel"
    bl_label = "Close Panel"
    bl_options = {'INTERNAL'}
    prop_name: bpy.props.StringProperty()
    def execute(self, context: bpy.types.Context) -> Set[str]:
        if hasattr(context.scene, self.prop_name):
            setattr(context.scene, self.prop_name, False)
            if self.prop_name == "lsd_panel_enabled_animation":
                settings = getattr(context.scene, 'lsd_anim_settings', None)
                if settings:
                    settings.layers_enabled = False
                    settings.onion_skin_enabled = False
                    settings.library_enabled = False
        return {'FINISHED'}
class LSD_OT_Core_SnapCursorToActive(bpy.types.Operator):
    """Snap 3D cursor to the active object's origin"""
    bl_idname = "lsd.snap_cursor_to_active"
    bl_label = "Snap Cursor to Active"
    bl_options = {'REGISTER', 'UNDO'}
    @classmethod
    def poll(cls, context):
        return context.active_object is not None
    def execute(self, context: bpy.types.Context) -> Set[str]:
        # Gather all target locations from selected objects and bones
        targets = []
        # 1. Handle Selected Objects
        if context.selected_objects:
            for obj in context.selected_objects:
                # For armatures, we might want the bones instead if in Pose/Edit mode
                # but if we are in Object mode, we use the object's bound box.
                if context.mode == 'OBJECT' or obj.type != 'ARMATURE':
                    for v in obj.bound_box:
                        targets.append(obj.matrix_world @ mathutils.Vector(v))
        # 2. Handle Selected Bones (Pose Mode)
        if context.mode == 'POSE' and context.selected_pose_bones:
            for pb in context.selected_pose_bones:
                # Add head and tail of each selected bone
                targets.append(pb.id_data.matrix_world @ pb.head)
                targets.append(pb.id_data.matrix_world @ pb.tail)
        # 3. Handle Selected Bones (Edit Mode)
        if context.mode == 'EDIT' and context.active_object and context.active_object.type == 'ARMATURE':
            for eb in context.selected_editable_bones:
                targets.append(context.active_object.matrix_world @ eb.head)
                targets.append(context.active_object.matrix_world @ eb.tail)
        if not targets:
            self.report({'WARNING'}, "No objects or bones selected.")
            return {'CANCELLED'}
        # Calculate the average center of all gathered points
        min_x = min(p.x for p in targets)
        max_x = max(p.x for p in targets)
        min_y = min(p.y for p in targets)
        max_y = max(p.y for p in targets)
        min_z = min(p.z for p in targets)
        max_z = max(p.z for p in targets)
        center = mathutils.Vector(((max_x + min_x) / 2, (max_y + min_y) / 2, (max_z + min_z) / 2))
        # Set cursor location
        context.scene.cursor.location = center
        self.report({'INFO'}, f"Snapped cursor to center of {len(targets)//2 if context.mode in {'POSE', 'EDIT'} else len(context.selected_objects)} item(s).")
        return {'FINISHED'}
def update_scene_lighting(self, context: bpy.types.Context):
    """
    Core logic to update scene environment and lighting based on properties.
    AI Editor Note: This function is triggered by property updates to provide
    immediate visual feedback.
    """
    props = context.scene.lsd_pg_lighting_props
    world = context.scene.world
    if not world:
        world = bpy.data.worlds.new("LSD_World")
        context.scene.world = world
    # --- 1. World Background ---
    # Sync World nodes or base color
    if world.use_nodes:
        bg = world.node_tree.nodes.get("Background")
        if bg:
            # For FLAT preset, use base color for World as well
            if props.light_preset == 'FLAT':
                bg.inputs[0].default_value = props.base_color
                bg.inputs[1].default_value = 1.0 # High strength for unlit feel
            else:
                bg.inputs[0].default_value = props.background_color
                bg.inputs[1].default_value = 1.0
    else:
        if props.light_preset == 'FLAT':
            world.color = props.base_color[:3]
        else:
            world.color = props.background_color[:3]
    # --- 2. Lighting Rig Management ---
    # Prefix for identifying addon-managed lights
    prefix = "LSD_ENV_"
    # Cleanup old lights
    for obj in list(bpy.data.objects):
        if obj.name.startswith(prefix) and obj.type == 'LIGHT':
            bpy.data.objects.remove(obj, do_unlink=True)
    preset = props.light_preset
    intensity = props.light_intensity
    tint = props.base_color[:3]
    def create_managed_light(name, type, location=(0,0,0), rotation=(0,0,0), energy=100.0, size=1.0):
        data = bpy.data.lights.new(name=f"{prefix}{name}", type=type)
        obj = bpy.data.objects.new(name=f"{prefix}{name}", object_data=data)
        context.collection.objects.link(obj)
        obj.location = location
        obj.rotation_euler = rotation
        data.energy = energy * intensity
        data.color = tint
        data.use_shadow = props.use_shadows
        if type == 'AREA':
            data.size = size
        return obj
    if preset == 'OUTDOOR':
        # Single Sun light for crisp outdoor illumination
        sun = create_managed_light("Sun", 'SUN', location=(0,0,10), rotation=(0.7, 0.4, 0), energy=5.0)
    elif preset == 'STUDIO':
        # 3-Point Studio Setup
        # Key Light
        create_managed_light("Key", 'AREA', location=(6, -6, 8), rotation=(0.7, 0, 0.78), energy=4000.0, size=3.0)
        # Fill Light (Warmer/Weaker)
        create_managed_light("Fill", 'AREA', location=(-6, -3, 5), rotation=(0.5, 0, -0.5), energy=1500.0, size=5.0)
        # Rim Light (Highlighting edge)
        create_managed_light("Rim", 'AREA', location=(0, 8, 6), rotation=(2.3, 0, 3.14), energy=3000.0, size=2.0)
    elif preset == 'EMPTY':
        # Only uses world ambient color. No direct lights.
        pass
    # --- 3. Viewport Shading Adjustments ---
    for area in context.screen.areas:
        if area.type == 'VIEW_3D':
            for space in area.spaces:
                if space.type == 'VIEW_3D' and hasattr(space, 'shading'):
                    shading = space.shading
                    if preset == 'FLAT':
                        shading.type = 'SOLID'
                        shading.light = 'FLAT'
                        shading.color_type = 'OBJECT'
                    elif shading.light == 'FLAT':
                        shading.light = 'STUDIO'
def update_scene_lighting(self, context):
    """
    Applies global scene-wide lighting adjustments (Tint, Background).
    """
    scene = context.scene
    props = scene.lsd_pg_lighting_props
    # 1. Update World Background Color
    if not scene.world:
        scene.world = bpy.data.worlds.new("LSD_World")
    # Check if using nodes for world
    if scene.world.use_nodes:
        sh_node = next((n for n in scene.world.node_tree.nodes if n.type == 'BACKGROUND'), None)
        if sh_node:
            sh_node.inputs[0].default_value = props.background_color[:4]
    else:
        scene.world.color = props.background_color[:3]
    # 2. Individual Light Tuning (Multiplier)
    # Note: We avoid heavy looping here to maintain UI performance
def update_selected_light(self, context: bpy.types.Context):
    """
    Syncs the active light object's data with the 'Smart Editing' properties.
    AI Editor Note: This provides a way to 'pick up' and modify any light in the scene.
    """
    obj = context.active_object
    if not obj or obj.type != 'LIGHT':
        return
    props = context.scene.lsd_pg_lighting_props
    light = obj.data
    # 1. Type Change
    if light.type != props.selected_light_type:
        light.type = props.selected_light_type
    is_flat = props.selected_light_shading == 'FLAT'
    # 2. Power & Color
    light.energy = props.selected_light_energy
    light.color = props.selected_light_color[:3]
    # 3. Shading Behavior
    if is_flat:
        if hasattr(light, 'shadow_soft_size'): light.shadow_soft_size = 0.0
        if hasattr(light, 'radius'): light.radius = 0.0
        if hasattr(light, 'specular_factor'): light.specular_factor = 0.0
        # Hard-edge footprint for Spot lights
        if hasattr(light, 'spot_blend'): light.spot_blend = 0.0
        # High Bias for digital noise removal (prevents blobby floor artifacts)
        if hasattr(light, 'shadow_buffer_bias'): light.shadow_buffer_bias = 0.1
        if hasattr(light, 'use_contact_shadow'):
            light.use_contact_shadow = True
            light.contact_shadow_distance = 0.02
    else:
        # Realistic Gradient
        if hasattr(light, 'shadow_soft_size'): light.shadow_soft_size = 0.1
        if hasattr(light, 'radius'): light.radius = 0.2
        if hasattr(light, 'specular_factor'): light.specular_factor = 1.0
        if hasattr(light, 'spot_blend'): light.spot_blend = 0.15
        if hasattr(light, 'use_custom_distance'): light.use_custom_distance = False
        if hasattr(light, 'shadow_buffer_bias'): light.shadow_buffer_bias = 0.05
        if hasattr(light, 'use_contact_shadow'): light.use_contact_shadow = False
        # Return to softer look if disabled
        if hasattr(light, 'shadow_soft_size'): light.shadow_soft_size = 0.1
        if hasattr(light, 'radius'): light.radius = 0.2
        if hasattr(light, 'angle'): light.angle = 0.1
        if hasattr(light, 'specular_factor'): light.specular_factor = 1.0
        if hasattr(light, 'use_contact_shadow'): light.use_contact_shadow = False
@persistent

def sync_light_props_handler(scene, depsgraph=None):
    """
    Synchronizes the UI properties with the currently selected light.
    AI Editor Note: This is triggered on every depsgraph update (selection change).
    We use a check to avoid infinite loops when the properties themselves update.
    """
    ctx = bpy.context
    obj = ctx.active_object
    if not obj or obj.type != 'LIGHT':
        return
    props = scene.lsd_pg_lighting_props
    light = obj.data
    # Avoid updating if we are currently in the middle of a manual property change
    # (Checking for active window or specific UI flag is hard, so we just check for value difference)
    if props.selected_light_type != light.type:
        props.selected_light_type = light.type
    # Energy/Color sync disabled to prevent automatic 'normalization' on selection.
    # The UI now binds directly to light.energy/light.color for real-time control.
    """
    if not math.isclose(props.selected_light_energy, light.energy, rel_tol=1e-5):
        props.selected_light_energy = light.energy
    # Sync Color
    for i in range(3):
        if not math.isclose(props.selected_light_color[i], light.color[i], rel_tol=1e-4):
            props.selected_light_color = (light.color[0], light.color[1], light.color[2], 1.0)
            break
    """
    # Sync Target (Eyedropper) - ONLY if constrained. Avoid clearing user pick.
    found_target = None
    for c in obj.constraints:
        if c.type == 'DAMPED_TRACK' and c.target:
            found_target = c.target
            break
    if found_target and props.selected_light_target != found_target:
        props.selected_light_target = found_target
    # Sync Shading Style (Flat vs Gradient)
    current_shading = 'GRADIENT'
    # Any light type can be 'FLAT' if shadows are zero and specular is zero
    if hasattr(light, 'shadow_soft_size') and light.shadow_soft_size < 1e-4:
        if hasattr(light, 'specular_factor') and light.specular_factor < 1e-4:
            current_shading = 'FLAT'
    elif hasattr(light, 'radius') and light.radius < 1e-4:
        if hasattr(light, 'specular_factor') and light.specular_factor < 1e-4:
            current_shading = 'FLAT'
    if props.selected_light_shading != current_shading:
        props.selected_light_shading = current_shading
def ensure_default_rig(context: bpy.types.Context) -> Optional[bpy.types.Object]:
    """
    Ensures that a valid armature is set as the active rig in the scene.
    This function is a cornerstone of the addon's stability. Many operators and UI
    elements depend on having an active rig to work with. This function guarantees
    that `context.scene.lsd_active_rig` always points to a valid armature.
    The logic is as follows:
    1. If an active rig is already set and exists in the scene, do nothing.
    2. If not, search the scene for any existing armature and set the first one
       found as the active rig.
    3. If no armatures exist in the scene, create a new one with a default name
       ("New_Kinematics") and set it as the active rig.
    Args:
        context: The current Blender context.
    Returns:
        The active rig object, or None if one could not be found or created.
    """
    # 1. Check if the currently set rig is valid and in the view layer.
    if context.scene.lsd_active_rig and context.scene.lsd_active_rig.name in context.view_layer.objects:
        return context.scene.lsd_active_rig
    # 2. Search for any existing armature in the view layer.
    for obj in context.view_layer.objects:
        if obj.type == 'ARMATURE':
            context.scene.lsd_active_rig = obj
            return obj
    # 3. If no armatures exist, create a new one.
    try:
        base_name = "New_Kinematics"
        name = get_unique_name(base_name)
        arm_data = bpy.data.armatures.new(name + "_Data")
        rig = bpy.data.objects.new(name, arm_data)
        rig.location = context.scene.cursor.location # AI Editor Note: Ensure implicit rig is created at cursor
        context.scene.collection.objects.link(rig)
        # Set display properties for better visibility.
        rig.display_type = 'WIRE'
        rig.show_in_front = True
        context.scene.lsd_active_rig = rig
        return rig
    except Exception as e:
        print(f"Error creating default rig: {e}")
        return None
@persistent

def auto_set_active_rig_handler(dummy: Any) -> None:
    """
    A persistent handler that runs automatically after a .blend file is loaded.
    This handler ensures that the addon is immediately ready to use upon file load
    by calling `ensure_default_rig`. This prevents errors or empty UI panels that
    could otherwise occur if no active rig is set when the user opens a file
    containing a robot model.
    The `@persistent` decorator ensures this handler remains active across multiple
    file loads within a single Blender session.
    Args:
        dummy: The scene object passed by the handler (unused).
    """
    # This handler can run in contexts where bpy.context is not fully formed.
    if bpy.context and bpy.context.scene:
        ensure_default_rig(bpy.context)
@persistent

def load_panel_order_handler(dummy: Any) -> None:
    """
    Applies the saved panel order from scene properties after loading a file.
    """
    # Use a timer to ensure context is ready
    bpy.app.timers.register(lambda: (bpy.ops.lsd.update_panel_order() and None), first_interval=0.2)
@persistent

def get_font_data(font_name: str, is_bold: bool = False, is_italic: bool = False) -> Optional[bpy.types.VectorFont]:
    """
    Discovery and loading logic for standard technical fonts.
    Returns a loaded VectorFont object or None.
    """
    if font_name == 'DEFAULT':
        # Default Blender font (BFont) doesn't easily expose its .ttf for loading into other slots.
        # If user wants Bold/Italic, we MUST use a real file. Fallback to Arial.
        if is_bold or is_italic: font_name = 'ARIAL'
        else: return None
    import platform, os
    system = platform.system()
    font_map = {
        'ARIAL':        ('arial.ttf', 'arialbd.ttf', 'ariali.ttf', 'arialbi.ttf'),
        'ROBOTO':       ('Roboto-Regular.ttf', 'Roboto-Bold.ttf', 'Roboto-Italic.ttf', 'Roboto-BoldItalic.ttf'),
        'DIN':          ('din.ttf', 'dinbd.ttf', 'dinai.ttf', 'dinbt.ttf', 'DIN.ttf'),
        'CENTURY':      ('centgoth.ttf', 'GOTHICB.TTF', 'GOTHICI.TTF', 'GOTHICZ.TTF'),
        'FUTURA':       ('futura.ttf', 'futurab.ttf', 'futurai.ttf'),
        'HELVETICA':    ('helvetica.ttf', 'Helvetica-Bold.ttf', 'Helvetica-Oblique.ttf', 'arial.ttf'),
        'GOTHAM':       ('Gotham-Medium.ttf', 'Gotham-Bold.ttf', 'Gotham-BookItalic.ttf'),
        'AVENIR':       ('Avenir.ttf', 'Avenir-Bold.ttf', 'Avenir-Italic.ttf'),
        'CONSOLAS':     ('consola.ttf', 'consolab.ttf', 'consolai.ttf', 'consolaz.ttf'),
        'SIMPLEX':      ('simplex.ttf', 'Simplex.ttf', 'SIMPLEX.TTF'),
        'ARCHITXT':     ('architxt.ttf', 'Architxt.ttf', 'archit.ttf'),
        'ROMANS':       ('romans.ttf', 'Romans.ttf', 'ROMANS.TTF'),
        'CITY':         ('cityb__.ttf', 'cityblueprint.ttf', 'CityBlueprint.ttf'),
        'ISO':          ('isocpeur.ttf', 'isocp.ttf', 'isoc____.ttf'),
        'STYLUS':       ('Stylus BT.ttf', 'StylusBT.ttf', 'Stylus.ttf'),
        'ARCH_DAUGHTER': ('ArchitectsDaughter.ttf', 'architects_daughter.ttf'),
        'POPPINS':      ('Poppins-Regular.ttf', 'Poppins-Bold.ttf', 'Poppins-Italic.ttf'),
        'TAHOMA':       ('tahoma.ttf', 'tahomabd.ttf'),
        'VERDANA':      ('verdana.ttf', 'verdanab.ttf', 'verdanai.ttf', 'verdanaz.ttf'),
        'SEGOE':        ('segoeui.ttf', 'seguib.ttf', 'seguii.ttf', 'seguiz.ttf'),
        'TREBUCHET':    ('trebuc.ttf', 'trebucbd.ttf', 'trebucit.ttf', 'trebucbi.ttf'),
        'QUICKSAND':    ('Quicksand-Regular.ttf', 'Quicksand-Bold.ttf', 'Quicksand-Light.ttf'),
        'BAUHAUS':      ('bauhaus.ttf', 'BAUHS93.TTF', 'Bauhaus.ttf'),
        'SPACE':        ('SpaceGrotesk-Regular.ttf', 'SpaceGrotesk-Bold.ttf'),
        'MONTSERRAT':   ('Montserrat-Regular.ttf', 'Montserrat-Bold.ttf', 'Montserrat-Italic.ttf'),
    }
    search_paths = []
    if system == 'Windows':
        search_paths = [
            os.path.join(os.environ.get('WINDIR', 'C:\\Windows'), 'Fonts'),
            os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Microsoft\\Windows\\Fonts')
        ]
    elif system == 'Darwin': search_paths = ['/Library/Fonts', '/System/Library/Fonts']
    else: search_paths = ['/usr/share/fonts', '/usr/local/share/fonts']
    f_files = font_map.get(font_name, (f"{font_name.lower()}.ttf", f"{font_name.lower()}.otf"))
    if is_bold or is_italic:
        styled_files = []
        patterns = []
        if is_bold and is_italic: patterns = ['bi', 'bolditalic', 'bold_italic', 'bold-italic', 'italiz', 'blackitalic', 'z']
        elif is_bold: patterns = ['bd', 'bold', 'black', 'heavy', 'bold_', '_b.', '-b.', ' b.']
        elif is_italic: patterns = ['it', 'ital', 'italic', 'oblique', 'ai', '_i.', '-i.']
        for f in f_files:
            f_base = os.path.splitext(f.lower())[0]
            if any(pat in f_base for pat in patterns):
                styled_files.append(f)
        if styled_files: f_files = tuple(styled_files) + f_files
    font_path = None
    for s_path in search_paths:
        if not os.path.exists(s_path): continue
        for f_file in f_files:
            test_p = os.path.join(s_path, f_file)
            if os.path.exists(test_p):
                font_path = test_p
                break
        if font_path: break
        # DEEP SEARCH FALLBACK: If standard map fails, scan the directory for partial name matches
        if not font_path:
            try:
                base_name_to_search = font_name.lower().replace("_", " ")
                files_in_dir = os.listdir(s_path)
                for f_item in files_in_dir:
                    f_item_low = f_item.lower()
                    if base_name_to_search in f_item_low:
                         # If we need bold/italic, check if this file matches the patterns
                         if is_bold or is_italic:
                              if any(pat in f_item_low for pat in patterns):
                                   font_path = os.path.join(s_path, f_item)
                                   break
                         else:
                              # Prefer the shortest file (likely the regular variant)
                              if len(f_item_low) < len(base_name_to_search) + 5:
                                   font_path = os.path.join(s_path, f_item)
                                   break
            except: pass
        if font_path: break
    if not font_path:
        # Fallback to Arial if standard fails (Windows specific as per metadata)
        arial_p = "C:\\Windows\\Fonts\\arial.ttf"
        if os.path.exists(arial_p): font_path = arial_p
    if font_path:
        try:
            f_name_key = os.path.basename(font_path)
            f_data = bpy.data.fonts.get(f_name_key)
            if not f_data: f_data = bpy.data.fonts.load(font_path)
            return f_data
        except: return None
    return None
@persistent

def toggle_placement_parenting(scene, context):
    """
    Toggles parenting relationship for placement mode.
    On: Unparents meshes from bones (keeping world transform).
    Off: Reparents meshes back to their original bones.
    """
    rig = scene.lsd_active_rig
    if not rig: return
    is_active = scene.lsd_placement_mode
    # AI Editor Note: Using a robust collection search to handle all parts.
    if is_active:
        # 1. Store and Detach
        for obj in bpy.data.objects:
            if obj.parent == rig and obj.parent_type == 'BONE' and obj.parent_bone:
                obj["lsd_temp_bone"] = obj.parent_bone
                # Store world matrix to preserve it exactly
                old_matrix = obj.matrix_world.copy()
                obj.parent = None
                obj.matrix_world = old_matrix
    else:
        # 2. Restore and Reattach
        for obj in bpy.data.objects:
            if "lsd_temp_bone" in obj:
                bone_name = obj["lsd_temp_bone"]
                if bone_name in rig.pose.bones:
                    old_matrix = obj.matrix_world.copy()
                    obj.parent = rig
                    obj.parent_type = 'BONE'
                    obj.parent_bone = bone_name
                    obj.matrix_world = old_matrix
                del obj["lsd_temp_bone"]
    # Refresh constraints to reflect placement state (unlock/lock)
    for bone in rig.pose.bones:
        apply_native_constraints(bone)
@persistent

def lsd_placement_handler(scene, depsgraph=None):
    """
    Persistent handler to ensure placement mode state consistency across view layers.
    Note: Heavy parenting logic is offloaded to property updates to maintain FPS.
    """
    pass
def ensure_material_mapping_nodes(mat: bpy.types.Material) -> None:
    """
    Ensures that the material has a 'Mapping' node and it is properly linked
    to the 'Base Color' of the Principled BSDF.
    Used for the 'Always Available' transform controls.
    """
    if not mat.use_nodes:
        mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    # 1. Ensure BSDF exists
    bsdf = next((n for n in nodes if n.type == 'BSDF_PRINCIPLED'), None)
    if not bsdf:
        mat.node_tree.nodes.clear() # Reset corrupted material
        bsdf = nodes.new('BSDF_PRINCIPLED')
        output = nodes.new('ShaderNodeOutputMaterial')
        links.new(bsdf.outputs['BSDF'], output.inputs['Surface'])
    # 2. Find or Create Mapping
    mapping = next((n for n in nodes if n.type == 'MAPPING'), None)
    if not mapping:
        mapping = nodes.new('ShaderNodeMapping')
        mapping.location = (bsdf.location.x - 400, bsdf.location.y)
    # 3. Find or Create TexCoord
    tex_coord = next((n for n in nodes if n.type == 'TEX_COORD'), None)
    if not tex_coord:
        tex_coord = nodes.new('ShaderNodeTexCoord')
        tex_coord.location = (mapping.location.x - 200, mapping.location.y)
    # Link them up
    if not any(l for l in mapping.inputs['Vector'].links):
        links.new(tex_coord.outputs['UV'], mapping.inputs['Vector'])
    # 4. Find anything currently plugged into BSDF Base Color (e.g. an image)
    # and ensure the mapping node is feeding into its 'Vector' input.
    base_color_input = bsdf.inputs['Base Color']
    if base_color_input.links:
        source_node = base_color_input.links[0].from_node
        if hasattr(source_node, "inputs") and "Vector" in source_node.inputs:
            if not any(l for l in source_node.inputs['Vector'].links if l.from_node == mapping):
                links.new(mapping.outputs['Vector'], source_node.inputs['Vector'])
def update_global_bones(self: bpy.types.Scene, context: bpy.types.Context) -> None:
    """
    Update callback for the global "Show Bones" toggle in the UI.
    This function is triggered when the `scene.lsd_show_bones` property is changed.
    It iterates through all 3D Viewport spaces in the current screen and sets their
    `overlay.show_bones` property to match the new value. This ensures that the
    visibility of bones is consistent across all viewports.
    Args:
        self: The scene object.
        context: The current Blender context.
    """
    if not context.screen:
        return
    for area in context.screen.areas:
        if area.type == 'VIEW_3D':
            for space in area.spaces:
                if space.type == 'VIEW_3D':
                    space.overlay.show_bones = self.lsd_show_bones
def get_asset_libraries(self, context):
    items = [('LOCAL', "Current File", "Assets in the current file")]
    libs = getattr(context.preferences.filepaths, "asset_libraries", [])
    for lib in libs:
        if lib.name:
            items.append((lib.name, lib.name, lib.path))
    return items
def get_or_create_arrow_mesh():
    """Returns a reusable conical arrowhead mesh."""
    mesh_name = "LSD_Arrow_Mesh"
    if mesh_name in bpy.data.meshes:
        return bpy.data.meshes[mesh_name]
    mesh = bpy.data.meshes.new(mesh_name)
    # Simple cone geometry (Z-up)
    verts = [
        (0, 0, 0),        # Tip (0)
        (0.05, 0, -0.15), # Base Circle
        (0.035, 0.035, -0.15),
        (0, 0.05, -0.15),
        (-0.035, 0.035, -0.15),
        (-0.05, 0, -0.15),
        (-0.035, -0.035, -0.15),
        (0, -0.05, -0.15),
        (0.035, -0.035, -0.15),
    ]
    # Faces: the tip-to-base triangles and the bottom circle
    faces = [(0, 1, 2), (0, 2, 3), (0, 3, 4), (0, 4, 5), (0, 5, 6), (0, 6, 7), (0, 7, 8), (0, 8, 1),
             (1, 8, 7, 6, 5, 4, 3, 2)]
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    return mesh
def update_category_enum(self: bpy.types.Scene, context: bpy.types.Context) -> None:
    """
    Update callback for the parametric part category dropdown.
    This function is triggered when the user changes the main part category (e.g.,
    from "Gears" to "Fasteners"). It improves usability by automatically setting a
    sensible default for the sub-type dropdown. For example, if the user selects
    "Gears", the sub-type will default to "Spur".
    Args:
        self: The scene object.
        context: The current Blender context.
    """
    cat = context.scene.lsd_part_category
    if cat == 'GEAR':
        context.scene.lsd_part_type = 'BEVEL'
    elif cat == 'RACK':
        context.scene.lsd_part_type = 'RACK_BEVEL'
    elif cat == 'FASTENER':
        context.scene.lsd_part_type = 'BOLT'
    elif cat == 'SPRING':
        context.scene.lsd_part_type = 'DAMPER'
    elif cat == 'CHAIN':
        context.scene.lsd_part_type = 'BELT'
    elif cat == 'WHEEL':
        context.scene.lsd_part_type = 'WHEEL_CASTER'
    elif cat == 'PULLEY':
        context.scene.lsd_part_type = 'PULLEY_UGROOVE'
    elif cat == 'ROPE':
        context.scene.lsd_part_type = 'ROPE_TUBE'
    elif cat == 'BASIC_JOINT':
        context.scene.lsd_part_type = 'JOINT_CONTINUOUS'
    elif cat == 'ARCHITECTURAL':
        context.scene.lsd_part_type = 'WALL'
    elif cat == 'BASIC_SHAPE':
        context.scene.lsd_part_type = 'SHAPE_CIRCLE'
def update_electronics_category_enum(self: bpy.types.Scene, context: bpy.types.Context) -> None:
    """
    Update callback for the electronics category dropdown.
    Resets the sub-type to a default for the new category.
    """
    cat = context.scene.lsd_electronics_category
    if cat == 'MOTOR':
        context.scene.lsd_electronics_type = 'MOTOR_BLDC_OUTRUNNER'
    elif cat == 'SENSOR':
        context.scene.lsd_electronics_type = 'SENSOR_CONTACT'
    elif cat == 'PCB':
        context.scene.lsd_electronics_type = 'PCB_ARDUINO'
    elif cat == 'IC':
        context.scene.lsd_electronics_type = 'IC_CAPACITOR'
    elif cat == 'CAMERA':
        context.scene.lsd_electronics_type = 'CAMERA_DEFAULT'
def get_gizmo_rotation_matrix(joint_type: str, axis_alignment: str) -> mathutils.Matrix:
    """Calculates the rotation matrix needed to align a default gizmo shape."""
    rot_matrix = mathutils.Matrix.Identity(4)
    target_axis = axis_alignment.replace("-", "")
    if joint_type == 'prismatic':
        if target_axis == 'X':
            rot_matrix = mathutils.Matrix.Rotation(math.radians(-90.0), 4, 'Z')
        elif target_axis == 'Z':
            rot_matrix = mathutils.Matrix.Rotation(math.radians(90.0), 4, 'X')
    else: # revolute/continuous
        if target_axis == 'X':
            rot_matrix = mathutils.Matrix.Rotation(math.radians(90.0), 4, 'Y')
        elif target_axis == 'Y':
            rot_matrix = mathutils.Matrix.Rotation(math.radians(-90.0), 4, 'X')
    return rot_matrix.to_3x3()
def get_or_create_text_material(target_obj):
    """Ensures a unique text material exists for the given object and returns it."""
    if not hasattr(target_obj, "name"):
         return bpy.data.materials.new("ERROR_MAT")
    mat_name = f"LSD_Material_{target_obj.name}"
    mat = bpy.data.materials.get(mat_name)
    if not mat:
        mat = bpy.data.materials.new(name=mat_name)
        mat.use_nodes = True
    host = get_dimension_host(target_obj)
    if host:
        dim_props = getattr(host, "lsd_pg_dim_props", None)
    else:
        dim_props = getattr(target_obj, "lsd_pg_dim_props", None)
    # 1.1.3 Global Color Sync Logic
    scene = bpy.context.scene
    if scene.lsd_dim_global_text_color_sync:
         color = list(scene.lsd_dim_universal_text_color)
    else:
         color = list(dim_props.text_color) if dim_props else [0.0, 0.0, 0.0, 1.0]
    if mat.use_nodes and mat.node_tree:
        bsdf = mat.node_tree.nodes.get("Principled BSDF")
        if bsdf:
            current_color = list(bsdf.inputs['Base Color'].default_value)
            if any(abs(a - b) > 0.001 for a, b in zip(current_color, color)):
                # AI Editor Note: High-Legibility Draftsman Display
                bsdf.inputs['Base Color'].default_value = color
                bsdf.inputs['Metallic'].default_value = 0.0
                bsdf.inputs['Roughness'].default_value = 1.0
                # Specular handling (Blender 4.0+ uses Specular IOR or weight)
                if 'Specular' in bsdf.inputs:
                    bsdf.inputs['Specular'].default_value = 0.0
                if 'Specular IOR Level' in bsdf.inputs:
                    bsdf.inputs['Specular IOR Level'].default_value = 0.0
                # Ensure visibility in Solid mode via Object Color sync.
                # We set a small emission to keep it crisp in Rendered mode without it being a light source.
                bsdf.inputs['Emission Color'].default_value = color
                bsdf.inputs['Emission Strength'].default_value = 0.01
    # Sync diffuse color for Solid viewport display
    if any(abs(a - b) > 0.001 for a, b in zip(list(mat.diffuse_color), color)):
        mat.diffuse_color = color
    return mat

def get_or_create_line_material(target_obj):
    """Ensures a unique line material exists for the given object and returns it."""
    if not hasattr(target_obj, "name"):
         return bpy.data.materials.new("ERROR_MAT")
    mat_name = f"LSD_Line_Material_{target_obj.name}"
    mat = bpy.data.materials.get(mat_name)
    if not mat:
        mat = bpy.data.materials.new(name=mat_name)
        mat.use_nodes = True
        
    host = get_dimension_host(target_obj)
    if host:
        dim_props = getattr(host, "lsd_pg_dim_props", None)
    else:
        dim_props = getattr(target_obj, "lsd_pg_dim_props", None)
    scene = bpy.context.scene
    
    if scene.lsd_dim_global_text_color_sync:
         color = list(scene.lsd_dim_universal_line_color)
    else:
         color = list(dim_props.line_color) if dim_props else [0.0, 0.0, 0.0, 1.0]
         
    if mat.use_nodes and mat.node_tree:
        bsdf = mat.node_tree.nodes.get("Principled BSDF")
        if bsdf:
            current_color = list(bsdf.inputs['Base Color'].default_value)
            if any(abs(a - b) > 0.001 for a, b in zip(current_color, color)):
                bsdf.inputs['Base Color'].default_value = color
                bsdf.inputs['Metallic'].default_value = 0.0
                bsdf.inputs['Roughness'].default_value = 1.0
                if 'Specular' in bsdf.inputs:
                    bsdf.inputs['Specular'].default_value = 0.0
                if 'Specular IOR Level' in bsdf.inputs:
                    bsdf.inputs['Specular IOR Level'].default_value = 0.0
                bsdf.inputs['Emission Color'].default_value = color
                bsdf.inputs['Emission Strength'].default_value = 0.01
            
    if any(abs(a - b) > 0.001 for a, b in zip(list(mat.diffuse_color), color)):
        mat.diffuse_color = color
    return mat

# Guard flag: True while any depsgraph handler is executing.
# Used to prevent material writes inside the depsgraph cycle.
_in_depsgraph_handler = False

def sync_dimension_assembly_material(host_obj):
    """
    Ensures that the correctly synced material is assigned to all members
    of the dimension assembly (arrows, lines, labels).
    """
    global _in_depsgraph_handler
    if not host_obj or not host_obj.get("lsd_is_dimension"):
        return
    # If called from within the depsgraph handler, defer via timer to avoid cycle
    if _in_depsgraph_handler:
        _queue_material_sync(host_obj.name)
        return
    root = host_obj.parent
    if not root:
        root = get_dimension_root(host_obj)
    if not root: return
    # Get/Create the materials for this specific host (dimension)
    text_mat = get_or_create_text_material(host_obj)
    line_mat = get_or_create_line_material(host_obj)
    
    # Assign to all relevant children
    for child in root.children:
        # 1. Label
        if child.get("lsd_is_dimension"):
            if child.data:
                if not child.data.materials:
                    child.data.materials.append(text_mat)
                elif child.data.materials[0] != text_mat:
                    child.data.materials[0] = text_mat
            if child.active_material != text_mat:
                child.active_material = text_mat
            if any(abs(a - b) > 0.001 for a, b in zip(list(child.color), list(text_mat.diffuse_color))):
                child.color = text_mat.diffuse_color
            
        # 2. Arrows (Linked Objects - Shared Mesh)
        elif child.get("lsd_is_dimension_anchor") == "VISUAL":
            # Force link to OBJECT for independent coloration of shared meshes
            if child.material_slots:
                if child.material_slots[0].link != 'OBJECT':
                    child.material_slots[0].link = 'OBJECT'
                if child.material_slots[0].material != line_mat:
                    child.material_slots[0].material = line_mat
            elif child.active_material != line_mat:
                child.active_material = line_mat
                
            if any(abs(a - b) > 0.001 for a, b in zip(list(child.color), list(line_mat.diffuse_color))):
                child.color = line_mat.diffuse_color
            
        # 3. Lines & Extensions
        elif child.get("lsd_is_dimension_line") or child.get("lsd_is_extension_line"):
            if child.active_material != line_mat:
                child.active_material = line_mat
            if any(abs(a - b) > 0.001 for a, b in zip(list(child.color), list(line_mat.diffuse_color))):
                child.color = line_mat.diffuse_color
    # root.update_tag()
    # host_obj.update_tag()
def get_dimension_host(obj: Optional[bpy.types.Object]) -> Optional[bpy.types.Object]:
    """
    Robustly identifies the host of the dimension properties (the Label object)
    from any component of a dimension assembly (Root, Anchors, Line, or Label).
    """
    if not obj: return None
    if obj.get("lsd_is_dimension"): return obj
    root = get_dimension_root(obj)
    if root:
        for child in root.children:
            if child.get("lsd_is_dimension"):
                return child
    return None

def propagate_dimension_follower_updates(master_obj, scene, length):
    """
    Recursively pushes length updates from a Master dimension to all registered Followers
    across both the Active Tracker and all Grouped Sets.
    """
    if not master_obj: return
    
    # 1. Active Tracker List
    for item in scene.lsd_dimensions_master:
        _apply_propagation_to_item(item, master_obj, length, scene)
        
    # 2. Archival Groups (Scene Tab)
    for g_set in getattr(scene, "lsd_dimensions_grouped_sets", []):
         for item in g_set.items:
              _apply_propagation_to_item(item, master_obj, length, scene)

def _apply_propagation_to_item(item, master_obj, length, scene):
    """Helper to process a single dimension item for Master-Follower propagation."""
    # Resolve the item's target to its actual host (Label) for comparison
    it_target = get_dimension_host(item.driver_target)
    if it_target == master_obj:
        follower = item.obj
        if not follower or follower == master_obj: return
        if follower.name in _dim_sync_active_ids: return # Recursion Guard
        
        f_len = length * item.ratio
        f_props = getattr(follower, "lsd_pg_dim_props", None)
        if f_props:
            _dim_sync_active_ids.add(follower.name)
            try:
                # 1. Update ID Property
                follower.lsd_pg_dim_props.length = f_len
                
                # 2. Trigger Batch Sync for robust Main-Thread evaluation
                import properties
                if hasattr(properties, 'queue_batch_sync'):
                    properties.queue_batch_sync(follower.name)
                
                # 3. Forced Viewport Refresh (Matrix bypass)
                update_dimension_length(follower, length_override=f_len)
                follower["_lsd_last_built_length"] = f_len
                
                # 4. Chain updates (Masters can be Followers)
                propagate_dimension_follower_updates(follower, scene, f_len)
            finally:
                _dim_sync_active_ids.remove(follower.name)
def get_dimension_root(obj: Optional[bpy.types.Object]) -> Optional[bpy.types.Object]:
    """
    Identifies the Root Empty of a dimension assembly starting from any child.
    """
    if not obj: return None
    if obj.get("lsd_is_dimension_root"): return obj
    # Check parent
    p = obj.parent
    if p and p.get("lsd_is_dimension_root"): return p
    # Check pointer fallback
    p_ptr = obj.get("lsd_dim_root")
    if p_ptr and isinstance(p_ptr, bpy.types.Object): return p_ptr
    return None
    # 4. Check active object for direct back-pointer (Participants)
    # The generators now store the Root on the target objects.
    root_pointer = obj.get("lsd_dim_root")
    if root_pointer:
         for child in root_pointer.children:
              if child.get("lsd_is_dimension"):
                   return child
    # 5. Global Search (participating targets - Fallback)
    for o in bpy.context.scene.objects:
        if o.get("lsd_is_dimension_root"):
             p_obj = o.get("lsd_parent_obj")
             s_obj = o.get("lsd_slave_obj")
             if (p_obj and p_obj.name == obj.name) or (s_obj and s_obj.name == obj.name):
                  for child in o.children:
                       if child.get("lsd_is_dimension"):
                            return child
    return None


# Pending material sync queue - objects are added during depsgraph, flushed via timer
_pending_material_sync = set()
_material_sync_timer_running = False

def _flush_material_sync():
    """Timer callback: applies all queued material syncs safely outside the depsgraph."""
    global _pending_material_sync, _material_sync_timer_running
    _material_sync_timer_running = False
    targets = list(_pending_material_sync)
    _pending_material_sync.clear()
    for obj_name in targets:
        obj = bpy.data.objects.get(obj_name)
        if obj and obj.get("lsd_is_dimension"):
            sync_dimension_assembly_material(obj)
        elif obj and getattr(obj, "lsd_is_standalone_offset", False):
            line_mat = get_or_create_line_material(obj)
            if obj.active_material != line_mat:
                obj.active_material = line_mat
            if any(abs(a - b) > 0.001 for a, b in zip(list(obj.color), list(line_mat.diffuse_color))):
                obj.color = line_mat.diffuse_color
    return None  # Don't repeat

def _queue_material_sync(obj_name):
    """Adds an object to the pending sync queue and ensures the timer is running."""
    global _pending_material_sync, _material_sync_timer_running
    _pending_material_sync.add(obj_name)
    if not _material_sync_timer_running:
        _material_sync_timer_running = True
        bpy.app.timers.register(_flush_material_sync, first_interval=0.05)

@persistent
def lsd_dimension_sync_handler(scene: bpy.types.Scene, depsgraph: bpy.types.Depsgraph) -> None:
    global _in_depsgraph_handler
    """Ultimate real-time synchronization for Procedural Dimensions."""
    # We exit immediately if we are in Edit Mode to prevent lag during extrusion/translation.
    context = bpy.context
    if context and context.mode != 'OBJECT':
        return
    _in_depsgraph_handler = True
    try:
        # Only process objects that were actually updated in this depsgraph cycle.
        for update in depsgraph.updates:
            if not isinstance(update.id, bpy.types.Object): continue
            obj = update.id
            
            # Verify it's an LSD dimension object or a participant (Hook)
            if not obj.get("lsd_is_dimension"):
                root_ptr = obj.get("lsd_dim_root")
                if root_ptr:
                    obj = get_dimension_host(root_ptr)
                    if not obj: continue
                else:
                    continue
                
            eval_obj = depsgraph.id_eval_get(obj) if depsgraph else obj
            dim_props = getattr(eval_obj, "lsd_pg_dim_props", None)
            if not dim_props: continue
            root = obj.parent if not obj.get("lsd_is_dimension_root") else obj
            if not root: continue
            
            target_mesh_hook = root.get("lsd_hook_end")
            if not target_mesh_hook or not isinstance(target_mesh_hook, bpy.types.Object):
                for child in root.children:
                    if child.get("lsd_is_dimension_anchor") == "END":
                        for o in bpy.data.objects:
                            if o.get("lsd_is_dimension_hook") == "END" and o.get("lsd_dim_root") == root:
                                target_mesh_hook = o
                                root["lsd_hook_end"] = o
                                break
                        if target_mesh_hook: break
            
            local_target = None
            if target_mesh_hook:
                local_target = root.matrix_world.inverted() @ target_mesh_hook.matrix_world.translation
            
            # 1. LENGTH SYNC
            if obj.name in _dim_sync_active_ids: continue
            last_l = obj.get("_lsd_last_built_length", -1.0)
            id_l = obj.lsd_pg_dim_props.length
            
            if not dim_props.is_manual:
                if local_target is None: continue
                dist = abs(local_target.z)
                if abs(id_l - dist) > 0.0001:
                    _dim_sync_active_ids.add(obj.name)
                    try:
                        obj.lsd_pg_dim_props.length = dist
                        update_dimension_length(obj, length_override=dist)
                        obj["_lsd_last_built_length"] = dist
                        propagate_dimension_follower_updates(obj, scene, dist)
                        for child in root.children:
                            if child.get("lsd_is_dimension_anchor"): child.update_tag()
                    finally:
                        _dim_sync_active_ids.discard(obj.name)
            else:
                if abs(last_l - id_l) > 0.0001:
                    update_dimension_length(obj, length_override=id_l)
                    obj["_lsd_last_built_length"] = id_l
                    propagate_dimension_follower_updates(obj, scene, id_l)

        # 3. MASTER TRACKER FINAL PASS
        for item in scene.lsd_dimensions_master:
            _process_item_visual_sync(item)
        for g_set in getattr(scene, "lsd_dimensions_grouped_sets", []):
            for item in g_set.items:
                _process_item_visual_sync(item)
    finally:
        _in_depsgraph_handler = False



def _process_item_visual_sync(item):
    """Helper to ensure ID-property changes are visually refreshed in the viewport."""
    f = item.obj
    if not f or not hasattr(f, "lsd_pg_dim_props"): return
    
    id_l = f.lsd_pg_dim_props.length
    last_l = f.get("_lsd_last_built_length", -1.0)
    
    # If the property changed (via propagation or driver) but the visual hasn't been rebuilt
    if abs(id_l - last_l) > 0.0001:
         if f.name in _dim_sync_active_ids: return
         _dim_sync_active_ids.add(f.name)
         try:
              update_dimension_length(f, length_override=id_l)
              f["_lsd_last_built_length"] = id_l
              # Ensure hooks are also tagged for the next frame
              hook = f.get("lsd_hook_end")
              if hook: hook.update_tag()
         finally:
              _dim_sync_active_ids.remove(f.name)
def update_dimension_length(obj, length_override=None):
    """
    Manual/Sync refresh for dimension components.
    Handles both Root and Label object inputs.
    Source of Truth: The Label (Host) object.
    
    length_override: If provided (e.g. from eval depsgraph), use this instead of original ID data.
    """
    if not obj: return
    root = get_dimension_root(obj)
    host = get_dimension_host(obj)
    if not root or not host: return
    dim_props = getattr(host, "lsd_pg_dim_props", None)
    if not dim_props: return
    
    length = length_override if length_override is not None else dim_props.length
    
    # Pre-calculate assembly root matrix for efficient world-coordinate projection
    root_mat = root.matrix_world.copy()
    
    # 1. Coordinate Sync (Labels, End-Anchors, Legs)
    label_obj = None
    for child in root.children:
        if child.get("lsd_is_dimension"): label_obj = child
        is_end = child.get("lsd_is_dimension_anchor") == "END"
        is_ext_b = child.get("lsd_is_extension_line") and child.get("lsd_extension_type") == "END"
        
        if is_end or is_ext_b:
            child.location.z = length
            # FORCE INSTANT VIEWPORT UPDATE:
            # We must manually set the world matrix to bypass the 1-frame constraint/parenting lag 
            # in post_update handlers.
            child.matrix_world = root_mat @ mathutils.Matrix.Translation((0, 0, length))
            
            # Also force-update any measure-hooks attached to this anchor
            if is_end:
                 hook = root.get("lsd_hook_end")
                 if hook and isinstance(hook, bpy.types.Object):
                      hook.matrix_world = child.matrix_world.copy()
        elif child.get("lsd_is_dimension_line"):
            # The midsection is updated in update_arrow_settings for alignment reasons
            pass
            
    # 2. Update Label String & Units
    if label_obj:
        unit_str = "m" if dim_props.unit_display == 'METERS' else "mm"
        val = length if unit_str == "m" else length * 1000.0
        if hasattr(label_obj.data, "body"):
             label_text = f"{val:.2f} {unit_str}"
             if label_obj.data.body != label_text:
                  label_obj.data.body = label_text
        
        # Position label at midpoint
        label_obj.location.z = length / 2
        label_obj.matrix_world = root_mat @ mathutils.Matrix.Translation((0, 0, length / 2))
        
    # 3. Global settings passthrough and final sync
    update_arrow_settings(root)
    # AI Editor Note: Added explicit tags to fix the 'offset until undo' bug
    host.update_tag()
    root.update_tag()
def update_arrow_settings(obj):
    """
    Updates visual settings (scale, color, direction) for the assembly.
    Handles both Label and Root object inputs.
    Source of Truth: The Label (Host) object.
    """
    if not obj: return
    root = get_dimension_root(obj)
    host = get_dimension_host(obj)
    if not root or not host: return
    dim_props = getattr(host, "lsd_pg_dim_props", None)
    if not dim_props: return
    direction_map = {
        'X': mathutils.Vector((1, 0, 0)),
        'Y': mathutils.Vector((0, 1, 0)),
        'Z': mathutils.Vector((0, 0, 1)),
        '-X': mathutils.Vector((-1, 0, 0)),
        '-Y': mathutils.Vector((0, -1, 0)),
        '-Z': mathutils.Vector((0, 0, -1)),
    }
    # 1. Parameter Sync
    # We resolve the scene's unit scale to keep visual components (line, arrows, text)
    # legible regardless of the measurement system (mm vs m).
    unit_scale = bpy.context.scene.unit_settings.scale_length
    us = 1.0 / unit_scale if unit_scale > 0 else 1.0
    length = dim_props.length
    arrow_s = dim_props.arrow_scale * us
    text_s = dim_props.text_scale * us
    offset = dim_props.offset * us
    line_t = dim_props.line_thickness * us
    dir_enum = dim_props.direction
    # Calculate drafting parallelogram offset vectors
    mat_inv = root.matrix_world.to_3x3().inverted_safe()
    # Resolve the world offset vector based on alignment flags
    # AI Editor Note: User Request - Allow combining axis alignments for diagonal offsets.
    offset_world_vec = mathutils.Vector((0, 0, 0))
    if dim_props.align_x: offset_world_vec.x += 1
    if dim_props.align_nx: offset_world_vec.x -= 1
    if dim_props.align_y: offset_world_vec.y += 1
    if dim_props.align_ny: offset_world_vec.y -= 1
    if dim_props.align_z: offset_world_vec.z += 1
    if dim_props.align_nz: offset_world_vec.z -= 1
    if offset_world_vec.length < 0.001:
         # Default: assembly local Y axis
         offset_world_vec = root.matrix_world.to_3x3() @ mathutils.Vector((0, 1, 0))
    else:
         offset_world_vec = offset_world_vec.normalized()
    offset_local_dir = (mat_inv @ offset_world_vec)
    offset_local_dir = (mat_inv @ offset_world_vec).normalized()
    # AI Editor Note: Logic Update (Parallelogram Drafting).
    # Per user feedback, we no longer enforce Zero-Z. If the user aligns with
    # a world-axis that is not perpendicular to the line, we allow the
    # assembly to "slant" or "slide" as long as it aligns with the set axis.
    # To prevent "pushing" or "overlap" artifacts, we must ensure END anchors
    # add the Z-offset to the length properly.
    # Compensation for Parent Scale:
    # If the root is parented to a scaled object, we must divide our children's
    # scale and location by the root's world scale to maintain absolute drafting units.
    rw_scale = root.matrix_world.to_scale()
    def safe_divide(val, s): return val / s if abs(s) > 0.0001 else val
    # Apply location scale compensation to move_vec
    if getattr(dim_props, "use_offset", False):
        move_vec = mathutils.Vector((
            safe_divide(offset_local_dir.x * offset, rw_scale.x),
            safe_divide(offset_local_dir.y * offset, rw_scale.y),
            safe_divide(offset_local_dir.z * offset, rw_scale.z)
        ))
    else:
        move_vec = mathutils.Vector((0, 0, 0))
        
    # Extension Leg Rotation: points from the dimension line back to the target points.
    # This vector is (-move_vec) in the assembly's local drafting space.
    ext_rot_vec = (-move_vec)
    ext_leg_length = ext_rot_vec.length
    if ext_leg_length > 0.00001:
        ext_rot_euler = (ext_rot_vec.normalized()).to_track_quat('Z', 'Y').to_euler()
    else:
        ext_rot_euler = mathutils.Euler((0, 0, 0))
    # Compensation for Parent Scale:
    # If the root is parented to a scaled object, we must divide our children's
    # scale by the root's world scale to maintain absolute draftsman units.
    rw_scale = root.matrix_world.to_scale()
    def safe_divide(val, s): return val / s if abs(s) > 0.0001 else val
    for child in root.children:
        # 1. PHYSICAL MASTER ANCHORS & HOOKS: Fixed scale
        tag = child.get("lsd_is_dimension_anchor")
        if tag in ["MASTER", "HOOK"]:
             if child.get("lsd_anchor_type") == "END" or tag == "HOOK":
                  child.location = (dim_props.target_x, dim_props.target_y, length)
             else:
                  child.location = (0, 0, 0)
             s_val = 0.05 if tag == "MASTER" else 0.4
             child.scale = (safe_divide(s_val, rw_scale.x), safe_divide(s_val, rw_scale.y), safe_divide(s_val, rw_scale.z))
             continue
        # 2. VISUAL COMPONENTS: These slide along the drafting offset
        if child.get("lsd_is_dimension_anchor") == "VISUAL":
             child.scale = (safe_divide(arrow_s, rw_scale.x), safe_divide(arrow_s, rw_scale.y), safe_divide(arrow_s, rw_scale.z))
             child.location = move_vec.copy()
             if child.get("lsd_anchor_type") == "END":
                  child.location.z += length
        elif child.get("lsd_is_dimension_line"): # The Main Line
            child.location = move_vec.copy()
            child.scale = (safe_divide(line_t, rw_scale.x), safe_divide(line_t, rw_scale.y), safe_divide(length, rw_scale.z))
            child.rotation_euler = (0, 0, 0)
        elif child.get("lsd_is_extension_line"):
            child.hide_viewport = not dim_props.use_extension_lines
            child.hide_render = not dim_props.use_extension_lines
            if not dim_props.use_extension_lines: continue
            child.scale.x = safe_divide(line_t * 0.9, rw_scale.x)
            child.scale.y = safe_divide(line_t * 0.9, rw_scale.y)
            child.location = move_vec.copy()
            if child.get("lsd_extension_type") == "END":
                 child.location.z += length
            child.rotation_euler = ext_rot_euler
            child.scale.z = safe_divide(ext_leg_length, rw_scale.z)
            if child.scale.z < 0.001: child.scale.z = 0.001
        elif child.get("lsd_is_dimension"): # The Label
            # OPTIC COMPENSATION: Bold fonts often feel 'smaller' or compressed.
            # Apply a 1.15x scale factor when Bold is active to ensure visual parity.
            ts_mod = 1.15 if dim_props.font_bold else 1.0
            child.scale = (
                safe_divide(text_s * ts_mod, rw_scale.x),
                safe_divide(text_s * ts_mod, rw_scale.y),
                safe_divide(text_s * ts_mod, rw_scale.z)
            )
            # Clearance from drafting line
            if move_vec.length > 0.0001:
                text_dir = move_vec.normalized()
            else:
                text_dir = mathutils.Vector((
                    safe_divide(offset_local_dir.x, rw_scale.x),
                    safe_divide(offset_local_dir.y, rw_scale.y),
                    safe_divide(offset_local_dir.z, rw_scale.z)
                )).normalized()
            text_clearance = text_dir * (dim_props.text_offset * us)
            # Reusable Font Assignment
            f_data = get_font_data(dim_props.font_name, dim_props.font_bold, dim_props.font_italic)
            if f_data:
                 child.data.font = f_data
                 f_path_low = f_data.filepath.lower()
                 # Functional Italic fix
                 v_shear = 0.2 if dim_props.font_italic and not any(p in f_path_low for p in ['it', 'ital', 'oblique', 'z']) else 0.0
                 if hasattr(child.data, "shear"): child.data.shear = v_shear
                 for mod in child.modifiers:
                     if mod.type == 'NODES' and mod.node_group:
                         for inp in mod.node_group.inputs:
                             if inp.name == "Shear":
                                 mod[inp.identifier] = v_shear
                                 break
                 child.data.offset = 0.0
                 if hasattr(child.data, "update_tag"): child.data.update_tag()
            # (Deleted old inline font block)
            # AI Editor Note: Flip Text Mirroring
            # Stay at original location but 'face the other way'.
            # Solution: Rotate 180 on Y (Mirror Reflector) to maintain upright baseline.
            flip_rot = mathutils.Euler((0, 0, 0))
            if dim_props.flip_text:
                # Rotate 180 around Local Y (Horizontal Reflection)
                # This mirrors the text direction while keeping it upright.
                flip_rot = mathutils.Euler((0, math.pi, 0))
            child.location = move_vec + text_clearance
            
            if dim_props.text_alignment == 'LEFT':
                child.location.z += 0.0
                align_val = 'RIGHT' if dim_props.flip_text else 'LEFT'
            elif dim_props.text_alignment == 'RIGHT':
                child.location.z += length
                align_val = 'LEFT' if dim_props.flip_text else 'RIGHT'
            else:
                child.location.z += length / 2
                align_val = 'CENTER'
                
            if hasattr(child.data, "align_x"):
                 child.data.align_x = align_val
            # Sync font to GN if present
            mod = child.modifiers.get("Dynamic_Dimension")
            if mod and mod.node_group:
                 font_id = None
                 text_size_id = None
                 if hasattr(mod.node_group, "interface"):
                      for item in mod.node_group.interface.items_tree:
                           if item.name == "Font":
                                font_id = item.identifier
                           elif item.name == "Text Size":
                                text_size_id = item.identifier
                 if font_id: mod[font_id] = f_data
                 if text_size_id: mod[text_size_id] = dim_props.text_scale
                 # Force GN Re-evaluation
                 child.update_tag()
            # Text Orientation System (Parallel Alignment).
            vec_x = mathutils.Vector((0, 0, 1)) # Assembly direction
            vec_y = offset_local_dir.normalized()
            vec_z = vec_x.cross(vec_y).normalized()
            # Construct a pure orthonormal orientation matrix (World-to-Text-Basis)
            m = mathutils.Matrix((vec_x, vec_y, vec_z)).transposed()
            base_rot = m.to_euler()
            # Apply user-defined Euler rotation + flip correctly
            user_euler = mathutils.Euler(dim_props.text_rotation, 'XYZ')
            combined_rot = (base_rot.to_matrix() @ flip_rot.to_matrix() @ user_euler.to_matrix()).to_euler()
            child.rotation_euler = combined_rot
            # Sync material & visibility
            mat = child.active_material
            if mat:
                 child.color = mat.diffuse_color
                 child.show_in_front = True
    root.update_tag()
    # AI Editor Note: Sync Flip Trigger.
    # Must call the atomic role-swap logic here to handle 'is_flipped' changes.
    sync_dimension_flipping(root)
def _bake_and_release_hook(hook_empty: bpy.types.Object) -> None:
    """Inline bake utility for the flip protocol.
    Applies the current deformation of the mesh as its new rest pose, re-creates
    the Hook modifier so parametric control is maintained, and removes the
    COPY_LOCATION constraint from the hook Empty so it no longer follows any anchor.
    This 'releases' the hook from the dimension's constraint chain without moving anything.
    """
    # 1. Find all meshes driven by this hook Empty
    meshes_to_bake = []
    for scene_obj in bpy.data.objects:
        if scene_obj.type != 'MESH': continue
        for mod in scene_obj.modifiers:
            if mod.type == 'HOOK' and mod.object == hook_empty:
                meshes_to_bake.append((scene_obj, mod.name, {
                    'vertex_group': mod.vertex_group,
                    'strength':     mod.strength,
                    'falloff_type': mod.falloff_type,
                    'falloff_radius': mod.falloff_radius,
                    'uniform':      mod.use_falloff_uniform,
                }))
    # 2. Bake each linked mesh's Hook modifier (apply → rebind at new rest state)
    prev_active = bpy.context.view_layer.objects.active
    for mesh_obj, mod_name, mod_data in meshes_to_bake:
        try:
            bpy.context.view_layer.objects.active = mesh_obj
            bpy.ops.object.modifier_apply(modifier=mod_name)
            # Re-create the hook modifier at the new rest pose
            new_mod = mesh_obj.modifiers.new(name=mod_name, type='HOOK')
            new_mod.object = hook_empty
            if mod_data['vertex_group']:
                new_mod.vertex_group = mod_data['vertex_group']
            new_mod.strength      = mod_data['strength']
            new_mod.falloff_type  = mod_data['falloff_type']
            new_mod.falloff_radius = mod_data['falloff_radius']
            new_mod.use_falloff_uniform = mod_data['uniform']
            # Reset modifier to bind at current position (new rest pose)
            try:
                bpy.ops.object.mode_set(mode='EDIT')
                bpy.ops.mesh.select_all(action='SELECT')
                bpy.ops.object.hook_reset(modifier=new_mod.name)
                bpy.ops.object.mode_set(mode='OBJECT')
            except: pass
        except Exception as e:
            print(f"[LSD] Flip bake failed on {mesh_obj.name}: {e}")
    if prev_active:
        bpy.context.view_layer.objects.active = prev_active
    # 3. Apply visual transform on the Empty itself (bake constraint result → location)
    try:
        world_mat = hook_empty.matrix_world.copy()
        # Remove ALL COPY_LOCATION constraints that target dimension anchors
        cons_to_remove = [c for c in hook_empty.constraints
                          if c.type == 'COPY_LOCATION' and c.target
                          and c.target.get("lsd_is_dimension_anchor")]
        for con in cons_to_remove:
            hook_empty.constraints.remove(con)
        # Apply the baked world position as the hook Empty's own location
        hook_empty.matrix_world = world_mat
    except Exception as e:
        print(f"[LSD] Flip: hook release failed on {hook_empty.name}: {e}")
def sync_dimension_flipping(obj):
    """
    Swaps which selected point is p1 (master/start) vs p2 (slave/end).
    AI Editor Note: This is now guarded per-item and handles chaining (root constraints).
    """
    root = get_dimension_root(obj)
    if not root: return
    host = get_dimension_host(obj)
    if not host: return
    host_props = host.lsd_pg_dim_props
    # Guard against recursion while role swapping
    if root.name in _dim_sync_active_ids: return
    # CHANGE-DETECTION GUARD: Only fire when is_flipped actually changes
    last_flipped = root.get("_lsd_last_flipped_state", None)
    current_flipped = host_props.is_flipped
    if last_flipped is None:
        root["_lsd_last_flipped_state"] = int(current_flipped)
        return
    if bool(last_flipped) == bool(current_flipped):
        return
    root["_lsd_last_flipped_state"] = int(current_flipped)
    _dim_sync_active_ids.add(root.name)
    try:
        # Re-resolve world matrices for the anchors before swapping
        bpy.context.view_layer.update()
        # 1. Capture current participants and their anchors
        start_anchor = next((c for c in root.children if c.get("lsd_anchor_type") == "START"), None)
        end_anchor = next((c for c in root.children if c.get("lsd_anchor_type") == "END"), None)
        obj_a = root.get("lsd_parent_obj") # Participant at Start (old P1)
        obj_b = root.get("lsd_slave_obj")  # Participant at End (old P2)
        if not all([start_anchor, end_anchor, obj_a, obj_b]):
             return
        # Capture world positions of actual anchors before we move the root
        old_p1_world = start_anchor.matrix_world.translation.copy()
        old_p2_world = end_anchor.matrix_world.translation.copy()
        # 2. Release participants (Bake + Remove constraints)
        for hook in [obj_a, obj_b]:
             if isinstance(hook, bpy.types.Object):
                  _bake_and_release_hook(hook)
        # 3. SWAP ROOT POSITION AND ORIENTATION
        # We move the Root to old_p2 (where the End was).
        # We then 180-flip the Y axis to keep the assembly on the same side.
        old_rot = root.matrix_world.to_quaternion()
        flip_quat = mathutils.Quaternion((0.0, 1.0, 0.0), math.pi) # 180 flip around local Y
        rot_quat = old_rot @ flip_quat
        rot_mat = rot_quat.to_matrix().to_4x4()
        rot_mat.translation = old_p2_world
        root.matrix_world = rot_mat
        # Recalculate distance (should be same but more robust)
        new_direction = old_p1_world - old_p2_world
        new_length = new_direction.length
        # Move End visual anchor to the new Z-length
        end_anchor.location = (0.0, 0.0, new_length)
        # 4. SWAP ROOT CONSTRAINTS (Chaining support)
        # If the root itself was following P1 and tracking P2, swap them.
        root_loc_con = next((c for c in root.constraints if c.type == 'COPY_LOCATION'), None)
        root_track_con = next((c for c in root.constraints if c.type == 'TRACK_TO'), None)
        if root_loc_con: root_loc_con.target = obj_b
        if root_track_con: root_track_con.target = obj_a
        # 5. RE-BIND PARTICIPANTS
        # Hook B (old P2) is now the START point.
        # Only apply reverse constraint if the Root is NOT following it.
        if not root_loc_con or root_loc_con.target != obj_b:
             con_b = obj_b.constraints.new('COPY_LOCATION')
             con_b.target = start_anchor
             con_b.use_offset = False
        obj_b["lsd_is_dimension_hook"] = "START"
        # Hook A (old P1) is now the END point.
        if not root_track_con or root_track_con.target != obj_a:
             con_a = obj_a.constraints.new('COPY_LOCATION')
             con_a.target = end_anchor
             con_a.use_offset = False
        obj_a["lsd_is_dimension_hook"] = "END"
        # Update labels for logic
        root["lsd_parent_obj"] = obj_b
        root["lsd_slave_obj"] = obj_a
        # Final property refresh
        host_props.length = new_length
        root.lsd_pg_dim_props.length = new_length
        update_dimension_length(root)
        root.update_tag()
    except Exception as e:
        print(f"[LSD] Flip Role Swap Error: {e}")
    finally:
        _dim_sync_active_ids.remove(root.name)
@staticmethod

def apply_path_vertex_alignment(context):
    """
    Physically aligns SELECTED path vertices to their combined bounding box.
    Enables Ortho-Snapping behavior for path drafting.
    """
    global _path_align_update_guard
    if _path_align_update_guard: return
    _path_align_update_guard = True
    try:
        scene = context.scene
        t_pos = scene.lsd_path_align_pos
        t_neg = scene.lsd_path_align_neg
        
        # Check if any alignment axis is actually enabled
        if not any(t_pos) and not any(t_neg):
            _path_align_update_guard = False
            return

        selected_paths = [obj for obj in context.selected_objects if obj.type in {'CURVE', 'MESH'}]
        if not selected_paths:
            _path_align_update_guard = False
            return

        # 1. Collect World Coordinates of SELECTED points only
        all_pts_w = []
        point_data = [] # List of (object, point_ref, current_world_pos)
        
        is_edit = context.mode == 'EDIT_MESH' or context.mode == 'EDIT_CURVE'
        
        for obj in selected_paths:
            mw = obj.matrix_world
            if obj.type == 'CURVE':
                # Curve handling (Edit Mode vs Object Mode)
                if obj.mode == 'EDIT':
                    import bmesh
                    # For curves in Edit mode, we still use data.splines but points have .select
                    for sp in obj.data.splines:
                        if sp.type == 'BEZIER':
                            for bp in sp.bezier_points:
                                if bp.select_control_point:
                                    co_w = mw @ bp.co
                                    all_pts_w.append(co_w)
                                    point_data.append((obj, bp, co_w))
                        else:
                            for p in sp.points:
                                if p.select:
                                    co_w = mw @ p.co.to_3d()
                                    all_pts_w.append(co_w)
                                    point_data.append((obj, p, co_w))
                else:
                    # Object mode: align ALL points if the object is selected? 
                    # No, usually we only want this in Edit Mode. Skip if not in Edit.
                    continue
            else: # MESH
                if obj.mode == 'EDIT':
                    bm = bmesh.from_edit_mesh(obj.data)
                    for v in bm.verts:
                        if v.select:
                            co_w = mw @ v.co
                            all_pts_w.append(co_w)
                            point_data.append((obj, v, co_w))
                else:
                    # Object mode: Skip mesh alignment to prevent accidental whole-mesh collapse
                    continue

        if not all_pts_w:
            _path_align_update_guard = False
            return

        # 2. Calculate Bounding Box of selected points
        mn = mathutils.Vector((min(p.x for p in all_pts_w), min(p.y for p in all_pts_w), min(p.z for p in all_pts_w)))
        mx = mathutils.Vector((max(p.x for p in all_pts_w), max(p.y for p in all_pts_w), max(p.z for p in all_pts_w)))

        # 3. Apply Snapping
        updated_meshes = set()
        for obj, pt, co_w in point_data:
            im = obj.matrix_world.inverted()
            new_w = co_w.copy()
            if t_pos[0]: new_w.x = mx.x
            if t_neg[0]: new_w.x = mn.x
            if t_pos[1]: new_w.y = mx.y
            if t_neg[1]: new_w.y = mn.y
            if t_pos[2]: new_w.z = mx.z
            if t_neg[2]: new_w.z = mn.z
            
            # Write back
            if hasattr(pt, "co"):
                if isinstance(pt.co, mathutils.Vector) and len(pt.co) == 4: # NURBS W
                    pt.co = (im @ new_w).to_4d(); pt.co.w = 1.0
                else:
                    pt.co = im @ new_w
            
            if obj.type == 'MESH':
                updated_meshes.add(obj)

        # 4. Refresh Viewports
        for m_obj in updated_meshes:
            bmesh.update_edit_mesh(m_obj.data)
        
    except Exception as e:
        print(f"[LSD] Path alignment error: {e}")
    finally:
        _path_align_update_guard = False
def create_flat_gizmo(shape_type: str = 'ROTATION', target_axis: str = 'Z', style: str = 'DEFAULT') -> Optional[bpy.types.Object]:
    """
    Creates or retrieves a custom bone shape (widget) object for visualizing joints.
    This function implements a flyweight pattern for gizmo objects. It creates a
    single, shared mesh and object for each type and axis of gizmo (e.g., one for
    'ROTATION' on 'X', one for 'SLIDER' on 'Y', etc.). This is highly efficient as
    it avoids duplicating geometry, saving memory in complex scenes.
    The gizmo objects are placed in a dedicated "Widgets" collection and are hidden
    from the viewport, rendering, and selection, as they are only templates to be
    referenced by the `custom_shape` property of a bone.
    Args:
        shape_type: The type of gizmo to create ('ROTATION', 'SLIDER', 'FIXED').
        target_axis: The axis the gizmo should be aligned to ('X', 'Y', 'Z').
        style: The visual style of the gizmo ('DEFAULT', '3D').
    Returns:
        The gizmo object, or None if creation fails (e.g., during rendering).
    """
    # Do not attempt to create meshes while a render job is running.
    if bpy.app.is_job_running("RENDER"):
        return None
    try:
        # AI Editor Note: Direct data access is safer in background threads/timers than bpy.context.
        shape_name = f"{WIDGET_PREFIX}_{style}_{shape_type}" if shape_type == 'BASE' else f"{WIDGET_PREFIX}_{style}_{shape_type}_{target_axis}"
        # If the gizmo object already exists and has mesh data, return it immediately.
        obj = bpy.data.objects.get(shape_name)
        if obj and obj.data:
            return obj
        # Create the mesh and object if they don't exist.
        mesh = bpy.data.meshes.get(shape_name)
        if not mesh:
            mesh = bpy.data.meshes.new(shape_name)
        if not obj:
            obj = bpy.data.objects.new(shape_name, mesh)
            obj.location = (0, 0, 0)
            # AI Editor Note: Gizmo templates must be hidden from viewports,
            # renders, and selection. They are only placeholders for 'custom_shape'.
            # Leaving them visible can cause them to overlap with real rigs at (0,0,0).
            obj.hide_viewport = True
            obj.hide_render = True
            obj.hide_select = True
            obj.display_type = 'WIRE'
        # Robust collection management.
        coll = bpy.data.collections.get(WIDGETS_COLLECTION_NAME)
        if not coll:
            coll = bpy.data.collections.new(WIDGETS_COLLECTION_NAME)
            # Link to the main scene collection if we have a context, otherwise it stays in data.
            # widgets do not need to be in the scene to be custom_shapes.
            if bpy.context and bpy.context.scene:
                try:
                    if coll.name not in bpy.context.scene.collection.children:
                        bpy.context.scene.collection.children.link(coll)
                except: pass
        if obj.name not in coll.objects:
            coll.objects.link(obj)
        # Generate the gizmo's geometry using BMesh.
        bm = bmesh.new()
        if True: # DEFAULT (Unified Style)
            if shape_type == 'ROTATION':
                bmesh.ops.create_circle(bm, cap_ends=False, radius=1.0, segments=32)
                # Add small arrows to indicate rotational direction.
                v1 = bm.verts.new((0.9, 0.2, 0))
                v2 = bm.verts.new((1.1, 0.2, 0))
                v3 = bm.verts.new((1.0, -0.1, 0))
                bm.faces.new((v1, v2, v3))
                v4 = bm.verts.new((-0.9, -0.2, 0))
                v5 = bm.verts.new((-1.1, -0.2, 0))
                v6 = bm.verts.new((-1.0, 0.1, 0))
                bm.faces.new((v4, v5, v6))
            elif shape_type == 'SLIDER':
                # A central line with arrows at the ends.
                v_start = bm.verts.new((0, -1.0, 0))
                v_end = bm.verts.new((0, 1.0, 0))
                bm.edges.new((v_start, v_end))
                t1 = bm.verts.new((0, 1.0, 0.2))
                t2 = bm.verts.new((0, 1.0, -0.2))
                t3 = bm.verts.new((0, 1.3, 0))
                bm.faces.new((t1, t2, t3))
                b1 = bm.verts.new((0, -1.0, 0.2))
                b2 = bm.verts.new((0, -1.0, -0.2))
                b3 = bm.verts.new((0, -1.3, 0))
                bm.faces.new((b3, b2, b1))
            elif shape_type == 'FIXED':
                # A simple cube to indicate a fixed joint.
                bmesh.ops.create_cube(bm, size=0.5)
            elif shape_type == 'SPHERICAL':
                # Wireframe sphere (3 circles) for ball-and-socket.
                bmesh.ops.create_circle(bm, cap_ends=False, radius=1.0, segments=32)
                bmesh.ops.create_circle(bm, cap_ends=False, radius=1.0, segments=32, matrix=mathutils.Matrix.Rotation(math.radians(90), 4, 'X'))
                bmesh.ops.create_circle(bm, cap_ends=False, radius=1.0, segments=32, matrix=mathutils.Matrix.Rotation(math.radians(90), 4, 'Y'))
        # Base is common for now
        if shape_type == 'BASE':
            # A 3-axis cross gizmo to represent a movable base.
            axis_len = 1.0
            axis_rad = 0.05
            # X axis (Red)
            bmesh.ops.create_cone(bm, cap_ends=True, radius1=axis_rad, radius2=axis_rad, depth=axis_len, segments=8, matrix=mathutils.Matrix.Rotation(math.radians(90), 4, 'Y'))
            # Y axis (Green)
            bmesh.ops.create_cone(bm, cap_ends=True, radius1=axis_rad, radius2=axis_rad, depth=axis_len, segments=8, matrix=mathutils.Matrix.Rotation(math.radians(-90), 4, 'X'))
            # Z axis (Blue)
            bmesh.ops.create_cone(bm, cap_ends=True, radius1=axis_rad, radius2=axis_rad, depth=axis_len, segments=8)
        # Rotate the generated geometry to match the target axis.
        # AI Editor Note: The 'BASE' gizmo is pre-aligned and should not be rotated.
        if shape_type != 'BASE':
            helper_type = 'prismatic' if shape_type == 'SLIDER' else 'revolute'
            rot_matrix = get_gizmo_rotation_matrix(helper_type, target_axis)
            bmesh.ops.rotate(bm, verts=bm.verts, cent=(0, 0, 0), matrix=rot_matrix)
        bm.to_mesh(mesh)
        mesh.update()
        bm.free()
        # Hide the template from selection and rendering, but keep it available for gizmo referencing.
        obj.hide_render = True
        obj.hide_select = True
        # Note: Do not use hide_viewport = True here, as some versions of Blender
        # may stop displaying custom shapes if their source object is globally hidden.
        # Instead, we rely on the collection being excluded or hidden from the main view.
        return obj
    except Exception as e:
        # If anything goes wrong, return None to prevent errors upstream.
        print(f"Error creating gizmo '{shape_name}': {e}")
        return None
def create_rotational_driver_gizmo_mesh() -> Optional[bpy.types.Object]:
    """
    Creates or retrieves a custom mesh object for the rotational driver gizmo.
    This mesh is designed to be used as the `custom_shape` for the Empty object
    that drives the chain animation, providing a visual cue similar to a
    continuous joint.
    The gizmo is a circle in the XY plane with small arrows, indicating rotation
    around the Z-axis.
    Returns:
        The gizmo object, or None if creation fails (e.g., during rendering).
    """
    # Do not attempt to create meshes while a render job is running.
    if bpy.app.is_job_running("RENDER"):
        return None
    try:
        gizmo_name = f"{WIDGET_PREFIX}_RotationalDriver"
        existing_obj = bpy.data.objects.get(gizmo_name)
        if existing_obj:
            return existing_obj
        mesh = bpy.data.meshes.new(gizmo_name)
        obj = bpy.data.objects.new(gizmo_name, mesh)
        obj.location = (0, 0, 0)
        coll = bpy.data.collections.get(WIDGETS_COLLECTION_NAME)
        if not coll:
            coll = bpy.data.collections.new(WIDGETS_COLLECTION_NAME)
            if bpy.context.scene:
                bpy.context.scene.collection.children.link(coll)
        if obj.name not in coll.objects:
            coll.objects.link(obj)
        bm = bmesh.new()
        bmesh.ops.create_circle(bm, cap_ends=False, radius=1.0, segments=32)
        v1 = bm.verts.new((0.9, 0.2, 0)); v2 = bm.verts.new((1.1, 0.2, 0)); v3 = bm.verts.new((1.0, -0.1, 0))
        bm.faces.new((v1, v2, v3))
        v4 = bm.verts.new((-0.9, -0.2, 0)); v5 = bm.verts.new((-1.1, -0.2, 0)); v6 = bm.verts.new((-1.0, 0.1, 0))
        bm.faces.new((v4, v5, v6))
        bm.to_mesh(mesh)
        bm.free()
        obj.hide_viewport = True
        obj.hide_render = True
        obj.hide_select = True
        return obj
    except Exception as e:
        print(f"Error creating rotational driver gizmo: {e}")
        return None
def setup_and_update_material(obj: bpy.types.Object, color: mathutils.Color) -> None:
    """
    Ensures an object has a URDF-managed material and sets its color.
    This function is the core of the real-time viewport material handling. It
    creates a unique, node-based material for the given object if one doesn't
    exist, assigns it, and then updates the 'Base Color' of its Principled BSDF
    node. This ensures that color changes are reflected instantly in the viewport.
    Args:
        obj: The mesh object to apply the material to.
        color: The RGBA color to set.
    """
    if not obj or obj.type != 'MESH':
        return
    # 1. Use a unique material name to avoid conflicts.
    mat_name = f"LSD_{obj.name}_VPMaterial"
    mat = bpy.data.materials.get(mat_name)
    if not mat:
        mat = bpy.data.materials.new(name=mat_name)
        mat.use_nodes = True
    # 2. Assign material to the object.
    # AI Editor Note: Use Object-linked materials to allow unique colors even if meshes are shared.
    if not obj.material_slots:
        # If no slots, add one to the mesh data (required to have a slot to override).
        obj.data.materials.append(mat)
    # Force the first slot to link to the Object and assign the unique material.
    # This ensures that duplicating the object (which shares mesh) allows for independent coloring.
    obj.material_slots[0].link = 'OBJECT'
    obj.material_slots[0].material = mat
    # 3. Find the Principled BSDF node and set its color.
    if mat.node_tree:
        # Ensure nodes are present
        if not mat.node_tree.nodes:
            mat.node_tree.nodes.new('ShaderNodeOutputMaterial')
        bsdf_node = next((n for n in mat.node_tree.nodes if n.type == 'BSDF_PRINCIPLED'), None)
        if not bsdf_node:
            # If no BSDF node, create one and link it for robustness.
            output_node = next((n for n in mat.node_tree.nodes if n.type == 'OUTPUT_MATERIAL'), None)
            if output_node:
                bsdf_node = mat.node_tree.nodes.new('ShaderNodeBsdfPrincipled')
                bsdf_node.location = output_node.location[0] - 250, output_node.location[1]
                mat.node_tree.links.new(bsdf_node.outputs['BSDF'], output_node.inputs['Surface'])
        if bsdf_node:
            bsdf_node.inputs['Base Color'].default_value = color
    # --- AI Editor Note: Update Geometry Nodes Material ---
    # For procedural parts like springs, we need to explicitly update the Set Material node
    # within the Geometry Nodes graph to ensure the color is applied to the generated geometry.
    for mod in obj.modifiers:
        if mod.type == 'NODES' and mod.node_group:
            # Check if node group is shared (users > 1). If so, make a unique copy.
            # This prevents changing the color of one spring from affecting duplicates.
            if mod.node_group.users > 1:
                new_group = mod.node_group.copy()
                new_group.name = f"LSD_Native_{obj.name}_Spring_GN"
                mod.node_group = new_group
            for node in mod.node_group.nodes:
                if node.type == 'SET_MATERIAL':
                    node.inputs['Material'].default_value = mat
def update_viewport_material(self: 'LSD_MaterialProperties', context: bpy.types.Context) -> None:
    """
    Update callback for the material color property. This function finds the
    associated object(s) and calls the material setup function.
    """
    # 'self' is the LSD_MaterialProperties group.
    # 'self.id_data' is the PropertyGroup that owns it (e.g., LSD_Properties).
    owner_prop_group = self.id_data
    if not owner_prop_group: return
    # 'owner_prop_group.id_data' is the Blender data-block (Object or PoseBone).
    owner_datablock = owner_prop_group.id_data
    if not owner_datablock: return
    objects_to_update = []
    if isinstance(owner_datablock, bpy.types.Object):
        # This is a parametric part (e.g., a gear or a chain curve).
        props = owner_datablock.lsd_pg_mech_props
        if props.category == 'CHAIN' and props.instanced_link_obj:
            # For chains, the material is applied to the hidden link object.
            objects_to_update.append(props.instanced_link_obj)
        else:
            objects_to_update.append(owner_datablock)
    elif isinstance(owner_datablock, bpy.types.PoseBone):
        # This is a bone, so update all child meshes.
        objects_to_update.extend(get_all_children_objects(owner_datablock, context))
    for obj in objects_to_update:
        setup_and_update_material(obj, self.color)
def create_driver(target_obj: bpy.types.Object, source_data_path: str, modifier_name: str, modifier_input_identifier: str, var_name: str = "var") -> None:
    """
    Creates a simple, robust, native driver to link a custom property to a
    modifier input.
    ...
    Args:
        target_obj: ...
        source_data_path: ...
        modifier_name: ...
        modifier_input_identifier: ...
        var_name: The name of the variable inside the driver expression (default "var").
    """
    # Construct the full data path to the modifier's input property.
    driver_path = f'modifiers["{modifier_name}"]["{modifier_input_identifier}"]'
    # Create the F-Curve and driver for the target property.
    fcurve = target_obj.driver_add(driver_path)
    if not fcurve:
        print(f"Error: Could not add driver to {target_obj.name} at path {driver_path}")
        return
    driver = fcurve.driver
    # Use the 'AVERAGE' type. For a single variable, this is the simplest and
    # most efficient way to pass its value directly to the driver's output.
    driver.type = 'AVERAGE'
    # Create a variable to read the value from the source custom property.
    # If a variable already exists, remove it to ensure a clean state.
    if driver.variables:
        driver.variables.remove(driver.variables[0])
    var = driver.variables.new()
    var.name = var_name
    var.type = 'SINGLE_PROP'
    # Set the variable's target to the object itself and the specified data path.
    var.targets[0].id = target_obj
    var.targets[0].data_path = source_data_path
    # With an 'AVERAGE' driver and one variable, the expression is implicitly
    # just the value of that variable.
    driver.expression = var_name
def get_unique_name(base_name: str) -> str:
    """
    Generates a unique object name in the current scene by appending a
    numbered suffix if the name is already taken.
    Args:
        base_name: The desired base name for the object.
    Returns:
        A unique name (e.g., "chassis" or "chassis.001").
    """
    name = base_name
    count = 1
    while name in bpy.data.objects:
        name = f"{base_name}.{count:03d}"
        count += 1
    return name
    # No constraints or parenting are needed. The Geometry Nodes setup is self-contained.
def setup_native_slinky(slinky_obj, start_empty, end_empty):
    """
    Sets up a curved, slinky-like spring using Geometry Nodes.
    The spring path is defined by a start object, an end object, and
    optional middle hooks.
    """
    gn_group_name = f"LSD_Native_Slinky_GN"
    gn_group = bpy.data.node_groups.get(gn_group_name)
    if not gn_group:
        gn_group = bpy.data.node_groups.new(name=gn_group_name, type='GeometryNodeTree')
        gn_group.interface.new_socket(name="Geometry", in_out='OUTPUT', socket_type='NodeSocketGeometry')
        gn_group.interface.new_socket(name="Geometry", in_out='INPUT', socket_type='NodeSocketGeometry')
        start_obj_sock = gn_group.interface.new_socket(name="Start Object", in_out='INPUT', socket_type='NodeSocketObject')
        end_obj_sock = gn_group.interface.new_socket(name="End Object", in_out='INPUT', socket_type='NodeSocketObject')
        hooks_coll_sock = gn_group.interface.new_socket(name="Hooks Collection", in_out='INPUT', socket_type='NodeSocketCollection')
        rad_sock = gn_group.interface.new_socket(name="Spring Radius", in_out='INPUT', socket_type='NodeSocketFloat')
        rad_sock.default_value = 0.05
        turns_sock = gn_group.interface.new_socket(name="Turns", in_out='INPUT', socket_type='NodeSocketFloat')
        turns_sock.default_value = 20.0
        wire_sock = gn_group.interface.new_socket(name="Wire Radius", in_out='INPUT', socket_type='NodeSocketFloat')
        wire_sock.default_value = 0.002
        res_sock = gn_group.interface.new_socket(name="Resolution", in_out='INPUT', socket_type='NodeSocketInt')
        res_sock.default_value = 512
        nodes = gn_group.nodes
        links = gn_group.links
        g_in = nodes.get("Group Input") or nodes.new('NodeGroupInput')
        g_out = nodes.get("Group Output") or nodes.new('NodeGroupOutput')
        # --- 1. Get Positions ---
        info_start = nodes.new('GeometryNodeObjectInfo'); info_start.transform_space = 'RELATIVE'
        info_end = nodes.new('GeometryNodeObjectInfo'); info_end.transform_space = 'RELATIVE'
        links.new(g_in.outputs[start_obj_sock.name], info_start.inputs['Object'])
        links.new(g_in.outputs[end_obj_sock.name], info_end.inputs['Object'])
        # --- 2. Create Backbone ---
        # AI Editor Note: Get middle hooks and join them with start/end in a single path.
        coll_info = nodes.new('GeometryNodeCollectionInfo'); coll_info.transform_space = 'RELATIVE'
        coll_info.inputs['Separate Children'].default_value = True
        links.new(g_in.outputs[hooks_coll_sock.name], coll_info.inputs['Collection'])
        # We need to convert start/end objects to points too
        p_start = nodes.new('GeometryNodeMeshLine')
        p_start.inputs['Count'].default_value = 1
        links.new(info_start.outputs['Location'], p_start.inputs['Start Location'])
        p_end = nodes.new('GeometryNodeMeshLine')
        p_end.inputs['Count'].default_value = 1
        links.new(info_end.outputs['Location'], p_end.inputs['Start Location'])
        join_pts = nodes.new('GeometryNodeJoinGeometry')
        links.new(p_start.outputs['Mesh'], join_pts.inputs['Geometry'])
        links.new(coll_info.outputs['Instances'], join_pts.inputs['Geometry'])
        links.new(p_end.outputs['Mesh'], join_pts.inputs['Geometry'])
        try:
            p_to_c = nodes.new('GeometryNodePointsToCurves')
        except Exception:
            p_to_c = nodes.new('GeometryNodePointsToCurve')
        links.new(join_pts.outputs['Geometry'], p_to_c.inputs['Points'])
        resample = nodes.new('GeometryNodeResampleCurve')
        # Handle socket naming changes in Blender 4.0+ (Points to Curves uses 'Curves' output)
        p_to_c_out = p_to_c.outputs.get('Curves') or p_to_c.outputs.get('Curve')
        resample_in = resample.inputs.get('Curve') or resample.inputs.get('Curves')
        links.new(p_to_c_out, resample_in)
        links.new(g_in.outputs[res_sock.name], resample.inputs['Count'])
        # We move each point of the resampled curve in a circle perpendicular to its tangent.
        set_pos = nodes.new('GeometryNodeSetPosition')
        resample_out = resample.outputs.get('Curve') or resample.outputs.get('Curves')
        links.new(resample_out, set_pos.inputs['Geometry'])
        # Rotation angle = 2 * PI * Turns * Curve Parameter
        param = nodes.new('GeometryNodeInputCurveHandleType') # Wait, I need Curve Parameter
        param = nodes.new('GeometryNodeSplineParameter')
        math_angle = nodes.new('ShaderNodeMath'); math_angle.operation = 'MULTIPLY'
        links.new(param.outputs['Factor'], math_angle.inputs[0])
        links.new(g_in.outputs[turns_sock.name], math_angle.inputs[1])
        math_2pi = nodes.new('ShaderNodeMath'); math_2pi.operation = 'MULTIPLY'; math_2pi.inputs[1].default_value = 6.283185
        links.new(math_angle.outputs['Value'], math_2pi.inputs[0])
        # Circular offset in local space (R * cos, R * sin, 0)
        cos = nodes.new('ShaderNodeMath'); cos.operation = 'COS'
        sin = nodes.new('ShaderNodeMath'); sin.operation = 'SIN'
        links.new(math_2pi.outputs['Value'], cos.inputs[0])
        links.new(math_2pi.outputs['Value'], sin.inputs[0])
        comb_vec = nodes.new('ShaderNodeCombineXYZ')
        links.new(cos.outputs['Value'], comb_vec.inputs[0])
        links.new(sin.outputs['Value'], comb_vec.inputs[1])
        # Scale by Radius
        math_rad = nodes.new('ShaderNodeVectorMath'); math_rad.operation = 'SCALE'
        links.new(comb_vec.outputs['Vector'], math_rad.inputs['Vector'])
        links.new(g_in.outputs[rad_sock.name], math_rad.inputs['Scale'])
        # Align to Tangent
        # We need the tangent of the curve to orient the circle.
        tan = nodes.new('GeometryNodeInputTangent')
        align = nodes.new('GeometryNodeAlignEulerToVector'); align.axis = 'Z'
        links.new(tan.outputs['Tangent'], align.inputs['Vector'])
        vec_rot = nodes.new('ShaderNodeVectorMath'); vec_rot.name = "Vector Rotate" # Custom node or use Rotate Vector
        vec_rot = nodes.new('GeometryNodeRotateVector')
        links.new(math_rad.outputs['Vector'], vec_rot.inputs['Vector'])
        links.new(align.outputs['Rotation'], vec_rot.inputs['Rotation'])
        links.new(vec_rot.outputs['Vector'], set_pos.inputs['Offset'])
        # --- 4. Profile ---
        profile = nodes.new('GeometryNodeCurvePrimitiveCircle'); profile.inputs['Resolution'].default_value = 8
        links.new(g_in.outputs[wire_sock.name], profile.inputs['Radius'])
        to_mesh = nodes.new('GeometryNodeCurveToMesh')
        to_mesh_in = to_mesh.inputs.get('Curve') or to_mesh.inputs.get('Curves')
        links.new(set_pos.outputs['Geometry'], to_mesh_in)
        profile_out = profile.outputs.get('Curve') or profile.outputs.get('Curves')
        links.new(profile_out, to_mesh.inputs['Profile Curve'])
        # Material
        set_mat = nodes.new('GeometryNodeSetMaterial')
        links.new(to_mesh.outputs['Mesh'], set_mat.inputs['Geometry'])
        links.new(set_mat.outputs['Geometry'], g_out.inputs['Geometry'])
    # --- Add Modifier ---
    mod = slinky_obj.modifiers.get(NATIVE_SLINKY_MOD_NAME)
    if not mod:
        mod = slinky_obj.modifiers.new(name=NATIVE_SLINKY_MOD_NAME, type='NODES')
    mod.node_group = gn_group
    # Drivers
    props = slinky_obj.lsd_pg_mech_props
    mod["Socket_2"] = start_empty # Start Object
    mod["Socket_3"] = end_empty # End Object
    # Link the hooks collection
    if props.slinky_hooks:
        # AI Editor Note: In Blender 4.0+, we can directly assign the collection.
        # However, for robustness we ensure the collection exists and is linked.
        coll_name = f"LSD_SlinkyHooks_{slinky_obj.name}"
        coll = bpy.data.collections.get(coll_name)
        if not coll:
            coll = bpy.data.collections.new(coll_name)
            bpy.context.scene.collection.children.link(coll)
        # Populate collection from the PointerProperty objects
        for item in props.slinky_hooks:
            if item.target and item.target.name not in coll.objects:
                coll.objects.link(item.target)
        mod["Socket_4"] = coll # Hooks Collection
    # Drive properties
    def add_driver(target_obj, data_path, prop_owner, prop_path):
        d = target_obj.driver_add(data_path).driver
        v = d.variables.new()
        v.name = "var"
        v.type = 'SINGLE_PROP'
        v.targets[0].id = prop_owner
        v.targets[0].data_path = prop_path
        d.expression = "var"
    add_driver(mod, '["Socket_5"]', slinky_obj, "lsd_pg_mech_props.radius")
    add_driver(mod, '["Socket_6"]', slinky_obj, "lsd_pg_mech_props.teeth")
    add_driver(mod, '["Socket_7"]', slinky_obj, "lsd_pg_mech_props.tooth_depth") # Repurposed as wire radius
    # Auto-smooth
    apply_auto_smooth(slinky_obj)
    # --- Add and Configure the Geometry Nodes Modifier ---
    mod = damper_obj.modifiers.get(NATIVE_DAMPER_MOD_NAME)
    if not mod:
        mod = damper_obj.modifiers.new(name=NATIVE_DAMPER_MOD_NAME, type='NODES')
    mod.node_group = gn_group
    # Connect Drivers
    if start_obj_socket: mod[start_obj_socket.identifier] = start_empty
    if end_obj_socket: mod[end_obj_socket.identifier] = end_empty
    if rad_sock: create_driver(damper_obj, '["spring_radius"]', mod.name, rad_sock.identifier)
    if wire_sock: create_driver(damper_obj, '["spring_wire_thickness"]', mod.name, wire_sock.identifier)
    if len_sock: create_driver(damper_obj, 'lsd_pg_mech_props.height', mod.name, len_sock.identifier)
    if piston_len_sock: create_driver(damper_obj, 'lsd_pg_mech_props.height', mod.name, piston_len_sock.identifier)
    if turns_sock: create_driver(damper_obj, '["spring_teeth"]', mod.name, turns_sock.identifier)
    if housing_rad_sock: create_driver(damper_obj, '["damper_housing_radius"]', mod.name, housing_rad_sock.identifier)
    if rod_rad_sock: create_driver(damper_obj, '["damper_rod_radius"]', mod.name, rod_rad_sock.identifier)
    if seat_rad_sock: create_driver(damper_obj, '["damper_seat_radius"]', mod.name, seat_rad_sock.identifier)
    if seat_thick_sock: create_driver(damper_obj, '["damper_seat_thickness"]', mod.name, seat_thick_sock.identifier)
def setup_native_rope_gn(rope_obj: bpy.types.Object) -> None:
    """
    High-Fidelity Rope Generator.
    Restores generation by ensuring stable GN linkage and driver assignment.
    """
    gn_group_name = f"LSD_Native_{rope_obj.name}_Rope_GN"
    gn_group = bpy.data.node_groups.get(gn_group_name)
    if not gn_group:
        gn_group = bpy.data.node_groups.new(name=gn_group_name, type='GeometryNodeTree')
        iface = gn_group.interface
        # --- 1. Sockets & Inputs ---
        in_geom = iface.new_socket(name="Geometry", in_out="INPUT", socket_type='NodeSocketGeometry')
        in_rad = iface.new_socket(name="Total Radius", in_out="INPUT", socket_type='NodeSocketFloat')
        in_rad.default_value = 0.01; in_rad.min_value = 0.0001
        in_strands = iface.new_socket(name="Strands", in_out="INPUT", socket_type='NodeSocketInt')
        in_strands.default_value = 6; in_strands.min_value = 1
        in_twist = iface.new_socket(name="Twist Rate", in_out="INPUT", socket_type='NodeSocketFloat')
        in_twist.default_value = 10.0
        in_tube = iface.new_socket(name="Tube Mode", in_out="INPUT", socket_type='NodeSocketBool')
        in_synth = iface.new_socket(name="Is Synthetic", in_out="INPUT", socket_type='NodeSocketBool')
        iface.new_socket(name="Geometry", in_out="OUTPUT", socket_type='NodeSocketGeometry')
        nodes = gn_group.nodes
        links = gn_group.links
        g_in = nodes.new('NodeGroupInput'); g_in.location = (-1500, 0)
        g_out = nodes.new('NodeGroupOutput'); g_out.location = (2800, 0)
        # Nodes
        m_to_c = nodes.new('GeometryNodeMeshToCurve'); m_to_c.location = (-1300, 500)
        resam = nodes.new('GeometryNodeResampleCurve'); resam.location = (-1100, 500)        # BALANCED RES: 2mm segments for smooth viewport performance
        resam.mode = 'LENGTH'; resam.inputs['Length'].default_value = 0.002
        dup = nodes.new('GeometryNodeDuplicateElements'); dup.domain = 'SPLINE'; dup.location = (-900, 500)
        para = nodes.new('GeometryNodeSplineParameter'); para.location = (-700, 400)
        idx = nodes.new('GeometryNodeInputIndex'); idx.location = (-700, 300)
        # Packing Math
        m_v_pi = nodes.new('ShaderNodeValue'); m_v_pi.outputs[0].default_value = math.pi
        m_pi = nodes.new('ShaderNodeMath'); m_pi.operation = 'DIVIDE'
        links.new(m_v_pi.outputs[0], m_pi.inputs[0])
        m_sin_a = nodes.new('ShaderNodeMath'); m_sin_a.operation = 'SINE'
        m_plus1 = nodes.new('ShaderNodeMath'); m_plus1.operation = 'ADD'; m_plus1.inputs[1].default_value = 1.0
        m_ring_r = nodes.new('ShaderNodeMath'); m_ring_r.operation = 'DIVIDE'
        m_strand_r = nodes.new('ShaderNodeMath'); m_strand_r.operation = 'MULTIPLY'
        # Angle Math
        m_v_2pi = nodes.new('ShaderNodeValue'); m_v_2pi.outputs[0].default_value = math.pi*2
        m_2pi = nodes.new('ShaderNodeMath'); m_2pi.operation = 'MULTIPLY'; m_2pi.inputs[1].default_value = 1.0 # Force 1.0 multiplier
        links.new(m_v_2pi.outputs[0], m_2pi.inputs[0])
        m_angle_step = nodes.new('ShaderNodeMath'); m_angle_step.operation = 'DIVIDE'
        m_base_angle = nodes.new('ShaderNodeMath'); m_base_angle.operation = 'MULTIPLY'
        m_twist_acc = nodes.new('ShaderNodeMath'); m_twist_acc.operation = 'MULTIPLY'
        # Lay & Weave
        m_lay_sw = nodes.new('GeometryNodeSwitch'); m_lay_sw.input_type = 'FLOAT'
        m_lay_sw.inputs['False'].default_value = 1.0
        m_mod2 = nodes.new('ShaderNodeMath'); m_mod2.operation = 'MODULO'; m_mod2.inputs[1].default_value = 2.0
        m_dir = nodes.new('ShaderNodeMath'); m_dir.operation = 'MULTIPLY'; m_dir.inputs[1].default_value = -2.0
        m_dir_add = nodes.new('ShaderNodeMath'); m_dir_add.operation = 'ADD'; m_dir_add.inputs[1].default_value = 1.0
        m_final_twist = nodes.new('ShaderNodeMath'); m_final_twist.operation = 'MULTIPLY'
        m_total_angle = nodes.new('ShaderNodeMath'); m_total_angle.operation = 'ADD'
        # Frenet vectors
        tan_v = nodes.new('GeometryNodeInputTangent')
        norm_v = nodes.new('GeometryNodeInputNormal')
        binorm_v = nodes.new('ShaderNodeVectorMath'); binorm_v.operation = 'CROSS_PRODUCT'
        # Pos Offset
        m_cos = nodes.new('ShaderNodeMath'); m_cos.operation = 'COSINE'
        m_sin = nodes.new('ShaderNodeMath'); m_sin.operation = 'SINE'
        m_rcos = nodes.new('ShaderNodeMath'); m_rcos.operation = 'MULTIPLY'
        m_rsin = nodes.new('ShaderNodeMath'); m_rsin.operation = 'MULTIPLY'
        v_off_n = nodes.new('ShaderNodeVectorMath'); v_off_n.operation = 'SCALE'
        v_off_b = nodes.new('ShaderNodeVectorMath'); v_off_b.operation = 'SCALE'
        v_sum = nodes.new('ShaderNodeVectorMath'); v_sum.operation = 'ADD'
        # Synthesis Weave
        m_wv_sine = nodes.new('ShaderNodeMath'); m_wv_sine.operation = 'SINE'
        m_wv_amp = nodes.new('ShaderNodeMath'); m_wv_amp.operation = 'MULTIPLY'
        m_sw_wv = nodes.new('GeometryNodeSwitch'); m_sw_wv.input_type = 'FLOAT'
        m_ring_final = nodes.new('ShaderNodeMath'); m_ring_final.operation = 'ADD'
        # Geometry
        set_pos = nodes.new('GeometryNodeSetPosition')
        prof_c = nodes.new('GeometryNodeCurvePrimitiveCircle'); prof_c.inputs['Resolution'].default_value = 12
        sweep = nodes.new('GeometryNodeCurveToMesh')
        # Core
        comp_core = nodes.new('FunctionNodeCompare'); comp_core.data_type = 'INT'; comp_core.operation = 'GREATER_EQUAL'; comp_core.inputs['B'].default_value = 5
        m_not_synth = nodes.new('ShaderNodeMath'); m_not_synth.operation = 'SUBTRACT'; m_not_synth.inputs[0].default_value = 1.0
        m_use_core = nodes.new('ShaderNodeMath'); m_use_core.operation = 'MULTIPLY'
        sw_core = nodes.new('GeometryNodeSwitch'); sw_core.input_type = 'GEOMETRY'
        core_sweep = nodes.new('GeometryNodeCurveToMesh'); core_prof = nodes.new('GeometryNodeCurvePrimitiveCircle'); core_prof.inputs['Resolution'].default_value = 12
        # Output Join
        join = nodes.new('GeometryNodeJoinGeometry')
        sw_tube = nodes.new('GeometryNodeSwitch'); sw_tube.input_type = 'GEOMETRY'
        tube_prof = nodes.new('GeometryNodeCurvePrimitiveCircle'); tube_prof.inputs['Resolution'].default_value = 12
        tube_sweep = nodes.new('GeometryNodeCurveToMesh')
        set_mat = nodes.new('GeometryNodeSetMaterial')
        # --- Linkage ---
        links.new(g_in.outputs[0], m_to_c.inputs['Mesh'])
        links.new(m_to_c.outputs['Curve'], resam.inputs['Curve'])
        links.new(resam.outputs['Curve'], dup.inputs['Geometry'])
        links.new(g_in.outputs[in_strands.name], dup.inputs['Amount'])
        # Packing
        links.new(g_in.outputs[in_strands.name], m_pi.inputs[1]); links.new(m_pi.outputs[0], m_sin_a.inputs[0])
        links.new(m_sin_a.outputs[0], m_plus1.inputs[0]); links.new(m_plus1.outputs[0], m_ring_r.inputs[1])
        links.new(g_in.outputs[in_rad.name], m_ring_r.inputs[0])
        links.new(m_ring_r.outputs[0], m_strand_r.inputs[0]); links.new(m_sin_a.outputs[0], m_strand_r.inputs[1])
        # Angle
        links.new(g_in.outputs[in_strands.name], m_angle_step.inputs[1]); links.new(m_2pi.outputs[0], m_angle_step.inputs[0])
        links.new(m_angle_step.outputs[0], m_base_angle.inputs[0]); links.new(dup.outputs['Duplicate Index'], m_base_angle.inputs[1])
        links.new(g_in.outputs[in_twist.name], m_twist_acc.inputs[1]); links.new(para.outputs['Factor'], m_twist_acc.inputs[0])
        # Lay Dir
        links.new(dup.outputs['Duplicate Index'], m_mod2.inputs[0]); links.new(m_mod2.outputs[0], m_dir.inputs[0])
        links.new(m_dir.outputs[0], m_dir_add.inputs[0]); links.new(m_dir_add.outputs[0], m_lay_sw.inputs['True'])
        links.new(g_in.outputs[in_synth.name], m_lay_sw.inputs['Switch'])
        links.new(m_twist_acc.outputs[0], m_final_twist.inputs[0]); links.new(m_lay_sw.outputs[0], m_final_twist.inputs[1])
        links.new(m_base_angle.outputs[0], m_total_angle.inputs[0]); links.new(m_final_twist.outputs[0], m_total_angle.inputs[1])
        # Stable Basis (Fallback protected Cross Products)
        up_v = nodes.new('ShaderNodeVectorMath'); up_v.operation = 'CROSS_PRODUCT'
        up_v.inputs[1].default_value = (0, 0, 1) # Primary Up
        # Fallback check
        v_len = nodes.new('ShaderNodeVectorMath'); v_len.operation = 'LENGTH'
        v_comp = nodes.new('FunctionNodeCompare'); v_comp.data_type = 'FLOAT'; v_comp.operation = 'LESS_THAN'; v_comp.inputs['B'].default_value = 0.1
        v_up_alt = nodes.new('GeometryNodeSwitch'); v_up_alt.input_type = 'VECTOR'
        v_up_alt.inputs['False'].default_value = (0, 0, 1)
        v_up_alt.inputs['True'].default_value = (0, 1, 0)
        links.new(tan_v.outputs[0], up_v.inputs[0])
        links.new(up_v.outputs[0], v_len.inputs[1]); links.new(v_len.outputs[0], v_comp.inputs['A'])
        links.new(v_comp.outputs[0], v_up_alt.inputs['Switch'])
        # Basis 1 (Normal-like)
        b1_v = nodes.new('ShaderNodeVectorMath'); b1_v.operation = 'CROSS_PRODUCT'
        links.new(tan_v.outputs[0], b1_v.inputs[0]); links.new(v_up_alt.outputs[0], b1_v.inputs[1])
        # Basis 2 (Binormal-like)
        b2_v = nodes.new('ShaderNodeVectorMath'); b2_v.operation = 'CROSS_PRODUCT'
        links.new(tan_v.outputs[0], b2_v.inputs[0]); links.new(b1_v.outputs[0], b2_v.inputs[1])
        # Weave Logic (Synthetic Rope Only)
        links.new(m_total_angle.outputs[0], m_wv_sine.inputs[0])
        links.new(m_wv_sine.outputs[0], m_wv_amp.inputs[0]); links.new(m_strand_r.outputs[0], m_wv_amp.inputs[1])
        links.new(g_in.outputs[in_synth.name], m_sw_wv.inputs['Switch'])
        links.new(m_wv_amp.outputs[0], m_sw_wv.inputs['True']); m_sw_wv.inputs['False'].default_value = 0.0
        links.new(m_ring_r.outputs[0], m_ring_final.inputs[0]); links.new(m_sw_wv.outputs[0], m_ring_final.inputs[1])
        links.new(m_total_angle.outputs[0], m_cos.inputs[0]); links.new(m_total_angle.outputs[0], m_sin.inputs[0])
        links.new(m_ring_final.outputs[0], m_rcos.inputs[0]); links.new(m_cos.outputs[0], m_rcos.inputs[1])
        links.new(m_ring_final.outputs[0], m_rsin.inputs[0]); links.new(m_sin.outputs[0], m_rsin.inputs[1])
        links.new(b1_v.outputs[0], v_off_n.inputs['Vector']); links.new(m_rcos.outputs[0], v_off_n.inputs['Scale'])
        links.new(b2_v.outputs[0], v_off_b.inputs['Vector']); links.new(m_rsin.outputs[0], v_off_b.inputs['Scale'])
        links.new(v_off_n.outputs[0], v_sum.inputs[0]); links.new(v_off_b.outputs[0], v_sum.inputs[1])
        links.new(dup.outputs['Geometry'], set_pos.inputs['Geometry']); links.new(v_sum.outputs[0], set_pos.inputs['Offset'])
        # Sweep
        # OVERSIZE FIX: Synthetic strands are oversized by 15% to eliminate gaps
        m_sw_sz = nodes.new('GeometryNodeSwitch'); m_sw_sz.input_type = 'FLOAT'
        links.new(g_in.outputs[in_synth.name], m_sw_sz.inputs['Switch'])
        m_ov_sz = nodes.new('ShaderNodeMath'); m_ov_sz.operation = 'MULTIPLY'; m_ov_sz.inputs[1].default_value = 1.15
        links.new(m_strand_r.outputs[0], m_ov_sz.inputs[0]); links.new(m_ov_sz.outputs[0], m_sw_sz.inputs['True'])
        links.new(m_strand_r.outputs[0], m_sw_sz.inputs['False'])
        links.new(set_pos.outputs['Geometry'], sweep.inputs['Curve']); links.new(prof_c.outputs['Curve'], sweep.inputs['Profile Curve'])
        links.new(m_sw_sz.outputs[0], prof_c.inputs['Radius'])
        # Core & Join
        links.new(g_in.outputs[in_strands.name], comp_core.inputs['A'])
        m_use_core = nodes.new('ShaderNodeMath'); m_use_core.operation = 'MAXIMUM'
        links.new(g_in.outputs[in_synth.name], m_use_core.inputs[0])
        links.new(comp_core.outputs['Result'], m_use_core.inputs[1])
        links.new(m_use_core.outputs[0], sw_core.inputs['Switch'])
        links.new(resam.outputs['Curve'], core_sweep.inputs['Curve']); links.new(core_prof.outputs['Curve'], core_sweep.inputs['Profile Curve'])
        # CORE RADIUS FIX: Core should fill the space (RingRadius - StrandRadius)
        m_c_rad = nodes.new('ShaderNodeMath'); m_c_rad.operation = 'SUBTRACT'
        links.new(m_ring_r.outputs[0], m_c_rad.inputs[0]); links.new(m_strand_r.outputs[0], m_c_rad.inputs[1])
        links.new(m_c_rad.outputs[0], core_prof.inputs['Radius'])
        links.new(core_sweep.outputs['Mesh'], sw_core.inputs['True'])
        links.new(sweep.outputs['Mesh'], join.inputs[0]); links.new(sw_core.outputs[0], join.inputs[0])
        # Tube Swapper
        links.new(resam.outputs['Curve'], tube_sweep.inputs['Curve']); links.new(tube_prof.outputs['Curve'], tube_sweep.inputs['Profile Curve'])
        links.new(g_in.outputs[in_rad.name], tube_prof.inputs['Radius'])
        links.new(g_in.outputs[in_tube.name], sw_tube.inputs['Switch'])
        links.new(join.outputs['Geometry'], sw_tube.inputs['False']); links.new(tube_sweep.outputs['Mesh'], sw_tube.inputs['True'])
        links.new(sw_tube.outputs[0], set_mat.inputs['Geometry'])
        links.new(set_mat.outputs['Geometry'], g_out.inputs[0])
    iface = gn_group.interface
    if hasattr(rope_obj.data, "twist_mode"): rope_obj.data.twist_mode = 'MINIMUM'
    mod = rope_obj.modifiers.get(f"{MOD_PREFIX}Native_Rope")
    if not mod: mod = rope_obj.modifiers.new(name=f"{MOD_PREFIX}Native_Rope", type='NODES')
    mod.node_group = gn_group
    # Drivers
    pref = '["rope_radius"]', '["rope_strands"]', '["rope_twist"]', '["rope_tube_mode"]', '["rope_is_synthetic"]'
    keys = "Total Radius", "Strands", "Twist Rate", "Tube Mode", "Is Synthetic"
    for k, p in zip(keys, pref):
        sock = iface.items_tree.get(k)
        if sock: create_driver(rope_obj, p, mod.name, sock.identifier)
def setup_native_wrap_gn(path_obj: bpy.types.Object) -> None:
    """
    Creates or updates the 'Wrap' Geometry Nodes modifier.
    This modifier is responsible for the "Dynamic Wrapping" feature. It takes a
    collection of objects and generates a convex hull curve around them.
    It is placed at the top of the modifier stack so that it feeds this generated
    path into the subsequent 'Chain' modifier.
    """
    gn_group_name = f"LSD_Native_{path_obj.name}_Wrap_GN"
    gn_group = bpy.data.node_groups.get(gn_group_name)
    if not gn_group:
        gn_group = bpy.data.node_groups.new(name=gn_group_name, type='GeometryNodeTree')
    # --- Clear existing nodes and interface to ensure a clean, up-to-date build ---
    # This guarantees that any logic changes in the script are applied to existing objects.
    gn_group.nodes.clear()
    gn_group.interface.clear()
    iface = gn_group.interface
    iface.new_socket(name="Geometry", in_out="INPUT", socket_type='NodeSocketGeometry')
    iface.new_socket(name="Wrap Collection", in_out="INPUT", socket_type='NodeSocketCollection')
    # ADDED: Input for resolution to control vertex density
    res_socket = iface.new_socket(name="Resolution", in_out="INPUT", socket_type='NodeSocketFloat')
    res_socket.default_value = 0.1
    res_socket.min_value = 0.001
    res_socket.description = "Target distance between vertices"
    iface.new_socket(name="Geometry", in_out="OUTPUT", socket_type='NodeSocketGeometry')
    nodes = gn_group.nodes
    links = gn_group.links
    group_input = nodes.new('NodeGroupInput')
    group_input.location = (-1800, 0)
    group_output = nodes.new('NodeGroupOutput')
    group_output.location = (1600, 0)
    # --- Node Logic: Wrap Collection (Convex Hull) ---
    col_info = nodes.new('GeometryNodeCollectionInfo')
    col_info.location = (-1400, 200)
    col_info.transform_space = 'RELATIVE'
    col_info.inputs['Separate Children'].default_value = True # Ensure nested hierarchies work
    # --- Data Gathering: Collect all points to wrap around ---
    # 1. Realize Instances to get actual mesh vertices from the collection.
    realize_instances = nodes.new('GeometryNodeRealizeInstances')
    realize_instances.location = (-1200, 200)
    # 2. Convert Instances to Points to capture Empties (which would otherwise disappear).
    instances_to_points = nodes.new('GeometryNodeInstancesToPoints')
    instances_to_points.location = (-1200, 0)
    # AI Editor Note: Removed curve_input_points to prevent the "invisible hook" issue.
    # The convex hull should be defined strictly by the wrap objects, not the original curve path.
    # 3. Join all these points into a single cloud for the Convex Hull.
    # NOTE: We intentionally do NOT include the original curve points here.
    # Including them would cause the default curve shape (e.g., a large circle)
    # to dominate the hull, preventing the chain from wrapping tightly around
    # the selected objects. The wrap path should be defined purely by the collection.
    join_geom = nodes.new('GeometryNodeJoinGeometry')
    join_geom.location = (-1000, 200)
    # Clean up points before hull to avoid degenerate geometry
    merge_dist = nodes.new('GeometryNodeMergeByDistance')
    merge_dist.location = (-600, 200)
    merge_dist.inputs['Distance'].default_value = 0.001
    convex_hull = nodes.new('GeometryNodeConvexHull')
    convex_hull.location = (-400, 200)
    # --- Node Logic: Hull Cleanup ---
    # AI Editor Note: Convex Hull on flat points can sometimes produce a double-sided
    # "pancake" mesh. We delete downward-facing faces to ensure a single layer,
    # which is required for the boundary extraction logic to work correctly.
    delete_bottom = nodes.new('GeometryNodeDeleteGeometry')
    delete_bottom.domain = 'FACE'
    delete_bottom.location = (-200, 300)
    normal_node = nodes.new('GeometryNodeInputNormal')
    normal_node.location = (-400, 400)
    sep_z = nodes.new('ShaderNodeSeparateXYZ')
    sep_z.location = (-250, 400)
    # AI Editor Note: Using 0.0 instead of -0.1 to be more robust for perfectly horizontal loops.
    # This ensures that we only keep the "top" faces, which allows the boundary extraction
    # to find the outer rim of the hull.
    compare_z = nodes.new('FunctionNodeCompare'); compare_z.operation = 'LESS_THAN'; compare_z.location = (-100, 400); compare_z.inputs['B'].default_value = 0.0
    # --- Node Logic: Boundary Extraction ---
    # The Convex Hull node outputs a filled mesh (often triangulated).
    # To get a clean path for the chain, we must delete internal edges
    # and keep only the boundary loop. We do this by keeping edges that
    # have exactly one adjacent face.
    edge_neighbors = nodes.new('GeometryNodeInputMeshEdgeNeighbors')
    edge_neighbors.location = (-400, 100)
    compare_edges = nodes.new('FunctionNodeCompare')
    # AI Editor Note: Boundary extraction logic. Boundary edges have exactly 1 face neighbor.
    # Interior edges have > 1. By deleting edges with > 1 neighbor, we isolate the loop.
    compare_edges.data_type = 'INT'
    compare_edges.operation = 'NOT_EQUAL' # Keep neighbor count == 1
    compare_edges.location = (-200, 100)
    compare_edges.inputs['B'].default_value = 1
    delete_geom = nodes.new('GeometryNodeDeleteGeometry')
    delete_geom.domain = 'EDGE'
    delete_geom.location = (0, 200)
    # --- Node Logic: Curve Finalization ---
    mesh_to_curve = nodes.new('GeometryNodeMeshToCurve')
    mesh_to_curve.location = (200, 200)
    # NEW: Force Zero Tilt to prevent Mobius twisting on the generated path
    set_tilt = nodes.new('GeometryNodeSetCurveTilt')
    set_tilt.location = (300, 200)
    set_tilt.inputs['Tilt'].default_value = 0.0
    # Force the curve to be cyclic (closed loop) to ensure a continuous chain.
    set_cyclic = nodes.new('GeometryNodeSetSplineCyclic')
    set_cyclic.location = (400, 200)
    set_cyclic.inputs['Cyclic'].default_value = True
    # ADDED: Resample the curve so vertices match the chain pitch.
    # This ensures the wrapping geometry has the correct resolution for the links.
    resample_curve = nodes.new('GeometryNodeResampleCurve')
    resample_curve.location = (600, 200)
    resample_curve.mode = 'LENGTH'
    # Set a default radius for the generated curve to ensure visibility
    set_radius = nodes.new('GeometryNodeSetCurveRadius')
    set_radius.location = (800, 200)
    set_radius.inputs['Radius'].default_value = 1.0
    # --- Node Logic: Smart Switch ---
    # We want to use the generated wrap curve ONLY if:
    # 1. The user has actually selected objects to wrap (Collection is not empty).
    # 2. The wrapping process succeeded and produced valid geometry.
    # Otherwise, we pass through the original user-drawn curve. This allows for
    # manual S-curves or other non-convex shapes when not using the wrap feature.
    # Check 1: Is the collection populated?
    col_size = nodes.new('GeometryNodeAttributeDomainSize')
    col_size.location = (-1200, 500)
    col_size.component = 'INSTANCES'
    col_has_items = nodes.new('FunctionNodeCompare')
    col_has_items.location = (-1000, 500)
    col_has_items.data_type = 'INT'
    col_has_items.operation = 'GREATER_THAN'
    col_has_items.inputs['B'].default_value = 0
    # Check 2: Did the hull generation produce a curve?
    hull_size = nodes.new('GeometryNodeAttributeDomainSize')
    hull_size.location = (800, 400)
    hull_size.component = 'CURVE'
    hull_valid = nodes.new('FunctionNodeCompare')
    hull_valid.location = (1000, 400)
    hull_valid.data_type = 'INT'
    hull_valid.operation = 'GREATER_THAN'
    hull_valid.inputs['B'].default_value = 0
    # Combine checks: (Collection > 0) AND (Hull > 0)
    logic_and = nodes.new('FunctionNodeBooleanMath')
    logic_and.location = (1200, 400)
    logic_and.operation = 'AND'
    switch = nodes.new('GeometryNodeSwitch')
    switch.location = (1400, 0)
    switch.input_type = 'GEOMETRY'
    # --- Links ---
    links.new(group_input.outputs["Wrap Collection"], col_info.inputs['Collection'])
    # Connect robust geometry gathering
    links.new(col_info.outputs['Instances'], realize_instances.inputs['Geometry'])
    links.new(col_info.outputs['Instances'], instances_to_points.inputs['Instances'])
    links.new(realize_instances.outputs['Geometry'], join_geom.inputs['Geometry'])
    links.new(instances_to_points.outputs['Points'], join_geom.inputs['Geometry'])
    # AI Editor Note: Direct connection to allow 3D wrapping (no flattening).
    # This fixes the "2D lock" issue, allowing the chain to wrap objects in 3D space.
    links.new(join_geom.outputs['Geometry'], merge_dist.inputs['Geometry'])
    links.new(merge_dist.outputs['Geometry'], convex_hull.inputs['Geometry'])
    # Hull Cleanup Links
    links.new(convex_hull.outputs['Convex Hull'], delete_bottom.inputs['Geometry'])
    links.new(normal_node.outputs['Normal'], sep_z.inputs['Vector'])
    links.new(sep_z.outputs['Z'], compare_z.inputs['A'])
    links.new(compare_z.outputs['Result'], delete_bottom.inputs['Selection'])
    # Filter for boundary edges
    links.new(delete_bottom.outputs['Geometry'], delete_geom.inputs['Geometry'])
    links.new(edge_neighbors.outputs['Face Count'], compare_edges.inputs['A'])
    links.new(compare_edges.outputs['Result'], delete_geom.inputs['Selection'])
    links.new(delete_geom.outputs['Geometry'], mesh_to_curve.inputs['Mesh'])
    links.new(mesh_to_curve.outputs['Curve'], set_tilt.inputs['Curve'])
    links.new(set_tilt.outputs['Curve'], set_cyclic.inputs['Geometry'])
    links.new(set_cyclic.outputs['Geometry'], resample_curve.inputs['Curve'])
    links.new(group_input.outputs["Resolution"], resample_curve.inputs['Length'])
    links.new(resample_curve.outputs['Curve'], set_radius.inputs['Curve'])
    # Switch Logic Links
    links.new(col_info.outputs['Instances'], col_size.inputs['Geometry'])
    links.new(col_size.outputs['Instance Count'], col_has_items.inputs['A'])
    links.new(set_radius.outputs['Curve'], hull_size.inputs['Geometry'])
    links.new(hull_size.outputs['Point Count'], hull_valid.inputs['A'])
    links.new(col_has_items.outputs['Result'], logic_and.inputs[0])
    links.new(hull_valid.outputs['Result'], logic_and.inputs[1])
    links.new(logic_and.outputs['Boolean'], switch.inputs['Switch'])
    links.new(set_radius.outputs['Curve'], switch.inputs['True'])
    links.new(group_input.outputs["Geometry"], switch.inputs['False'])
    links.new(switch.outputs['Output'], group_output.inputs['Geometry'])
    mod_name = f"{MOD_PREFIX}Native_Wrap"
    mod = path_obj.modifiers.get(mod_name)
    if not mod:
        mod = path_obj.modifiers.new(name=mod_name, type='NODES')
    mod.node_group = gn_group
    # Ensure it's at the top of the stack to process the curve before the chain generator
    with bpy.context.temp_override(object=path_obj):
        bpy.ops.object.modifier_move_to_index(modifier=mod_name, index=0)
    if hasattr(path_obj.lsd_pg_mech_props, "chain_wrap_collection"):
        wrap_socket = gn_group.interface.items_tree.get("Wrap Collection")
        if wrap_socket:
            mod[wrap_socket.identifier] = path_obj.lsd_pg_mech_props.chain_wrap_collection
    # ADDED: Connect Resolution driver
    res_socket = gn_group.interface.items_tree.get("Resolution")
    if res_socket:
        create_driver(path_obj, '["lsd_native_chain_res"]', mod.name, res_socket.identifier)
def setup_native_chain_gn(path_obj: bpy.types.Object, link_obj: bpy.types.Object) -> None:
    """
    Creates and configures a dynamic, procedural Geometry Nodes setup for a rigid
    chain or belt.
    This function is responsible for two main tasks:
    1.  Creating a unique, independent Geometry Nodes group for this specific
        chain object. This is crucial to prevent any shared data issues when
        multiple chains are in the scene.
    2.  Adding a Geometry Nodes modifier to the `path_obj` (a curve) and
        configuring it to instance the `link_obj` (a mesh) along the curve.
    AI Editor Note:
    This function is another example of the addon's native-first philosophy.
    The setup is fully native and robust. Key parameters like link pitch and
    animation are controlled by native custom properties on the `path_obj`, which
    are connected to the node group via drivers. This ensures the chain continues
    to function perfectly even if the addon is disabled or removed. The instanced
    link object is kept separate and hidden, and the main curve object serves as
    the single point of control for the user.
    Args:
        path_obj: The curve object that defines the chain's path. This object
                  will receive the Geometry Nodes modifier.
        link_obj: The mesh object representing a single link to be instanced.
    """
    # --- UNIQUE NODE GROUP ---
    # Create a unique name for the node group based on the path object's name.
    # This ensures that each chain has its own independent node group.
    gn_group_name = f"LSD_Native_{path_obj.name}_GN"
    gn_group = bpy.data.node_groups.get(gn_group_name)
    if not gn_group:
        gn_group = bpy.data.node_groups.new(name=gn_group_name, type='GeometryNodeTree')
    # AI Editor Note: Force clear the node tree to ensure logic updates are applied to existing chains.
    gn_group.nodes.clear()
    gn_group.interface.clear()
    # --- 1. Define Node Group Interface (Inputs & Outputs) ---
    iface = gn_group.interface
    iface.new_socket(name="Geometry", in_out="INPUT", socket_type='NodeSocketGeometry')
    link_obj_socket = iface.new_socket(name="Link Object", in_out="INPUT", socket_type='NodeSocketObject')
    link_len_socket = iface.new_socket(name="Link Length", in_out="INPUT", socket_type='NodeSocketFloat')
    link_len_socket.default_value = 0.2
    link_len_socket.min_value = 0.01
    anim_socket = iface.new_socket(name="Animation Offset", in_out="INPUT", socket_type='NodeSocketFloat')
    
    # NEW: Belt & Custom Hardware Sockets
    is_belt_socket = iface.new_socket(name="Is Belt", in_out="INPUT", socket_type='NodeSocketBool')
    belt_w_socket = iface.new_socket(name="Belt Width", in_out="INPUT", socket_type='NodeSocketFloat')
    belt_w_socket.default_value = 0.02
    belt_t_socket = iface.new_socket(name="Belt Thickness", in_out="INPUT", socket_type='NodeSocketFloat')
    belt_t_socket.default_value = 0.005
    
    use_custom_roller_socket = iface.new_socket(name="Use Custom Roller", in_out="INPUT", socket_type='NodeSocketBool')
    custom_roller_socket = iface.new_socket(name="Custom Roller", in_out="INPUT", socket_type='NodeSocketObject')
    use_custom_conn_socket = iface.new_socket(name="Use Custom Connector", in_out="INPUT", socket_type='NodeSocketBool')
    custom_conn_socket = iface.new_socket(name="Custom Connector", in_out="INPUT", socket_type='NodeSocketObject')
    
    # Colors
    roller_col_socket = iface.new_socket(name="Roller Color", in_out="INPUT", socket_type='NodeSocketColor')
    conn_col_socket = iface.new_socket(name="Connector Color", in_out="INPUT", socket_type='NodeSocketColor')
    
    iface.new_socket(name="Geometry", in_out="OUTPUT", socket_type='NodeSocketGeometry')
    # --- 2. Create Core Nodes ---
    nodes = gn_group.nodes
    links = gn_group.links
    group_input = nodes.new('NodeGroupInput')
    group_input.location = (-1400, 0)
    group_output = nodes.new('NodeGroupOutput')
    group_output.location = (800, 0)
    # --- 3. Node Logic: Generate Base Points ---
    # We generate points on the *original* curve to get the correct count and spacing.
    # We will then override their positions to animate them.
    curve_to_points = nodes.new('GeometryNodeCurveToPoints')
    curve_to_points.location = (-1200, 100)
    curve_to_points.mode = 'LENGTH'
    # --- 4. Node Logic: Calculate Animated Position (Rolling) ---
    # Formula: TargetLength = (Index * Pitch + AnimOffset) % TotalLength
    # Get Index and Pitch
    index_node = nodes.new('GeometryNodeInputIndex')
    index_node.location = (-1200, -100)
    math_base_len = nodes.new('ShaderNodeMath')
    math_base_len.operation = 'MULTIPLY'
    math_base_len.location = (-1000, -100)
    # Add Animation Offset
    math_add_anim = nodes.new('ShaderNodeMath')
    math_add_anim.operation = 'ADD'
    math_add_anim.location = (-800, -100)
    # Get Total Curve Length
    curve_length = nodes.new('GeometryNodeCurveLength')
    curve_length.location = (-1200, -300)
    # Wrap around the curve (Modulo)
    math_mod = nodes.new('ShaderNodeMath')
    math_mod.operation = 'FLOORED_MODULO' # Handles negative animation correctly
    math_mod.location = (-600, -100)
    # --- 5. Node Logic: Sample Curve at New Position ---
    # This gives us the Position, Tangent, and Normal at the animated location.
    sample_curve = nodes.new('GeometryNodeSampleCurve')
    sample_curve.location = (-400, 0)
    sample_curve.mode = 'LENGTH'
    # Set the new position of the points
    set_position = nodes.new('GeometryNodeSetPosition')
    set_position.location = (-200, 100)
    # Calculate Rotation from Tangent
    align_euler = nodes.new('FunctionNodeAlignEulerToVector')
    align_euler.location = (-200, -100)
    align_euler.axis = 'X'
    align_euler.pivot_axis = 'AUTO'
    # --- 6. Node Logic: Instance Links ---
    link_info = nodes.new('GeometryNodeObjectInfo')
    link_info.location = (-200, -300)
    link_info.transform_space = 'ORIGINAL'
    instance_on_points = nodes.new('GeometryNodeInstanceOnPoints')
    instance_on_points.location = (200, 0)
    # --- 7. Node Logic: Radius Scaling (Optional but recommended) ---
    # We need to sample the radius at the *new* position.
    # Since Sample Curve doesn't output radius, we sample the nearest point on the original curve
    # to the new position, then get the radius from there.
    sample_nearest = nodes.new('GeometryNodeSampleNearest')
    sample_nearest.location = (0, -400)
    sample_index = nodes.new('GeometryNodeSampleIndex')
    sample_index.location = (200, -400)
    sample_index.data_type = 'FLOAT'
    radius_attr = nodes.new('GeometryNodeInputNamedAttribute')
    radius_attr.location = (0, -550)
    radius_attr.data_type = 'FLOAT'
    radius_attr.inputs['Name'].default_value = "radius"
    # --- 8. Link Everything ---
    # Base Points
    links.new(group_input.outputs['Geometry'], curve_to_points.inputs['Curve'])
    links.new(group_input.outputs['Link Length'], curve_to_points.inputs['Length'])
    
    # Calculation Chain
    links.new(index_node.outputs['Index'], math_base_len.inputs[0])
    links.new(group_input.outputs['Link Length'], math_base_len.inputs[1])
    links.new(math_base_len.outputs['Value'], math_add_anim.inputs[0])
    links.new(group_input.outputs['Animation Offset'], math_add_anim.inputs[1])
    links.new(group_input.outputs['Geometry'], curve_length.inputs['Curve'])
    links.new(math_add_anim.outputs['Value'], math_mod.inputs[0])
    links.new(curve_length.outputs['Length'], math_mod.inputs[1])
    
    # Sampling
    # AI Editor Note: Handle socket naming changes in Blender 4.0+ (Sample Curve uses 'Curves' input)
    links.new(group_input.outputs['Geometry'], sample_curve.inputs.get('Curves') or sample_curve.inputs.get('Curve'))
    links.new(math_mod.outputs['Value'], sample_curve.inputs['Length'])
    
    # Set Position & Rotation
    links.new(curve_to_points.outputs['Points'], set_position.inputs['Geometry'])
    links.new(sample_curve.outputs['Position'], set_position.inputs['Position'])
    links.new(sample_curve.outputs['Tangent'], align_euler.inputs['Vector'])
    
    # --- 9. Node Logic: Split Logic (Belt vs Chain) ---
    switch_geom = nodes.new('GeometryNodeSwitch')
    switch_geom.input_type = 'GEOMETRY'
    switch_geom.location = (600, 0)
    
    # Path A: Chain (Instancing - Dual Phase)
    # Phase 1: Connectors (Links)
    instance_links = nodes.new('GeometryNodeInstanceOnPoints')
    instance_links.location = (200, 100)
    links.new(set_position.outputs['Geometry'], instance_links.inputs['Points'])
    links.new(align_euler.outputs['Rotation'], instance_links.inputs['Rotation'])
    
    # Selection for Link vs Custom
    switch_conn = nodes.new('GeometryNodeSwitch')
    switch_conn.input_type = 'OBJECT'
    switch_conn.location = (0, 200)
    links.new(group_input.outputs['Link Object'], switch_conn.inputs['False'])
    links.new(group_input.outputs['Custom Connector'], switch_conn.inputs['True'])
    links.new(group_input.outputs['Use Custom Connector'], switch_conn.inputs['Switch'])
    
    conn_info = nodes.new('GeometryNodeObjectInfo')
    conn_info.location = (50, 200)
    links.new(switch_conn.outputs['Output'], conn_info.inputs['Object'])
    links.new(conn_info.outputs['Geometry'], instance_links.inputs['Instance'])
    
    # Phase 2: Rollers
    instance_rollers = nodes.new('GeometryNodeInstanceOnPoints')
    instance_rollers.location = (200, 300)
    links.new(set_position.outputs['Geometry'], instance_rollers.inputs['Points'])
    
    switch_roller = nodes.new('GeometryNodeSwitch')
    switch_roller.input_type = 'OBJECT'
    switch_roller.location = (0, 400)
    links.new(group_input.outputs['Custom Roller'], switch_roller.inputs['True'])
    links.new(group_input.outputs['Use Custom Roller'], switch_roller.inputs['Switch'])
    
    roller_info = nodes.new('GeometryNodeObjectInfo')
    roller_info.location = (50, 400)
    links.new(switch_roller.outputs['Output'], roller_info.inputs['Object'])
    links.new(roller_info.outputs['Geometry'], instance_rollers.inputs['Instance'])
    
    join_chain = nodes.new('GeometryNodeJoinGeometry')
    join_chain.location = (400, 100)
    links.new(instance_links.outputs['Instances'], join_chain.inputs['Geometry'])
    links.new(instance_rollers.outputs['Instances'], join_chain.inputs['Geometry'])
    
    # --- Path B: Belt (Curve to Mesh) ---
    curve_to_mesh = nodes.new('GeometryNodeCurveToMesh')
    curve_to_mesh.location = (400, -200)
    
    # ATOMIC FIX: Use proper identifier for Quadrilateral (Curve Primitive)
    try:
        profile_rect = nodes.new('GeometryNodeCurvePrimitiveQuadrilateral')
    except Exception:
        profile_rect = nodes.new('GeometryNodeMeshQuadrilateral') # Legacy guess
    
    profile_rect.location = (200, -300)
    links.new(profile_rect.outputs.get('Curve') or profile_rect.outputs.get('Mesh'), curve_to_mesh.inputs['Profile Curve'])
    links.new(group_input.outputs['Belt Width'], profile_rect.inputs['Width'])
    links.new(group_input.outputs['Belt Thickness'], profile_rect.inputs['Height'])
    
    # Animated points to curve for smooth belt
    try:
        points_to_curve = nodes.new('GeometryNodePointsToCurves')
    except Exception:
        points_to_curve = nodes.new('GeometryNodePointsToCurve')
    
    points_to_curve.location = (200, -100)
    links.new(set_position.outputs['Geometry'], points_to_curve.inputs['Points'])
    
    # Socket fallback for Curves vs Curve
    p_to_c_out = points_to_curve.outputs.get('Curves') or points_to_curve.outputs.get('Curve')
    c_to_m_in = curve_to_mesh.inputs.get('Curve') or curve_to_mesh.inputs.get('Curves')
    links.new(p_to_c_out, c_to_m_in)
    
    # Final Switching
    links.new(group_input.outputs['Is Belt'], switch_geom.inputs['Switch'])
    links.new(join_chain.outputs['Geometry'], switch_geom.inputs['False'])
    links.new(curve_to_mesh.outputs['Mesh'], switch_geom.inputs['True'])
    
    links.new(switch_geom.outputs['Output'], group_output.inputs['Geometry'])
    
    # --- Add/Update the Modifier ---
    mod_name = f"{MOD_PREFIX}Native_{path_obj.lsd_pg_mech_props.type_chain.capitalize()}Chain"
    mod = path_obj.modifiers.get(mod_name)
    if not mod:
        mod = path_obj.modifiers.new(name=mod_name, type='NODES')
    mod.node_group = gn_group
    
    # --- Connect Modifier Inputs ---
    iface = gn_group.interface
    mod[iface.items_tree.get("Link Object").identifier] = link_obj
    mod[iface.items_tree.get("Is Belt").identifier] = (path_obj.lsd_pg_mech_props.type_chain == 'BELT')
    
    # Standard Drivers
    create_driver(path_obj, '["lsd_native_chain_pitch"]', mod.name, iface.items_tree.get("Link Length").identifier)
    create_driver(path_obj, '["lsd_native_anim_offset"]', mod.name, iface.items_tree.get("Animation Offset").identifier, var_name="rotation")
    
    # Belt properties drivers
    create_driver(path_obj, 'lsd_pg_mech_props["belt_width"]', mod.name, iface.items_tree.get("Belt Width").identifier)
    create_driver(path_obj, 'lsd_pg_mech_props["belt_thickness"]', mod.name, iface.items_tree.get("Belt Thickness").identifier)
    
    # Custom hardware toggles and objects
    mod[iface.items_tree.get("Use Custom Roller").identifier] = path_obj.lsd_pg_mech_props.chain_use_custom_roller
    mod[iface.items_tree.get("Custom Roller").identifier] = path_obj.lsd_pg_mech_props.chain_custom_roller_obj
    mod[iface.items_tree.get("Use Custom Connector").identifier] = path_obj.lsd_pg_mech_props.chain_use_custom_connector
    mod[iface.items_tree.get("Custom Connector").identifier] = path_obj.lsd_pg_mech_props.chain_custom_connector_obj
    
    # Final Material/Color pass (using drivers for live updates)
    create_driver(path_obj, 'lsd_pg_mech_props["chain_roller_color"]', mod.name, iface.items_tree.get("Roller Color").identifier)
    create_driver(path_obj, 'lsd_pg_mech_props["chain_connector_color"]', mod.name, iface.items_tree.get("Connector Color").identifier)
def update_chain_driver_settings(self: 'LSD_PG_Mech_Props', context: bpy.types.Context) -> None:
    """
    Updates the chain driver expression based on radius, ratio, and invert settings.
    AI Editor Note: This allows for real-time manual adjustment of the drive system
    without re-running the link operator.
    """
    obj = self.id_data
    if not obj or self.category != 'CHAIN':
        return
    # Check if driver exists on the native animation offset property
    if not obj.animation_data or not obj.animation_data.drivers:
        return
    fcurve = obj.animation_data.drivers.find('["lsd_native_anim_offset"]')
    if not fcurve:
        return
    driver = fcurve.driver
    # Calculate final multiplier components
    radius = self.chain_drive_radius
    ratio = self.chain_drive_ratio
    invert = -1.0 if self.chain_drive_invert else 1.0
    # Update expression
    # We assume the variable 'rotation' exists as created by the Link operator.
    # Formula: offset = rotation * radius * manual_ratio * direction
    driver.expression = f"rotation * {radius:.4f} * {ratio:.4f} * {invert:.1f}"
def update_dimensions_for_object(obj: bpy.types.Object) -> None:
    """Updates a single dimension object (label or arrow) based on its local properties."""
    if not obj or not obj.get("lsd_is_dimension"):
        return
    mod = obj.modifiers.get("Dynamic_Dimension")
    # If it has a GN modifier, update the GN parameters
    if mod and mod.node_group:
        # UPGRADE CHECK: Ensure GN modifier has all required sockets for new features
        if hasattr(mod.node_group, "interface"):
            # ... socket checks ...
            pass
        unit_display = getattr(obj, "lsd_dim_unit_display", 'SCENE')
        gn_scale, gn_suffix = get_dimension_unit_settings(bpy.context.scene, unit_display)
        scale_id = None
        suffix_id = None
        font_id = None
        text_size_id = None
        if hasattr(mod.node_group, "interface"):
            for item in mod.node_group.interface.items_tree:
                if item.name == "Scale":
                    scale_id = item.identifier
                elif item.name == "Suffix":
                    suffix_id = item.identifier
                elif item.name == "Font":
                    font_id = item.identifier
                elif item.name == "Text Size":
                    text_size_id = item.identifier
        if scale_id: mod[scale_id] = gn_scale
        if suffix_id: mod[suffix_id] = gn_suffix
        if text_size_id: mod[text_size_id] = obj.lsd_pg_dim_props.text_scale
        if font_id:
            # Re-read props from obj
            from . import core
            f_name = getattr(obj.lsd_pg_dim_props, "font_name", 'DEFAULT')
            f_bold = getattr(obj.lsd_pg_dim_props, "font_bold", False)
            f_italic = getattr(obj.lsd_pg_dim_props, "font_italic", False)
            f_data = core.get_font_data(f_name, f_bold, f_italic)
            if f_data: mod[font_id] = f_data
        # Force Viewport Update
        obj.update_tag()
    # Update color / materials
    from . import operators
    mat = operators.get_or_create_text_material(obj)
    if obj.data and hasattr(obj.data, "materials"):
        if not obj.data.materials:
            obj.data.materials.append(mat)
        else:
            obj.data.materials[0] = mat
    obj.update_tag()
def get_dimension_unit_settings(scene, unit_display):
    """Helper to convert unit enum to scale and suffix."""
    unit_settings = scene.unit_settings
    unit_sys = unit_settings.system
    u_type = unit_settings.length_unit
    scale_length = unit_settings.scale_length
    if unit_display == 'MM':
        return 1000.0, "mm"
    elif unit_display == 'CM':
        return 100.0, "cm"
    elif unit_display == 'IMPERIAL':
        return 3.28084, "ft"
    elif unit_display == 'METRIC':
        return 1.0, "m"
    # Fallback to SCENE units
    gn_scale = 1.0
    gn_suffix = "m"
    if unit_sys == 'METRIC':
        if u_type == 'MILLIMETERS': gn_scale = 1000.0; gn_suffix = "mm"
        elif u_type == 'CENTIMETERS': gn_scale = 100.0; gn_suffix = "cm"
        elif u_type == 'KILOMETERS': gn_scale = 0.001; gn_suffix = "km"
        gn_scale *= scale_length
    elif unit_sys == 'IMPERIAL':
        if u_type == 'FEET': gn_scale = 3.28084; gn_suffix = "ft"
        elif u_type == 'INCHES': gn_scale = 39.3701; gn_suffix = "in"
        gn_scale *= scale_length
    return gn_scale, gn_suffix
def update_dimensions(scene: bpy.types.Scene) -> None:
    """Updates all URDF dimension objects."""
    for obj in scene.objects:
        if obj.get("lsd_is_dimension"):
            update_dimensions_for_object(obj)
@persistent

def dimension_update_handler(scene: bpy.types.Scene, depsgraph: bpy.types.Depsgraph) -> None:
    """Handler to update dimensions when units change."""
    if not scene: return
    unit_settings = scene.unit_settings
    # AI Editor Note: Include scale_length in key to detect unit scale changes
    current_unit_key = f"{unit_settings.system}_{unit_settings.length_unit}_{unit_settings.scale_length}"
    if scene.get("lsd_last_unit_key") != current_unit_key:
        update_dimensions(scene)
        scene["lsd_last_unit_key"] = current_unit_key
# ------------------------------------------------------------------------

#   PART 1.2: AI GENERATION LOGIC (LOCAL)

# ------------------------------------------------------------------------

def get_part_catalog_prompt() -> str:
    """
    Generates a system prompt describing the available parametric parts.
    This is used to inform the Cloud AI about the tools it can use.
    """
    catalog = []
    catalog.append("You are a robot design assistant for Blender.")
    catalog.append("Your goal is to generate a robot configuration in JSON format.")
    catalog.append("Prefer using the following available parametric parts over generic primitives:")
    # List categories and types
    catalog.append("- GEAR: " + ", ".join([t[0] for t in GEAR_TYPES]))
    catalog.append("- WHEEL: " + ", ".join([t[0] for t in WHEEL_TYPES]))
    catalog.append("- ELECTRONICS: " + ", ".join([t[0] for t in ALL_ELECTRONICS_TYPES]))
    catalog.append("- SPRING: " + ", ".join([t[0] for t in SPRING_TYPES]))
    catalog.append("- FASTENER: " + ", ".join([t[0] for t in FASTENER_TYPES]))
    catalog.append("\nOutput Format (JSON):")
    catalog.append("{ 'type': 'ROVER', 'components': [ { 'category': 'WHEEL', 'type': 'WHEEL_OFFROAD', 'location': [x,y,z], ... } ] }")
    return "\n".join(catalog)
def create_parametric_part_object(context: bpy.types.Context, category: str, type_sub: str, location: mathutils.Vector, scale_factor: float = 1.0, **kwargs) -> bpy.types.Object:
    """
    Programmatically creates a parametric part object with the specified properties.
    This function centralizes the creation logic, ensuring consistency between
    UI operators and AI generation.
    """
    # AI Editor Note: Ensure Object Mode and clean selection to prevent context errors
    # with operators like transform_apply or when creating objects.
    if context.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    bpy.ops.object.select_all(action='DESELECT')
    coll_name = "Mechanical_Parts"
    coll = bpy.data.collections.get(coll_name)
    if not coll:
        coll = bpy.data.collections.new(coll_name)
        context.scene.collection.children.link(coll)
    # Generate unique name
    base_name = type_sub.replace('_', ' ').title()
    name = get_unique_name(base_name)
    new_obj = None
    # --- 1. Create Object based on Category ---
    if category == 'CHAIN':
        new_obj = create_parametric_chain(context, type_sub)
    elif category == 'SPRING':
        # Springs are typically represented by a mesh object
        mesh = bpy.data.meshes.new(name)
        new_obj = bpy.data.objects.new(name, mesh)
        coll.objects.link(new_obj)
        # AI Editor Note: Initial length of spring should match size cage
        props = new_obj.lsd_pg_mech_props
        props.length = scale_factor
        # Helper empties for positioning
        e_start = bpy.data.objects.new(name=f"Start_{new_obj.name}", object_data=None)
        e_start.location = new_obj.location
        context.collection.objects.link(e_start)
        props.spring_start_obj = e_start
        e_end = bpy.data.objects.new(name=f"End_{new_obj.name}", object_data=None)
        e_end.location = new_obj.location + mathutils.Vector((0, 0, scale_factor))
        context.collection.objects.link(e_end)
        props.spring_end_obj = e_end
        if type_sub == 'SPRING':
            setup_native_spring(new_obj, e_start, e_end)
        elif type_sub == 'DAMPER':
            # Initialize damper specific properties
            props.height = scale_factor
            props.damper_seat_radius = 0.08 * scale_factor
            props.damper_seat_thickness = 0.003 * scale_factor
            setup_native_damper(new_obj, e_start, e_end)
        elif type_sub == 'SPRING_SLINKY':
            # Initialize slinky specific properties
            props.radius = 0.05 * scale_factor
            props.tooth_depth = 0.002 * scale_factor # Wire radius
            props.teeth = 20
            setup_native_slinky(new_obj, e_start, e_end)
    elif category == 'ROPE':
        # Rope creation logic (simplified from operator)
        mesh = bpy.data.meshes.new(f"{name}_Mesh")
        new_obj = bpy.data.objects.new(name, mesh)
        context.collection.objects.link(new_obj)
        bm = bmesh.new()
        seg_count = 64
        # SIZE CAGE FIX: Ensure initial length matches the cage scale
        rope_len = scale_factor
        for i in range(seg_count + 1):
            # Create segments along the -Z axis
            z = (i / seg_count) * rope_len
            bm.verts.new((0, 0, -z))
        bm.verts.ensure_lookup_table()
        for i in range(seg_count):
            bm.edges.new((bm.verts[i], bm.verts[i+1]))
        bm.to_mesh(mesh)
        bm.free()
        # Initialize properties
        new_obj.lsd_pg_mech_props.is_part = True
        new_obj.lsd_pg_mech_props.category = 'ROPE'
        # length property in props should stay in sync
        new_obj.lsd_pg_mech_props.length = rope_len
    # --- 2. Create Standard BMesh Objects if needed ---
    if not new_obj:
        mesh = bpy.data.meshes.new(name)
        new_obj = bpy.data.objects.new(name, mesh)
        coll.objects.link(new_obj)
    new_obj.location = location
    # AI Editor Note: Initial Setup for Stators/Sub-objects (Basic Joints)
    # The separation of concerns mandate requires distinct objects for stationary components.
    props = new_obj.lsd_pg_mech_props
    if category == 'BASIC_JOINT':
        # Every basic joint needs a stator (body/bracket)
        stator_mesh = bpy.data.meshes.new(f"{name}_Stator_Mesh")
        stator_obj = bpy.data.objects.new(f"{name}_Stator", stator_mesh)
        coll.objects.link(stator_obj)
        stator_obj.matrix_world = new_obj.matrix_world.copy()
        stator_obj.parent = new_obj
        props.joint_stator_obj = stator_obj
        # Prismatic joints also need a dedicated screw shaft
        if type_sub == 'JOINT_PRISMATIC':
             screw_mesh = bpy.data.meshes.new(f"{name}_Screw_Mesh")
             screw_obj = bpy.data.objects.new(f"{name}_Screw", screw_mesh)
             coll.objects.link(screw_obj)
             screw_obj.matrix_world = new_obj.matrix_world.copy()
             screw_obj.parent = new_obj
             props.joint_screw_obj = screw_obj
    # AI Editor Note: Force update to ensure matrix_world is correct for subsequent operations (like rigging).
    # This prevents parts from being rigged at the world origin if the dependency graph hasn't caught up.
    context.view_layer.update()
    # --- 3. Set Final Properties (Synced with Generator logic) ---
    props.is_part = True
    props.category = category
    # Set type property dynamically based on category
    type_prop_map = {
        'GEAR': 'type_gear', 'RACK': 'type_rack', 'FASTENER': 'type_fastener',
        'SPRING': 'type_spring', 'CHAIN': 'type_chain', 'WHEEL': 'type_wheel',
        'PULLEY': 'type_pulley', 'ROPE': 'type_rope', 'BASIC_JOINT': 'type_basic_joint', 'BASIC_SHAPE': 'type_basic_shape',
        'ELECTRONICS': 'type_electronics',
        'ARCHITECTURAL': 'type_architectural',
        'VEHICLE': 'type_vehicle'
    }
    if category in type_prop_map:
        setattr(props, type_prop_map[category], type_sub)
    # --- 3. Apply Defaults & Scaling ---
    # Apply realistic defaults first, then scale
    if category == 'ARCHITECTURAL':
        # Default Architectural: Scale relative to actual base size (Length 5.0m)
        multiplier = scale_factor / 5.0
        props.length = 5.0 * multiplier; props.height = 2.5 * multiplier
        props.wall_thickness = 0.2 * multiplier
        if type_sub == 'COLUMN': props.radius = 0.2 * multiplier
        elif type_sub == 'STAIRS':
            props.step_count = 12; props.step_height = (2.5 * multiplier) / 12
            props.step_depth = 0.28 * multiplier
        elif type_sub == 'WINDOW' or type_sub == 'DOOR':
            props.window_frame_thickness = 0.05 * multiplier
            props.glass_thickness = 0.01 * multiplier
    elif category == 'VEHICLE':
        # Realistic scale mapping (units in meters) proportional to scale_factor (Length)
        l = scale_factor
        if type_sub == 'CAR':
            props.vehicle_length = l; props.vehicle_width = l * 0.4; props.vehicle_height = l * 0.31
            props.vehicle_wheel_radius = props.vehicle_height * 0.25; props.vehicle_wheel_width = props.vehicle_width * 0.15
            props.vehicle_wheelbase = l * 0.6; props.vehicle_track_width = props.vehicle_width * 0.8
        elif type_sub == 'TRUCK':
            props.vehicle_length = l; props.vehicle_width = l * 0.4; props.vehicle_height = l * 0.51
            props.vehicle_wheel_radius = props.vehicle_height * 0.15; props.vehicle_wheel_width = props.vehicle_width * 0.12
            props.vehicle_wheelbase = l * 0.7; props.vehicle_track_width = props.vehicle_width * 0.85
        elif type_sub == 'DRONE':
            props.vehicle_length = l; props.vehicle_width = l; props.vehicle_height = l * 0.33
            props.vehicle_wheel_radius = l * 0.25; props.vehicle_wheel_width = l * 0.05
        elif type_sub == 'TANK':
            props.vehicle_length = l; props.vehicle_width = l * 0.39; props.vehicle_height = l * 0.28
            props.vehicle_wheel_radius = props.vehicle_height * 0.4; props.vehicle_wheel_width = props.vehicle_width * 0.25
        elif type_sub == 'FORKLIFT':
            props.vehicle_length = l; props.vehicle_width = l * 0.34; props.vehicle_height = l * 0.71
    elif category == 'FASTENER':
        # AI Editor Note: Scale relative to actual base size (0.02m) to maximize cage.
        # Default Fastener: Length 0.02, Radius 0.003. Max dim is 0.02.
        multiplier = scale_factor / 0.02
        props.radius = 0.003 * multiplier; props.length = 0.02 * multiplier
    elif category == 'ELECTRONICS':
        # Electronics defaults (simplified logic from operator)
        # AI Editor Note: Scale relative to actual base size to maximize cage.
        if 'MOTOR' in type_sub:
            # Max dim 0.06 (Body 0.04 + Shaft/Tabs ~0.02)
            multiplier = scale_factor / 0.06
            props.radius = 0.015 * multiplier; props.length = 0.04 * multiplier
            # AI Editor Note: Specific defaults for BLDC Outrunner Shaft per user request
            if type_sub == 'MOTOR_BLDC_OUTRUNNER':
                # Normalize to default scale (0.1) to ensure 2.2mm/8.5mm at base scale
                scale_ratio = scale_factor / 0.1
                props.motor_shaft_radius = 0.0022 * scale_ratio
                props.motor_shaft_length = 0.0085 * scale_ratio
            # AI Editor Note: Specific defaults for Pancake Motor
            if type_sub == 'MOTOR_PANCAKE':
                props.radius = 0.04 * multiplier # Wide
                props.length = 0.012 * multiplier # Short (Thin)
                props.motor_shaft_length = 0.008 * multiplier # Short shaft
                props.motor_shaft_radius = 0.004 * multiplier
        elif 'SENSOR' in type_sub:
            # Max dim 0.06
            multiplier = scale_factor / 0.06
            props.radius = 0.03 * multiplier; props.length = 0.04 * multiplier
        elif 'PCB' in type_sub:
            # Max dim 0.06 (Board 0.05 + Connectors)
            multiplier = scale_factor / 0.06
            props.radius = 0.025 * multiplier; props.length = 0.05 * multiplier
        else:
            # Max dim 0.02
            multiplier = scale_factor / 0.02
            props.radius = 0.01 * multiplier; props.length = 0.02 * multiplier
    elif category == 'GEAR':
        # Default Gear: Radius 0.05. Diameter 0.1.
        multiplier = scale_factor / 0.1
        props.gear_radius = 0.05 * multiplier
        props.gear_width = 0.02 * multiplier
        props.gear_tooth_depth = 0.005 * multiplier
        props.gear_bore_radius = 0.01 * multiplier
    elif category == 'RACK':
        # Default Rack: Length 0.2.
        multiplier = scale_factor / 0.2
        props.rack_length = scale_factor # Direct mapping to cage size
        props.rack_width = 0.02 * multiplier
        props.rack_height = 0.02 * multiplier
        props.rack_tooth_depth = 0.005 * multiplier
    elif category == 'WHEEL':
        # Default Wheel: Radius 0.05. Diameter 0.1.
        multiplier = scale_factor / 0.1
        props.wheel_radius = 0.05 * multiplier
        props.wheel_width = 0.04 * multiplier
        props.wheel_hub_radius = 0.012 * multiplier
        props.wheel_hub_length = 0.02 * multiplier
    elif category == 'PULLEY':
        # Default Pulley: Radius 0.03. Diameter 0.06.
        multiplier = scale_factor / 0.06
        props.pulley_radius = 0.03 * multiplier
        props.pulley_width = 0.02 * multiplier
        props.pulley_groove_depth = 0.005 * multiplier
    elif category == 'BASIC_JOINT':
        # AI Editor Note: Precision Scaling for Basic Joints.
        # Ensure the 'span' correctly represents the physical total length of the default parts along the major axis (Z).
        # 1. Determine base dimensions from procedural generation defaults
        # JOINT_CONTINUOUS: body (0.12) + shaft (0.02) = 0.14
        # JOINT_REVOLUTE: frame length (0.08) + eye radius (0.03) = 0.11
        # JOINT_PRISMATIC: screw length (0.73)
        base_h = 0.14 if type_sub == 'JOINT_CONTINUOUS' else 0.11
        if type_sub in ['JOINT_PRISMATIC_WHEELS', 'JOINT_PRISMATIC_WHEELS_ROT']:
            base_h = 0.2 # Standard Rack Length
        elif type_sub == 'JOINT_PRISMATIC':
            base_h = 0.73 # Screw Length
        multiplier = scale_factor / base_h
        # 2. Apply Proportional Scaling (Explicitly set instead of *= to prevent cumulative error)
        # Default proportions normalized to their respective base_h
        props.joint_radius = 0.03 * multiplier
        props.joint_width = 0.08 * multiplier
        props.joint_pin_radius = 0.007 * multiplier
        props.joint_pin_length = 0.06 * multiplier
        props.joint_sub_size = 0.001 * multiplier
        props.joint_sub_thickness = 0.001 * multiplier
        props.joint_frame_width = 0.06 * multiplier
        props.joint_frame_length = 0.08 * multiplier
        # Continuous-Specific Proportions (Now using the new engineering defaults)
        props.joint_base_radius = 0.06 * multiplier
        props.joint_base_length = 0.12 * multiplier
        props.joint_motor_shaft_radius = 0.01 * multiplier
        props.joint_motor_shaft_length = 0.02 * multiplier
        # Rotor Arm proportions (Screenshot 4 Defaults)
        props.rotor_arm_length = 0.194 * multiplier
        props.rotor_arm_width = 0.001 * multiplier
        props.rotor_arm_height = 0.001 * multiplier
        # 3. Categorized Overrides for specific span requirements
        if type_sub == 'JOINT_REVOLUTE':
            props.joint_pin_length = props.joint_width * 1.5
        elif type_sub in ['JOINT_PRISMATIC_WHEELS', 'JOINT_PRISMATIC_WHEELS_ROT']:
            props.joint_radius = 0.012 * multiplier # Wheel Radius
            props.joint_sub_thickness = 0.01 * multiplier # Rack Thickness
            props.rack_width = 0.04 * multiplier
            props.rack_length = scale_factor # Matches the cage directly
            props.joint_sub_size = 0.08 * multiplier # Carriage Length
    elif category == 'BASIC_SHAPE':
        # Default Basic Shape: Normalized to scale_factor (default 0.1m / 100mm)
        props.shape_size = scale_factor
        props.shape_length_x = scale_factor
        props.shape_width_y = scale_factor
        props.shape_height_z = scale_factor
        props.shape_radius = scale_factor / 2.0
        props.shape_height = scale_factor
        props.shape_major_radius = scale_factor * 0.4
        props.shape_tube_radius = scale_factor * 0.1
        # Fallback props for legacy mesh calculation
        props.radius = scale_factor / 2.0
        props.length = scale_factor
        props.height = scale_factor
    elif category == 'ARCHITECTURAL':
        props.length = scale_factor
        props.height = scale_factor
        props.width = scale_factor / 5.0
        props.radius = scale_factor / 10.0
        props.wall_thickness = 0.2 * scale_factor
        props.window_frame_thickness = 0.05 * scale_factor
        props.glass_thickness = 0.01 * scale_factor
        props.step_count = 10
        props.step_height = scale_factor / 10.0
        props.step_depth = scale_factor / 8.0
    elif category == 'CHAIN':
        # AI Editor Note: For chains, scale the object itself to fit the cage.
        # Default curve diameter is 0.4.
        unit_scale = context.scene.unit_settings.scale_length
        s = 1.0 / unit_scale if unit_scale > 0 else 1.0
        target_scale = (scale_factor / 0.4) * s
        new_obj.scale = (target_scale, target_scale, target_scale)
        # Do not scale properties to maintain proportions relative to the scaled curve.
        pass
    elif category == 'ROPE':
        unit_scale = context.scene.unit_settings.scale_length
        s = 1.0 / unit_scale if unit_scale > 0 else 1.0
        # Default construction: 6-strand steel wire or 20-strand synthetic braid
        if type_sub == 'ROPE_STEEL':
            props.teeth = 6 # Traditional 6-strand wire rope
            props.twist = math.radians(200.0) # 200 deg
        elif type_sub == 'ROPE_SYNTHETIC':
            props.teeth = 20 # As per user screenshot
            props.twist = math.radians(200.0) # As per user screenshot
        multiplier = scale_factor / 0.1
        props.radius = 0.01 * multiplier # Total radius
        # Initialize Object ID properties for drivers
        new_obj["rope_radius"] = props.radius * s
        new_obj["rope_strands"] = props.teeth
        new_obj["rope_twist"] = props.twist
        new_obj["rope_tube_mode"] = (type_sub == 'ROPE_TUBE')
        new_obj["rope_is_synthetic"] = (type_sub == 'ROPE_SYNTHETIC')
    # --- 4. Apply User Overrides (kwargs) ---
    for k, v in kwargs.items():
        if hasattr(props, k):
            setattr(props, k, v)
    # --- 5. Create Helpers (Springs/Fasteners/Ropes) ---
    if category == 'FASTENER':
        # Create Head Cutter
        head_mesh = bpy.data.meshes.new(f"{CUTTER_PREFIX}{new_obj.name}_Head_Mesh")
        head_cutter = bpy.data.objects.new(f"{CUTTER_PREFIX}{new_obj.name}_Head", head_mesh)
        coll.objects.link(head_cutter); head_cutter.parent = new_obj
        head_cutter.display_type = 'WIRE'; head_cutter.hide_set(True); head_cutter.hide_render = True
        mod = new_obj.modifiers.new(name=f"{BOOL_PREFIX}Head", type='BOOLEAN')
        mod.operation = 'UNION'; mod.solver = 'EXACT'; mod.object = head_cutter
        # Create Nut Cutter
        nut_mesh = bpy.data.meshes.new(f"{CUTTER_PREFIX}{new_obj.name}_Nut_Mesh")
        nut_cutter = bpy.data.objects.new(f"{CUTTER_PREFIX}{new_obj.name}_Nut", nut_mesh)
        coll.objects.link(nut_cutter); nut_cutter.parent = new_obj
        nut_cutter.display_type = 'WIRE'; nut_cutter.hide_set(True); nut_cutter.hide_render = True
        mod = new_obj.modifiers.new(name=f"{BOOL_PREFIX}Nut", type='BOOLEAN')
        mod.operation = 'UNION'; mod.solver = 'EXACT'; mod.object = nut_cutter
    elif category == 'SPRING':
        # SIZE CAGE FIX: Ensure initial length matches the cage scale
        props.length = scale_factor
        # --- AI Editor Note: Unit-Aware Display Size ---
        unit_scale = context.scene.unit_settings.scale_length
        s = 1.0 / unit_scale if unit_scale > 0 else 1.0
        # Create Empties
        s_empty = bpy.data.objects.new(f"Spring_Start_{new_obj.name}", None)
        e_empty = bpy.data.objects.new(f"Spring_End_{new_obj.name}", None)
        coll.objects.link(s_empty); coll.objects.link(e_empty)
        s_empty.location = location
        e_empty.location = location + mathutils.Vector((0, 0, props.length))
        props.spring_start_obj = s_empty; props.spring_end_obj = e_empty
        s_empty.empty_display_size = 0.2 * scale_factor * s
        e_empty.empty_display_size = 0.2 * scale_factor * s
        if type_sub == 'DAMPER':
            # Housing & Piston (combined)
            props.height = 0.5 * scale_factor
            props.radius = 0.08 * scale_factor # Housing Radius
            props.tooth_depth = 0.015 * scale_factor # Rod Radius
            props.teeth = 9 # Piston Segments
            props.outer_radius = 0.06 * scale_factor # Thicker Housing
            props.bore_radius = 0.03 * scale_factor # Rod
            props.damper_seat_radius = 0.1 * scale_factor
            props.damper_seat_thickness = 0.03 * scale_factor
        elif type_sub == 'SPRING':
            props.spring_radius = 0.2 * scale_factor
            props.spring_wire_thickness = 0.03 * scale_factor
            props.spring_turns = 10
        # Setup Driver
        # AI Editor Note: The original code was trying to drive a non-existent custom property '["spring_length"]'.
        # The correct target is the 'length' property within the object's URDF property group.
        # This ensures the data model is kept in sync with the dynamic length of the spring.
        fcurve = new_obj.driver_add('lsd_pg_mech_props.length')
        driver = fcurve.driver; driver.type = 'AVERAGE'
        var = driver.variables.new(); var.name = "dist"; var.type = 'LOC_DIFF'
        var.targets[0].id = s_empty; var.targets[1].id = e_empty
        driver.expression = "dist"
        if type_sub == 'SPRING': setup_native_spring(new_obj, s_empty, e_empty)
        elif type_sub == 'DAMPER': setup_native_damper(new_obj, s_empty, e_empty)
    elif category == 'CHAIN':
        # AI Editor Note: Ensure the Geometry Nodes modifier is set up for the chain.
        setup_native_chain_gn(new_obj, props.instanced_link_obj)
    elif category == 'ROPE':
        # Setup rope hooks/physics (simplified)
        setup_native_rope_gn(new_obj)
    # --- 6. Final Regeneration ---
    from .generators import regenerate_mech_mesh
    regenerate_mech_mesh(new_obj, context)
    # AI Editor Note: Ensure the new object is active and selected for immediate operations.
    context.view_layer.objects.active = new_obj
    new_obj.select_set(True)
    # AI Editor Note: Force update again to ensure any sub-objects created during regeneration (like stators)
    # have valid matrices before being used in rigging or other operations.
    context.view_layer.update()
    return new_obj
def _calculate_bone_geometry(objs, axis_orient: str, reference_obj=None):
    """
    Calculates the head, tail, roll vector, and radius for a bone based on
    one or more mesh objects.
    Supports a single object or a list/tuple/set of objects. When multiple
    objects are given the bounding boxes are merged before computing the bone.
    """
    if not isinstance(objs, (list, tuple, set)):
        objs = [objs]
    if not reference_obj:
        reference_obj = objs[0]
    mat = reference_obj.matrix_world
    inv_mat = mat.inverted()
    # Build a combined bounding box in the reference object's local space
    all_points_local = []
    depsgraph = bpy.context.evaluated_depsgraph_get()
    for o in objs:
        o_eval = o.evaluated_get(depsgraph) if o.type == 'MESH' else o
        o_mat = o.matrix_world
        if hasattr(o_eval, 'bound_box') and o_eval.bound_box:
            for b in o_eval.bound_box:
                v_world = o_mat @ mathutils.Vector(b)
                all_points_local.append(inv_mat @ v_world)
        else:
            all_points_local.append(inv_mat @ o_mat.translation)
    if not all_points_local:
        return (mat.translation,
                mat.translation + mathutils.Vector((0, 0.1, 0)),
                mathutils.Vector((0, 0, 1)), 0.1)
    head_world = mat.translation
    tail_world: mathutils.Vector
    roll_vec_world: mathutils.Vector
    if axis_orient == 'AUTO':
        y_vec_local = mathutils.Vector((0, 0, 1.0))
        z_vec_local = mathutils.Vector((1.0, 0, 0))
        max_dist = max((p.dot(y_vec_local) for p in all_points_local), default=0.1)
        if max_dist < 0.001:
            max_dist = 0.1
        tail_local = y_vec_local * max_dist
        initial_tail_world = mat @ tail_local
        initial_roll_vec_world = mat.to_3x3() @ z_vec_local
        y_vec_world = (initial_tail_world - head_world).normalized()
        z_vec_world = initial_roll_vec_world.normalized()
        x_vec_world = y_vec_world.cross(z_vec_world).normalized()
        orient_mat = mathutils.Matrix((x_vec_world, y_vec_world, z_vec_world)).transposed()
        new_y_vec_world = orient_mat.col[1]
        new_z_vec_world = orient_mat.col[2]
        bone_length = (initial_tail_world - head_world).length
        tail_world = head_world + bone_length * new_y_vec_world
        roll_vec_world = new_z_vec_world
    else:
        is_neg = axis_orient.startswith("-")
        axis_char = axis_orient.replace("-", "")
        if axis_char == 'X':
            y_vec_local = mathutils.Vector((1, 0, 0))
            roll_vec_local = mathutils.Vector((0, 0, 1))
        elif axis_char == 'Y':
            y_vec_local = mathutils.Vector((0, 1, 0))
            roll_vec_local = mathutils.Vector((1, 0, 0))
        else:  # 'Z'
            y_vec_local = mathutils.Vector((0, 0, 1))
            roll_vec_local = mathutils.Vector((0, 1, 0))
        if is_neg:
            y_vec_local *= -1.0
        obj_rot_mat = mat.to_3x3()
        y_vec_world = (obj_rot_mat @ y_vec_local).normalized()
        roll_vec_world = (obj_rot_mat @ roll_vec_local).normalized()
        origin_proj = head_world.dot(y_vec_world)
        projections = [(mat @ p).dot(y_vec_world) for p in all_points_local]
        max_extent = max(p - origin_proj for p in projections)
        if max_extent < 0.001:
            length = max(projections) - min(projections)
        else:
            length = max_extent
        if length < 0.01:
            length = 0.1
        tail_world = head_world + length * y_vec_world
    # Radius calculation (common for all modes)
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
        max_radius = 0.5
    # --- AI Editor Note: Return radius in physical Meters ---
    # Blender Units (BU) must be normalized by the scene scale to get meters.
    unit_scale = bpy.context.scene.unit_settings.scale_length
    normalized_radius = max_radius * unit_scale if unit_scale > 0 else max_radius
    return head_world, tail_world, roll_vec_world, normalized_radius
def rig_parametric_joint(context: bpy.types.Context, obj: bpy.types.Object) -> Tuple[str, str]:
    """
    Automatically rigs a parametric BASIC_JOINT object.
    Creates base and joint bones, parents the stator and rotor, and sets constraints.
    Returns the names of the created (base_bone, joint_bone).
    """
    props = obj.lsd_pg_mech_props
    if props.category != 'BASIC_JOINT': return None, None
    rig = ensure_default_rig(context)
    if not rig: return None, None
    # 1. Calculate bone geometry from the rotor object
    head, tail, roll, radius = _calculate_bone_geometry(obj, context.scene.lsd_bone_axis)
    unit_scale = context.scene.unit_settings.scale_length
    s = 1.0 / unit_scale if unit_scale > 0 else 1.0
    # --- AI Editor Note: Ensure radius is always in physical Meters (Normalized) ---
    # _calculate_bone_geometry already returns normalized meters.
    if props.type_basic_joint == 'JOINT_CONTINUOUS':
        # Use the explicit joint_radius property from the part as ground truth for motors.
        radius = props.joint_radius
    # Radius must be > 0
    if radius < 0.001: radius = 0.05
    # 2. Create bones in Edit Mode
    if context.mode != 'OBJECT': bpy.ops.object.mode_set(mode='OBJECT')
    bpy.ops.object.select_all(action='DESELECT')
    context.view_layer.objects.active = rig
    rig.select_set(True)
    bpy.ops.object.mode_set(mode='EDIT')
    rig_mat_inv = rig.matrix_world.inverted()
    # Create base bone
    base_bone_name = f"Bone_{obj.name.replace('.', '_')}_base"
    eb_base = rig.data.edit_bones.new(base_bone_name)
    if props.type_basic_joint == 'JOINT_PRISMATIC':
        screw_len = props.length
        screw_rad = props.radius
        block_h = screw_rad * 3.0
        base_head_local = mathutils.Vector((0, 0, -screw_len/2 - block_h/2))
        base_tail_local = mathutils.Vector((0, 0, -screw_len/2))
        base_head_world = obj.matrix_world @ base_head_local
        base_tail_world = obj.matrix_world @ base_tail_local
        eb_base.head = rig_mat_inv @ base_head_world
        eb_base.tail = rig_mat_inv @ base_tail_world
        eb_base.align_roll(rig_mat_inv.to_3x3() @ roll)
    else:
        # Offset must be scaled by 's' to be visible in mm scenes
        offset_vec_world = (tail - head).normalized() * -0.05 * s
        eb_base.head = rig_mat_inv @ (head + offset_vec_world)
        eb_base.tail = rig_mat_inv @ head
        eb_base.align_roll(rig_mat_inv.to_3x3() @ roll)
    # Create joint bone
    joint_bone_name = f"Bone_{obj.name.replace('.', '_')}"
    eb_joint = rig.data.edit_bones.new(joint_bone_name)
    eb_joint.head = rig_mat_inv @ head
    eb_joint.tail = rig_mat_inv @ tail
    eb_joint.align_roll(rig_mat_inv.to_3x3() @ roll)
    eb_joint.parent = eb_base
    # Create screw bone (for Prismatic joints)
    screw_bone_name = f"Bone_{obj.name.replace('.', '_')}_screw"
    if props.type_basic_joint == 'JOINT_PRISMATIC':
        eb_screw = rig.data.edit_bones.new(screw_bone_name)
        eb_screw.head = eb_joint.head
        eb_screw.tail = eb_joint.tail
        eb_screw.roll = eb_joint.roll
        eb_screw.parent = eb_base
    # 3. Configure bones and parent meshes in Pose Mode
    bpy.ops.object.mode_set(mode='POSE')
    # Configure base bone and parent stator
    pbone_base = rig.pose.bones.get(base_bone_name)
    if pbone_base:
        stator_obj = props.joint_stator_obj
        if stator_obj:
            original_matrix = stator_obj.matrix_world.copy()
            stator_obj.parent = rig
            stator_obj.parent_type = 'BONE'
            stator_obj.parent_bone = pbone_base.name
            stator_obj.matrix_world = original_matrix
        pbone_base.lsd_pg_kinematic_props.joint_type = 'base'
        update_single_bone_gizmo(pbone_base, context.scene.lsd_viz_gizmos)
        apply_native_constraints(pbone_base)
    # Configure screw bone (Prismatic only)
    if props.type_basic_joint == 'JOINT_PRISMATIC':
        pbone_screw = rig.pose.bones.get(screw_bone_name)
        if pbone_screw:
            pbone_screw.lsd_pg_kinematic_props.joint_type = 'continuous'
            pbone_screw.lsd_pg_kinematic_props.axis_alignment = 'Z'
            pbone_screw.lsd_pg_kinematic_props.joint_radius = radius
            update_single_bone_gizmo(pbone_screw, context.scene.lsd_viz_gizmos)
            apply_native_constraints(pbone_screw)
            screw_obj = props.joint_screw_obj
            if screw_obj:
                original_matrix = screw_obj.matrix_world.copy()
                screw_obj.parent = rig
                screw_obj.parent_type = 'BONE'
                screw_obj.parent_bone = screw_bone_name
                screw_obj.matrix_world = original_matrix
    # Configure pin object (Revolute only)
    if props.type_basic_joint == 'JOINT_REVOLUTE':
        pin_obj = props.joint_pin_obj
        if pin_obj:
            # Parent pin to the joint bone so it rotates with the rotor
            original_matrix = pin_obj.matrix_world.copy()
            pin_obj.parent = rig
            pin_obj.parent_type = 'BONE'
            pin_obj.parent_bone = joint_bone_name
            pin_obj.matrix_world = original_matrix
            apply_auto_smooth(pin_obj)
    # Configure joint bone and parent rotor
    pbone_joint = rig.pose.bones.get(joint_bone_name)
    if pbone_joint:
        original_matrix = obj.matrix_world.copy()
        obj.parent = rig
        obj.parent_type = 'BONE'
        obj.parent_bone = joint_bone_name
        obj.matrix_world = original_matrix
        joint_type_map = {
            'JOINT_REVOLUTE': 'revolute',
            'JOINT_CONTINUOUS': 'continuous',
            'JOINT_PRISMATIC': 'prismatic',
            'JOINT_PRISMATIC_WHEELS': 'prismatic',
            'JOINT_PRISMATIC_WHEELS_ROT': 'prismatic',
            'JOINT_SPHERICAL': 'base',
        }
        pbone_joint.lsd_pg_kinematic_props.joint_type = joint_type_map.get(props.type_basic_joint, 'fixed')
        pbone_joint.lsd_pg_kinematic_props.axis_alignment = 'Z'
        pbone_joint.lsd_pg_kinematic_props.joint_radius = radius
        # AI Editor Note: Set default limits for Revolute joints as requested (-115 to 115)
        if props.type_basic_joint == 'JOINT_REVOLUTE':
            pbone_joint.lsd_pg_kinematic_props.lower_limit = -115.0
            pbone_joint.lsd_pg_kinematic_props.upper_limit = 115.0
        update_single_bone_gizmo(pbone_joint, context.scene.lsd_viz_gizmos)
        apply_native_constraints(pbone_joint)
        if props.type_basic_joint == 'JOINT_PRISMATIC':
            # AI Editor Note: Calculate ratio based on screw radius to ensure it scales with the size cage.
            # Base ratio 0.015 corresponds to base radius 0.005 (Ratio = 3 * Radius).
            calc_ratio = props.radius * 3.0
            add_native_driver_relation(pbone_joint, screw_bone_name, ratio=calc_ratio, invert=False)
            if not any(m.target_bone == screw_bone_name for m in pbone_joint.lsd_pg_kinematic_props.mimic_drivers):
                mimic_entry = pbone_joint.lsd_pg_kinematic_props.mimic_drivers.add()
                mimic_entry.target_bone = screw_bone_name
                mimic_entry.ratio = calc_ratio
        rig.data.bones.active = pbone_joint.bone
    return base_bone_name, joint_bone_name
def _build_procedural_drone(context: bpy.types.Context, config: Dict[str, Any], scale_factor: float):
    """Procedurally builds a quadcopter drone."""
    rig = context.scene.lsd_active_rig
    cursor_loc = context.scene.cursor.location
    # Body
    body_rad = 0.1 * scale_factor
    body_height = 0.05 * scale_factor
    bpy.ops.mesh.primitive_cylinder_add(radius=body_rad, depth=body_height, location=cursor_loc + mathutils.Vector((0,0,body_height/2 + 0.1*scale_factor)))
    body = context.active_object
    body.name = get_unique_name("Drone_Body")
    context.view_layer.objects.active = body
    body.select_set(True)
    bpy.ops.lsd.add_bone()
    body_bone = f"Bone_{body.name.replace('.', '_')}"
    # Base joint
    pbone = rig.pose.bones.get(body_bone)
    if pbone:
        pbone.lsd_pg_kinematic_props.joint_type = 'base'
        update_single_bone_gizmo(pbone, True)
    # Arms & Motors
    num_arms = 4
    arm_len = 0.25 * scale_factor
    for i in range(num_arms):
        # AI Editor Note: Ensure Object Mode for primitive creation
        if context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.select_all(action='DESELECT')
        angle = (i / num_arms) * 2 * math.pi + (math.pi / 4) # X configuration
        x = math.cos(angle) * arm_len
        y = math.sin(angle) * arm_len
        # Arm Mesh (Simple tube)
        mid_x = x / 2
        mid_y = y / 2
        arm_rot = -angle
        bpy.ops.mesh.primitive_cylinder_add(radius=0.02*scale_factor, depth=arm_len, location=cursor_loc + mathutils.Vector((mid_x, mid_y, body_height/2 + 0.1*scale_factor)))
        arm = context.active_object
        arm.name = get_unique_name(f"Arm_{i+1}")
        arm.rotation_euler.z = angle
        arm.rotation_euler.x = math.pi/2
        bpy.ops.object.transform_apply(rotation=True, scale=True)
        # Parent arm to body (Fixed)
        arm.parent = rig
        arm.parent_type = 'BONE'
        arm.parent_bone = body_bone
        # Motor
        motor_pos = cursor_loc + mathutils.Vector((x, y, body_height/2 + 0.1*scale_factor + 0.02*scale_factor))
        motor = create_parametric_part_object(context, 'ELECTRONICS', 'MOTOR_BLDC_OUTRUNNER', motor_pos, scale_factor=scale_factor, radius=0.03*scale_factor, length=0.03*scale_factor)
        motor.name = get_unique_name(f"Motor_{i+1}")
        context.view_layer.objects.active = motor
        motor.select_set(True)
        bpy.ops.lsd.add_bone()
        motor_bone = f"Bone_{motor.name.replace('.', '_')}"
        # Parent motor bone to body bone
        context.view_layer.objects.active = rig
        rig.select_set(True)
        bpy.ops.object.mode_set(mode='EDIT', toggle=False)
        rig.data.edit_bones[motor_bone].parent = rig.data.edit_bones[body_bone]
        bpy.ops.object.mode_set(mode='POSE', toggle=False)
        pbone = rig.pose.bones.get(motor_bone)
        if pbone:
            pbone.lsd_pg_kinematic_props.joint_type = 'continuous'
            pbone.lsd_pg_kinematic_props.axis_alignment = 'Z'
            update_single_bone_gizmo(pbone, True)
            apply_native_constraints(pbone)
def _build_procedural_plane(context: bpy.types.Context, config: Dict[str, Any], scale_factor: float):
    """Procedurally builds a simple airplane."""
    rig = context.scene.lsd_active_rig
    cursor_loc = context.scene.cursor.location
    # Fuselage
    fuselage_len = 1.0 * scale_factor
    fuselage_rad = 0.15 * scale_factor
    bpy.ops.mesh.primitive_cylinder_add(radius=fuselage_rad, depth=fuselage_len, location=cursor_loc + mathutils.Vector((0,0,fuselage_rad + 0.2*scale_factor)))
    fuselage = context.active_object
    fuselage.name = get_unique_name("Fuselage")
    fuselage.rotation_euler.x = math.pi/2
    bpy.ops.object.transform_apply(rotation=True, scale=True)
    context.view_layer.objects.active = fuselage
    fuselage.select_set(True)
    bpy.ops.lsd.add_bone()
    base_bone = f"Bone_{fuselage.name.replace('.', '_')}"
    pbone = rig.pose.bones.get(base_bone)
    if pbone:
        pbone.lsd_pg_kinematic_props.joint_type = 'base'
        update_single_bone_gizmo(pbone, True)
    # Wings
    wing_span = 1.2 * scale_factor
    wing_chord = 0.3 * scale_factor
    wing_thick = 0.05 * scale_factor
    bpy.ops.mesh.primitive_cube_add(size=1, location=cursor_loc + mathutils.Vector((0, 0, fuselage_rad + 0.2*scale_factor)))
    wings = context.active_object
    wings.name = get_unique_name("Wings")
    wings.dimensions = (wing_span, wing_chord, wing_thick)
    bpy.ops.object.transform_apply(scale=True)
    wings.parent = rig
    wings.parent_type = 'BONE'
    wings.parent_bone = base_bone
    # Propeller (Front)
    prop_pos = cursor_loc + mathutils.Vector((0, -fuselage_len/2 - 0.05*scale_factor, fuselage_rad + 0.2*scale_factor))
    # Motor
    motor = create_parametric_part_object(context, 'ELECTRONICS', 'MOTOR_DC_ROUND', prop_pos, scale_factor=scale_factor, radius=0.05*scale_factor, length=0.05*scale_factor)
    motor.rotation_euler.x = math.pi/2
    bpy.ops.object.transform_apply(rotation=True)
    context.view_layer.objects.active = motor
    motor.select_set(True)
    bpy.ops.lsd.add_bone()
    motor_bone = f"Bone_{motor.name.replace('.', '_')}"
    context.view_layer.objects.active = rig
    rig.select_set(True)
    bpy.ops.object.mode_set(mode='EDIT', toggle=False)
    rig.data.edit_bones[motor_bone].parent = rig.data.edit_bones[base_bone]
    # Align motor bone to Y axis (forward)
    rig.data.edit_bones[motor_bone].tail = rig.data.edit_bones[motor_bone].head + mathutils.Vector((0, -0.1, 0))
    bpy.ops.object.mode_set(mode='POSE', toggle=False)
    pbone = rig.pose.bones.get(motor_bone)
    if pbone:
        pbone.lsd_pg_kinematic_props.joint_type = 'continuous'
        pbone.lsd_pg_kinematic_props.axis_alignment = '-Y'
        update_single_bone_gizmo(pbone, True)
        apply_native_constraints(pbone)
def _build_procedural_furniture(context: bpy.types.Context, config: Dict[str, Any], scale_factor: float):
    """Procedurally builds a closet/cabinet."""
    rig = context.scene.lsd_active_rig
    cursor_loc = context.scene.cursor.location
    # Cabinet Body
    width = 0.8 * scale_factor
    depth = 0.5 * scale_factor
    height = 1.8 * scale_factor
    bpy.ops.mesh.primitive_cube_add(size=1, location=cursor_loc + mathutils.Vector((0, 0, height/2)))
    cabinet = context.active_object
    cabinet.name = get_unique_name("Cabinet_Body")
    cabinet.dimensions = (width, depth, height)
    bpy.ops.object.transform_apply(scale=True)
    context.view_layer.objects.active = cabinet
    cabinet.select_set(True)
    bpy.ops.lsd.add_bone()
    base_bone = f"Bone_{cabinet.name.replace('.', '_')}"
    pbone = rig.pose.bones.get(base_bone)
    if pbone:
        pbone.lsd_pg_kinematic_props.joint_type = 'base'
        update_single_bone_gizmo(pbone, True)
    # Doors (Left and Right)
    door_w = width / 2
    door_thick = 0.02 * scale_factor
    for side in [-1, 1]: # Left (-1), Right (1)
        # AI Editor Note: Ensure Object Mode for primitive creation
        if context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.select_all(action='DESELECT')
        # Hinge position: Outer edge front
        hinge_x = side * (width/2)
        hinge_y = -depth/2
        # Door mesh
        # Origin at hinge
        bpy.ops.mesh.primitive_cube_add(size=1, location=cursor_loc + mathutils.Vector((hinge_x - (side * door_w/2), hinge_y - door_thick/2, height/2)))
        door = context.active_object
        door.name = get_unique_name(f"Door_{'L' if side<0 else 'R'}")
        door.dimensions = (door_w, door_thick, height)
        bpy.ops.object.transform_apply(scale=True)
        # Shift object origin to hinge side
        hinge_pos = cursor_loc + mathutils.Vector((hinge_x, hinge_y, height/2))
        cursor = context.scene.cursor
        saved_loc = cursor.location.copy()
        cursor.location = hinge_pos
        context.view_layer.objects.active = door
        door.select_set(True)
        bpy.ops.object.origin_set(type='ORIGIN_CURSOR')
        cursor.location = saved_loc
        bpy.ops.lsd.add_bone()
        door_bone = f"Bone_{door.name.replace('.', '_')}"
        # Parent to cabinet
        context.view_layer.objects.active = rig
        rig.select_set(True)
        bpy.ops.object.mode_set(mode='EDIT', toggle=False)
        rig.data.edit_bones[door_bone].parent = rig.data.edit_bones[base_bone]
        bpy.ops.object.mode_set(mode='POSE', toggle=False)
        pbone = rig.pose.bones.get(door_bone)
        if pbone:
            pbone.lsd_pg_kinematic_props.joint_type = 'revolute'
            pbone.lsd_pg_kinematic_props.axis_alignment = 'Z'
            pbone.lsd_pg_kinematic_props.lower_limit = 0 if side > 0 else -90
            pbone.lsd_pg_kinematic_props.upper_limit = 90 if side > 0 else 0
            update_single_bone_gizmo(pbone, True)
            apply_native_constraints(pbone)
def _build_procedural_conveyor(context: bpy.types.Context, config: Dict[str, Any], scale_factor: float):
    """Procedurally builds an escalator/conveyor."""
    rig = context.scene.lsd_active_rig
    cursor_loc = context.scene.cursor.location
    # Ramp/Base
    length = 3.0 * scale_factor
    width = 1.0 * scale_factor
    height = 1.5 * scale_factor
    bpy.ops.mesh.primitive_cube_add(size=1, location=cursor_loc + mathutils.Vector((length/2, 0, height/2)))
    ramp = context.active_object
    ramp.name = get_unique_name("Escalator_Base")
    ramp.dimensions = (math.sqrt(length**2 + height**2), width, 0.2*scale_factor)
    ramp.rotation_euler.y = -math.atan2(height, length)
    bpy.ops.object.transform_apply(scale=True, rotation=True)
    context.view_layer.objects.active = ramp
    ramp.select_set(True)
    bpy.ops.lsd.add_bone()
    base_bone = f"Bone_{ramp.name.replace('.', '_')}"
    pbone = rig.pose.bones.get(base_bone)
    if pbone:
        pbone.lsd_pg_kinematic_props.joint_type = 'base'
        update_single_bone_gizmo(pbone, True)
    # Steps
    step_depth = 0.3 * scale_factor
    step_height = 0.2 * scale_factor
    step_pos = cursor_loc + mathutils.Vector((0.5*scale_factor, 0, 0.5*scale_factor))
    bpy.ops.mesh.primitive_cube_add(size=1, location=step_pos)
    step = context.active_object
    step.name = get_unique_name("Step")
    step.dimensions = (step_depth, width*0.8, step_height)
    bpy.ops.object.transform_apply(scale=True)
    context.view_layer.objects.active = step
    step.select_set(True)
    bpy.ops.lsd.add_bone()
    step_bone = f"Bone_{step.name.replace('.', '_')}"
    # Parent to base
    context.view_layer.objects.active = rig
    rig.select_set(True)
    bpy.ops.object.mode_set(mode='EDIT', toggle=False)
    rig.data.edit_bones[step_bone].parent = rig.data.edit_bones[base_bone]
    # Align bone to ramp slope
    slope_vec = mathutils.Vector((length, 0, height)).normalized()
    rig.data.edit_bones[step_bone].tail = rig.data.edit_bones[step_bone].head + slope_vec * 0.2
    bpy.ops.object.mode_set(mode='POSE', toggle=False)
    pbone = rig.pose.bones.get(step_bone)
    if pbone:
        pbone.lsd_pg_kinematic_props.joint_type = 'prismatic'
        pbone.lsd_pg_kinematic_props.axis_alignment = 'Y' # Bone Y is length/direction
        pbone.lsd_pg_kinematic_props.lower_limit = 0.0
        pbone.lsd_pg_kinematic_props.upper_limit = math.sqrt(length**2 + height**2)
        update_single_bone_gizmo(pbone, True)
        apply_native_constraints(pbone)
def parse_natural_language_prompt(prompt: str) -> Dict[str, Any]:
    """
    Parses a natural language prompt into a structured robot configuration.
    This function uses keyword matching to interpret user intent for robot
    type, parameters, and components.
    """
    prompt = prompt.lower()
    config = {
        'type': 'UNKNOWN',
        'components': [],
        'params': {}
    }
    # --- Keyword Dictionaries for Robust Parsing ---
    # AI Editor Note: Expanded dictionaries to cover more synonyms and variations.
    # This makes the local AI more flexible and user-friendly for both local and API methods.
    TYPE_KEYWORDS = {
        'ROVER': ['rover', 'car', 'vehicle', 'buggy', 'mobile robot', 'ugv', 'ground vehicle', 'wheeled robot', 'bot'],
        'ARM': ['arm', 'manipulator', 'crane', 'robotic arm', 'robot arm'],
        'QUADRUPED': ['quadruped', 'spider', 'walker', 'dog', 'legged robot', 'dog bot', 'spot'],
        'DRONE': ['drone', 'quadcopter', 'uav', 'aerial', 'copter', 'multirotor', 'quadrotor', 'vtol'],
        'PLANE': ['plane', 'airplane', 'aircraft', 'glider', 'jet', 'fixed-wing'],
        'FURNITURE': ['closet', 'cabinet', 'wardrobe', 'shelf', 'furniture', 'cupboard', 'dresser', 'bookcase'],
        'CONVEYOR': ['escalator', 'conveyor', 'stairs', 'lift', 'conveyor belt', 'moving walkway'],
        'HUMANOID': ['humanoid', 'biped', 'human-like robot', 'android', 'human robot'],
        'BOX': ['box', 'cube', 'block', 'square'],
        'CYLINDER': ['cylinder', 'tube', 'pipe'],
        'SPHERE': ['sphere', 'ball', 'orb'],
    }
    COMPONENT_KEYWORDS = {
        'LIDAR': ['lidar', 'laser scanner', 'laser sensor'],
        'CAMERA': ['camera', 'vision sensor', 'webcam'],
        'MOTOR_SERVO': ['servo'],
        'MOTOR_STEPPER': ['stepper'],
        'MOTOR': ['motor', 'actuator', 'engine'],
        'ARM': ['arm', 'manipulator'], # For components on other robots
        'GEAR': ['gear', 'cog', 'sprocket', 'pinion'],
        'RACK': ['rack', 'linear gear'],
        'FASTENER': ['bolt', 'screw', 'nut', 'rivet', 'fastener'],
        'SPRING': ['spring', 'coil', 'shock'],
        'DAMPER': ['damper'],
        'CHAIN': ['chain', 'track'],
        'BELT': ['belt'],
        'PULLEY': ['pulley', 'sheave'],
        'ROPE': ['rope', 'cable', 'wire', 'string'],
        'WHEEL_MECANUM': ['mecanum'],
        'WHEEL_OMNI': ['omni'],
        'WHEEL_OFFROAD': ['offroad', 'off-road'],
        'WHEEL_CASTER': ['caster'],
        'WHEEL': ['wheel', 'tire', 'tyre'],
        'JOINT': ['joint', 'hinge', 'pivot'],
        'PCB': ['pcb', 'circuit board', 'breadboard', 'arduino', 'raspberry pi'],
        'IC': ['chip', 'ic', 'integrated circuit', 'resistor', 'capacitor', 'diode', 'led'],
    }
    # --- 1. Detect Robot Type ---
    for robot_type, keywords in TYPE_KEYWORDS.items():
        if any(keyword in prompt for keyword in keywords):
            config['type'] = robot_type
            break
    # --- 2. Detect Parameters ---
    # Wheel count
    wheel_match = re.search(r'(\d+)\s*[- ]?(wheel|wheeled)', prompt)
    if wheel_match:
        config['params']['wheels'] = int(wheel_match.group(1))
    elif 'tri' in prompt:
        config['params']['wheels'] = 3
    elif 'quad' in prompt and config['type'] == 'ROVER':
        config['params']['wheels'] = 4
    elif 'six' in prompt:
        config['params']['wheels'] = 6
    # Joint count for arms
    joint_match = re.search(r'(\d+)\s*[- ]?(dof|axis|joint|link|degree of freedom)', prompt)
    if joint_match:
        config['params']['joints'] = int(joint_match.group(1))
    # --- 3. Detect Components ---
    for component_type, keywords in COMPONENT_KEYWORDS.items():
        if component_type == 'ARM' and config['type'] == 'ARM': continue
        if any(keyword in prompt for keyword in keywords) and component_type not in config['components']:
            config['components'].append(component_type)
    if 'sensor' in prompt and not any(c in config['components'] for c in ['LIDAR', 'CAMERA']):
        config['components'].append('LIDAR')
    # --- 3.1 Cleanup Generics ---
    if 'MOTOR_SERVO' in config['components'] or 'MOTOR_STEPPER' in config['components']:
        if 'MOTOR' in config['components']: config['components'].remove('MOTOR')
    if any(k in config['components'] for k in ['WHEEL_MECANUM', 'WHEEL_OMNI', 'WHEEL_OFFROAD', 'WHEEL_CASTER']):
        if 'WHEEL' in config['components']: config['components'].remove('WHEEL')
    # --- 4. Apply Defaults and Fallbacks ---
    if config['type'] == 'ROVER' and 'wheels' not in config['params']:
        config['params']['wheels'] = 4
    if config['type'] == 'ARM' and 'joints' not in config['params']:
        config['params']['joints'] = 4 # Default arm length
    if config['type'] == 'UNKNOWN':
        if 'wheels' in config['params']: config['type'] = 'ROVER'
        elif 'joints' in config['params']: config['type'] = 'ARM'
        elif config['components']: config['type'] = 'PARTS_ONLY'
        else: config['type'] = 'ROVER' # Fallback
    return config
def _build_procedural_humanoid(context: bpy.types.Context, config: Dict[str, Any], scale_factor: float):
    """Procedurally builds a simple humanoid robot."""
    rig = context.scene.lsd_active_rig
    cursor_loc = context.scene.cursor.location
    # --- FOUNDATION: Torso ---
    torso_h = 0.5 * scale_factor
    torso_w = 0.3 * scale_factor
    torso_d = 0.2 * scale_factor
    # Raise humanoid to stand on the grid floor
    torso_pos = cursor_loc + mathutils.Vector((0, 0, torso_h / 2 + 0.8 * scale_factor))
    bpy.ops.mesh.primitive_cube_add(size=1, location=torso_pos)
    torso = context.active_object
    torso.name = get_unique_name("Torso")
    torso.dimensions = (torso_w, torso_d, torso_h)
    bpy.ops.object.transform_apply(scale=True)
    context.view_layer.objects.active = torso
    torso.select_set(True)
    bpy.ops.lsd.add_bone()
    torso_bone = f"Bone_{torso.name.replace('.', '_')}"
    pbone = rig.pose.bones.get(torso_bone)
    if pbone:
        pbone.lsd_pg_kinematic_props.joint_type = 'base'
        update_single_bone_gizmo(pbone, True)
    # --- ACTION: LEGS ---
    for side in [-1, 1]: # Left (-1), Right (1)
        side_str = "L" if side < 0 else "R"
        parent_bone_name = torso_bone
        # --- Hip Joint (Z-axis rotation) ---
        hip_pos = torso_pos + mathutils.Vector((side * torso_w / 4, 0, -torso_h / 2))
        hip_joint = create_parametric_part_object(context, 'BASIC_JOINT', 'JOINT_REVOLUTE', hip_pos, scale_factor=scale_factor*0.8, rotor_arm_length=0.05*scale_factor)
        hip_joint.name = get_unique_name(f"Hip_{side_str}")
        base_bone, joint_bone = rig_parametric_joint(context, hip_joint)
        context.view_layer.objects.active = rig; rig.select_set(True); bpy.ops.object.mode_set(mode='EDIT')
        rig.data.edit_bones[base_bone].parent = rig.data.edit_bones[parent_bone_name]
        bpy.ops.object.mode_set(mode='POSE')
        pbone = rig.pose.bones.get(joint_bone); pbone.lsd_pg_kinematic_props.axis_alignment = 'Z'
        parent_bone_name = joint_bone
        context.view_layer.update() # Update for next calculation
        current_pos = hip_joint.matrix_world @ mathutils.Vector((hip_joint.lsd_pg_mech_props.radius + hip_joint.lsd_pg_mech_props.rotor_arm_length, 0, 0))
        # --- Knee Joint (Y-axis rotation) ---
        knee_joint = create_parametric_part_object(context, 'BASIC_JOINT', 'JOINT_REVOLUTE', current_pos, scale_factor=scale_factor*0.7, rotor_arm_length=0.3*scale_factor)
        knee_joint.name = get_unique_name(f"Knee_{side_str}")
        knee_joint.rotation_euler.x = -math.pi / 2; bpy.ops.object.transform_apply(rotation=True)
        base_bone, joint_bone = rig_parametric_joint(context, knee_joint)
        context.view_layer.objects.active = rig; rig.select_set(True); bpy.ops.object.mode_set(mode='EDIT')
        rig.data.edit_bones[base_bone].parent = rig.data.edit_bones[parent_bone_name]
        bpy.ops.object.mode_set(mode='POSE')
        pbone = rig.pose.bones.get(joint_bone); pbone.lsd_pg_kinematic_props.axis_alignment = 'Y'
        parent_bone_name = joint_bone
        context.view_layer.update() # Update for next calculation
        current_pos = knee_joint.matrix_world @ mathutils.Vector((knee_joint.lsd_pg_mech_props.radius + knee_joint.lsd_pg_mech_props.rotor_arm_length, 0, 0))
        # --- Foot ---
        foot_pos = current_pos + mathutils.Vector((0.05*scale_factor, 0, 0))
        bpy.ops.mesh.primitive_cube_add(size=1, location=foot_pos)
        foot = context.active_object
        foot.name = get_unique_name(f"Foot_{side_str}")
        foot.dimensions = (0.15 * scale_factor, 0.1 * scale_factor, 0.04 * scale_factor)
        bpy.ops.object.transform_apply(scale=True)
        context.view_layer.objects.active = foot; foot.select_set(True); bpy.ops.lsd.add_bone()
        foot_bone = f"Bone_{foot.name.replace('.', '_')}"
        context.view_layer.objects.active = rig; rig.select_set(True); bpy.ops.object.mode_set(mode='EDIT')
        rig.data.edit_bones[foot_bone].parent = rig.data.edit_bones[parent_bone_name]
        bpy.ops.object.mode_set(mode='POSE')
        pbone = rig.pose.bones.get(foot_bone); pbone.lsd_pg_kinematic_props.joint_type = 'fixed'
    # --- ACTION: ARMS ---
    for side in [-1, 1]: # Left (-1), Right (1)
        side_str = "L" if side < 0 else "R"
        parent_bone_name = torso_bone
        # --- Shoulder Joint ---
        shoulder_pos = torso_pos + mathutils.Vector((side * (torso_w / 2 + 0.05 * scale_factor), 0, torso_h / 2 * 0.8))
        shoulder_joint = create_parametric_part_object(context, 'BASIC_JOINT', 'JOINT_REVOLUTE', shoulder_pos, scale_factor=scale_factor*0.6, rotor_arm_length=0.2*scale_factor)
        shoulder_joint.name = get_unique_name(f"Shoulder_{side_str}")
        shoulder_joint.rotation_euler.y = -side * math.pi / 2; bpy.ops.object.transform_apply(rotation=True)
        base_bone, joint_bone = rig_parametric_joint(context, shoulder_joint)
        context.view_layer.objects.active = rig; rig.select_set(True); bpy.ops.object.mode_set(mode='EDIT')
        rig.data.edit_bones[base_bone].parent = rig.data.edit_bones[parent_bone_name]
        bpy.ops.object.mode_set(mode='POSE')
        pbone = rig.pose.bones.get(joint_bone); pbone.lsd_pg_kinematic_props.axis_alignment = 'Y'
        parent_bone_name = joint_bone
        context.view_layer.update() # Update for next calculation
        current_pos = shoulder_joint.matrix_world @ mathutils.Vector((shoulder_joint.lsd_pg_mech_props.radius + shoulder_joint.lsd_pg_mech_props.rotor_arm_length, 0, 0))
        # --- Elbow Joint ---
        elbow_joint = create_parametric_part_object(context, 'BASIC_JOINT', 'JOINT_REVOLUTE', current_pos, scale_factor=scale_factor*0.5, rotor_arm_length=0.2*scale_factor)
        elbow_joint.name = get_unique_name(f"Elbow_{side_str}")
        elbow_joint.rotation_euler.y = -side * math.pi / 2; bpy.ops.object.transform_apply(rotation=True)
        base_bone, joint_bone = rig_parametric_joint(context, elbow_joint)
        context.view_layer.objects.active = rig; rig.select_set(True); bpy.ops.object.mode_set(mode='EDIT')
        rig.data.edit_bones[base_bone].parent = rig.data.edit_bones[parent_bone_name]
        bpy.ops.object.mode_set(mode='POSE')
        pbone = rig.pose.bones.get(joint_bone); pbone.lsd_pg_kinematic_props.axis_alignment = 'Y'
    # --- ACTION: HEAD ---
    unit_scale = context.scene.unit_settings.scale_length
    s = 1.0 / unit_scale if unit_scale > 0 else 1.0
    neck_pos = torso_pos + mathutils.Vector((0, 0, (torso_h / 2) * s))
    neck_joint = create_parametric_part_object(context, 'BASIC_JOINT', 'JOINT_REVOLUTE', neck_pos, scale_factor=scale_factor*0.5, rotor_arm_length=0.05*scale_factor)
    neck_joint.name = get_unique_name("Neck")
    base_bone, joint_bone = rig_parametric_joint(context, neck_joint)
    context.view_layer.objects.active = rig; rig.select_set(True); bpy.ops.object.mode_set(mode='EDIT')
    rig.data.edit_bones[base_bone].parent = rig.data.edit_bones[torso_bone]
    bpy.ops.object.mode_set(mode='POSE')
    pbone = rig.pose.bones.get(joint_bone); pbone.lsd_pg_kinematic_props.axis_alignment = 'Z'
    context.view_layer.update() # Update for head position
    head_pos = neck_joint.matrix_world @ mathutils.Vector((0, 0, (neck_joint.lsd_pg_mech_props.length / 2 + 0.1*scale_factor) * s))
    bpy.ops.mesh.primitive_uv_sphere_add(radius=0.12*scale_factor*s, location=head_pos)
    head = context.active_object; head.name = get_unique_name("Head")
    context.view_layer.objects.active = head; head.select_set(True); bpy.ops.lsd.add_bone()
    head_mesh_bone = f"Bone_{head.name.replace('.', '_')}"
    context.view_layer.objects.active = rig; rig.select_set(True); bpy.ops.object.mode_set(mode='EDIT')
    rig.data.edit_bones[head_mesh_bone].parent = rig.data.edit_bones[joint_bone]
    bpy.ops.object.mode_set(mode='POSE')
    pbone = rig.pose.bones.get(head_mesh_bone); pbone.lsd_pg_kinematic_props.joint_type = 'fixed'
def _build_simple_shape(context: bpy.types.Context, shape_type: str, scale_factor: float):
    """Generates a simple primitive shape with a base bone."""
    rig = context.scene.lsd_active_rig
    cursor_loc = context.scene.cursor.location
    unit_scale = context.scene.unit_settings.scale_length
    s = 1.0 / unit_scale if unit_scale > 0 else 1.0
    if shape_type == 'BOX':
        bpy.ops.mesh.primitive_cube_add(size=1.0 * scale_factor * s, location=cursor_loc)
    elif shape_type == 'CYLINDER':
        bpy.ops.mesh.primitive_cylinder_add(radius=0.5 * scale_factor * s, depth=1.0 * scale_factor * s, location=cursor_loc)
    elif shape_type == 'SPHERE':
        bpy.ops.mesh.primitive_uv_sphere_add(radius=0.5 * scale_factor * s, location=cursor_loc)
    obj = context.active_object
    obj.name = get_unique_name(shape_type.capitalize())
    bpy.ops.object.transform_apply(scale=True)
    context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.lsd.add_bone()
    bone_name = f"Bone_{obj.name.replace('.', '_')}"
    pbone = rig.pose.bones.get(bone_name)
    if pbone:
        pbone.lsd_pg_kinematic_props.joint_type = 'base'
        update_single_bone_gizmo(pbone, True)
def _build_procedural_parts(context: bpy.types.Context, config: Dict[str, Any], scale_factor: float):
    """Generates standalone components based on the prompt."""
    rig = context.scene.lsd_active_rig
    cursor_loc = context.scene.cursor.location
    offset = mathutils.Vector((0, 0, 0))
    spacing = 0.2 * scale_factor
    for comp in config['components']:
        pos = cursor_loc + offset
        obj = None
        if comp == 'LIDAR':
            obj = create_parametric_part_object(context, 'ELECTRONICS', 'SENSOR_LIDAR', pos, scale_factor=scale_factor, radius=0.03*scale_factor, length=0.04*scale_factor)
        elif comp == 'CAMERA':
            obj = create_parametric_part_object(context, 'ELECTRONICS', 'CAMERA_DEFAULT', pos, scale_factor=scale_factor, radius=0.015*scale_factor, length=0.03*scale_factor)
        elif comp == 'MOTOR_SERVO':
             obj = create_parametric_part_object(context, 'ELECTRONICS', 'MOTOR_SERVO_STD', pos, scale_factor=scale_factor, radius=0.01*scale_factor, length=0.04*scale_factor)
        elif comp == 'MOTOR_STEPPER':
             obj = create_parametric_part_object(context, 'ELECTRONICS', 'MOTOR_STEPPER_NEMA', pos, scale_factor=scale_factor, radius=0.021*scale_factor, length=0.04*scale_factor)
        elif comp == 'MOTOR':
             obj = create_parametric_part_object(context, 'ELECTRONICS', 'MOTOR_DC_ROUND', pos, scale_factor=scale_factor, radius=0.015*scale_factor, length=0.04*scale_factor)
        elif comp == 'GEAR':
             obj = create_parametric_part_object(context, 'GEAR', 'SPUR', pos, scale_factor=scale_factor, radius=0.05*scale_factor, length=0.01*scale_factor)
        elif comp == 'RACK':
             obj = create_parametric_part_object(context, 'RACK', 'RACK_SPUR', pos, scale_factor=scale_factor, radius=0.01*scale_factor, length=0.1*scale_factor)
        elif comp == 'FASTENER':
             obj = create_parametric_part_object(context, 'FASTENER', 'BOLT', pos, scale_factor=scale_factor, radius=0.005*scale_factor, length=0.02*scale_factor)
        elif comp == 'SPRING':
             obj = create_parametric_part_object(context, 'SPRING', 'SPRING', pos, scale_factor=scale_factor, radius=0.02*scale_factor, length=0.1*scale_factor)
        elif comp == 'DAMPER':
             obj = create_parametric_part_object(context, 'SPRING', 'DAMPER', pos, scale_factor=scale_factor, radius=0.02*scale_factor, length=0.1*scale_factor)
        elif comp == 'CHAIN':
             obj = create_parametric_part_object(context, 'CHAIN', 'ROLLER', pos, scale_factor=scale_factor, length=0.02*scale_factor)
        elif comp == 'BELT':
             obj = create_parametric_part_object(context, 'CHAIN', 'BELT', pos, scale_factor=scale_factor, length=0.02*scale_factor)
        elif comp == 'PULLEY':
             obj = create_parametric_part_object(context, 'PULLEY', 'PULLEY_FLAT', pos, scale_factor=scale_factor, radius=0.05*scale_factor, length=0.02*scale_factor)
        elif comp == 'ROPE':
             obj = create_parametric_part_object(context, 'ROPE', 'ROPE_STEEL', pos, scale_factor=scale_factor, radius=0.005*scale_factor, length=1.0*scale_factor)
        elif comp == 'WHEEL':
             obj = create_parametric_part_object(context, 'WHEEL', 'WHEEL_STANDARD', pos, scale_factor=scale_factor, radius=0.05*scale_factor, length=0.03*scale_factor)
        elif comp == 'WHEEL_MECANUM':
             obj = create_parametric_part_object(context, 'WHEEL', 'WHEEL_MECANUM', pos, scale_factor=scale_factor, radius=0.05*scale_factor, length=0.04*scale_factor)
        elif comp == 'WHEEL_OMNI':
             obj = create_parametric_part_object(context, 'WHEEL', 'WHEEL_OMNI', pos, scale_factor=scale_factor, radius=0.05*scale_factor, length=0.03*scale_factor)
        elif comp == 'WHEEL_OFFROAD':
             obj = create_parametric_part_object(context, 'WHEEL', 'WHEEL_OFFROAD', pos, scale_factor=scale_factor, radius=0.06*scale_factor, length=0.04*scale_factor)
        elif comp == 'WHEEL_CASTER':
             obj = create_parametric_part_object(context, 'WHEEL', 'WHEEL_CASTER', pos, scale_factor=scale_factor, radius=0.03*scale_factor, length=0.03*scale_factor)
        elif comp == 'JOINT':
             obj = create_parametric_part_object(context, 'BASIC_JOINT', 'JOINT_REVOLUTE', pos, scale_factor=scale_factor, radius=0.03*scale_factor, length=0.05*scale_factor)
        elif comp == 'PCB':
             obj = create_parametric_part_object(context, 'ELECTRONICS', 'PCB_BOARD', pos, scale_factor=scale_factor, radius=0.03*scale_factor, length=0.05*scale_factor)
        elif comp == 'IC':
             obj = create_parametric_part_object(context, 'ELECTRONICS', 'IC_MICROCHIP', pos, scale_factor=scale_factor, radius=0.01*scale_factor, length=0.02*scale_factor)
        if obj:
            # Auto-rig as base link so it can be moved
            context.view_layer.objects.active = obj
            obj.select_set(True)
            bpy.ops.lsd.add_bone()
            bone_name = f"Bone_{obj.name.replace('.', '_')}"
            pbone = rig.pose.bones.get(bone_name)
            if pbone:
                pbone.lsd_pg_kinematic_props.joint_type = 'base'
                update_single_bone_gizmo(pbone, True)
        offset.x += spacing
def _build_procedural_arm(context: bpy.types.Context, config: Dict[str, Any], scale_factor: float, base_obj: Optional[bpy.types.Object] = None, start_z: float = 0.0):
    """
    Procedurally builds a robotic arm.
    """
    rig = context.scene.lsd_active_rig
    cursor_loc = context.scene.cursor.location
    num_joints = config['params'].get('joints', 4)
    # Base position
    base_pos = cursor_loc.copy()
    if base_obj:
        # AI Editor Note: Use world translation to ensure correct connectivity even if parent has transforms
        base_pos = base_obj.matrix_world.translation.copy()
        base_pos.z += start_z
    parent_bone_name = None
    if base_obj:
        # Find the bone of the base object
        parent_bone_name = base_obj.parent_bone
    # Create Arm Base (if standalone)
    if not base_obj:
        unit_scale = context.scene.unit_settings.scale_length
        s = 1.0 / unit_scale if unit_scale > 0 else 1.0
        bpy.ops.mesh.primitive_cylinder_add(radius=0.08 * scale_factor * s, depth=0.05 * scale_factor * s, location=base_pos + mathutils.Vector((0,0,0.025*scale_factor*s)))
        base_link = context.active_object
        base_link.name = get_unique_name("Arm_Base")
        context.view_layer.objects.active = base_link
        base_link.select_set(True)
        bpy.ops.lsd.add_bone()
        parent_bone_name = f"Bone_{base_link.name.replace('.', '_')}"
        # Set base joint type
        pbone = rig.pose.bones.get(parent_bone_name)
        if pbone:
            pbone.lsd_pg_kinematic_props.joint_type = 'base'
            update_single_bone_gizmo(pbone, True)
        base_pos.z += 0.05 * scale_factor
    # Build Joints using Parametric Parts
    current_pos = base_pos.copy()
    # Start slightly above base
    current_pos.z += 0.02 * scale_factor
    for i in range(num_joints):
        # Determine joint type and orientation
        # Joint 1: Turret (Continuous, Z)
        # Joint 2+: Arm segments (Revolute, Y)
        joint_type = 'JOINT_REVOLUTE'
        if i == 0:
            joint_type = 'JOINT_CONTINUOUS'
        # Scale down joints progressively
        j_scale = scale_factor * (1.0 - (i * 0.1))
        if j_scale < 0.5 * scale_factor: j_scale = 0.5 * scale_factor
        # Create Parametric Joint
        # Note: We create it at current_pos.
        # For Revolute joints, we rotate them 90 deg on X to align axis to Y.
        joint_obj = create_parametric_part_object(
            context, 'BASIC_JOINT', joint_type, current_pos, scale_factor=j_scale,
            radius=0.04 * j_scale, length=0.06 * j_scale,
            rotor_arm_length=0.25 * scale_factor, # Length of the link
            rotor_arm_width=0.03 * j_scale,
            rotor_arm_height=0.03 * j_scale
        )
        joint_obj.name = get_unique_name(f"Joint_{i+1}")
        # Rotate Y-axis joints
        if i > 0:
            joint_obj.rotation_euler.x = math.pi / 2
            bpy.ops.object.transform_apply(rotation=True, scale=True)
        # Rig the joint
        base_bone, joint_bone = rig_parametric_joint(context, joint_obj)
        # Parent new joint's base to previous joint's moving part
        context.view_layer.objects.active = rig
        rig.select_set(True)
        bpy.ops.object.mode_set(mode='EDIT', toggle=False)
        if parent_bone_name:
            rig.data.edit_bones[base_bone].parent = rig.data.edit_bones[parent_bone_name]
        bpy.ops.object.mode_set(mode='POSE', toggle=False)
        # Configure Joint Limits/Axis
        pbone = rig.pose.bones.get(joint_bone)
        if pbone:
            if i == 0:
                pbone.lsd_pg_kinematic_props.joint_type = 'continuous' # Base rotation
                pbone.lsd_pg_kinematic_props.axis_alignment = 'Z'
            elif i % 2 == 1:
                pbone.lsd_pg_kinematic_props.joint_type = 'revolute'
                pbone.lsd_pg_kinematic_props.axis_alignment = 'Y'
            else:
                pbone.lsd_pg_kinematic_props.joint_type = 'revolute'
                pbone.lsd_pg_kinematic_props.axis_alignment = 'Y' # Keep Y for main lift joints usually
            update_single_bone_gizmo(pbone, True)
            apply_native_constraints(pbone)
        parent_bone_name = joint_bone
        # AI Editor Note: Force update to ensure matrix_world is correct after parenting/rigging.
        # This is critical for calculating the attachment point of the *next* joint in the chain.
        context.view_layer.update()
        # --- COGNITION: Calculate Next Joint Position ---
        # Determine the attachment point for the next link based on the current joint's geometry.
        # For continuous joints (turrets), stack vertically. For revolute arms, move along the arm length.
        if joint_type == 'JOINT_CONTINUOUS':
            # Stack on top: Z-offset = length/2 (center to top)
            current_pos = joint_obj.matrix_world @ mathutils.Vector((0, 0, joint_obj.lsd_pg_mech_props.length / 2.0))
        else:
            # Extend along arm: X-offset = radius + arm_length
            # Note: The joint object is rotated, so local X is the arm direction.
            arm_ext = joint_obj.lsd_pg_mech_props.radius + joint_obj.lsd_pg_mech_props.rotor_arm_length
            current_pos = joint_obj.matrix_world @ mathutils.Vector((arm_ext, 0, 0))
def _build_procedural_rover(context: bpy.types.Context, config: Dict[str, Any], scale_factor: float):
    """
    Procedurally builds a rover based on config.
    """
    rig = context.scene.lsd_active_rig
    cursor_loc = context.scene.cursor.location
    # --- COGNITION: Dimension Planning ---
    unit_scale = context.scene.unit_settings.scale_length
    s = 1.0 / unit_scale if unit_scale > 0 else 1.0
    num_wheels = config['params'].get('wheels', 4)
    # Chassis Dimensions (Normalized to Meters internally, so apply s for BU)
    chassis_len = (0.15 * num_wheels) * scale_factor * s
    if chassis_len < 0.4 * scale_factor * s: chassis_len = 0.4 * scale_factor * s
    chassis_width = 0.3 * scale_factor * s
    chassis_height = 0.1 * scale_factor * s
    # Calculate placement (Chassis bottom at clearance height)
    ground_clearance = 0.05 * scale_factor * s
    chassis_center_z = ground_clearance + (chassis_height / 2.0)
    chassis_pos = cursor_loc + mathutils.Vector((0, 0, chassis_center_z)) # chassis_center_z is already scaled
    # --- ACTION: Create Chassis ---
    bpy.ops.mesh.primitive_cube_add(size=1.0 * s, location=chassis_pos)
    chassis = context.active_object
    chassis.name = get_unique_name("Chassis")
    chassis.dimensions = (chassis_len, chassis_width, chassis_height)
    bpy.ops.object.transform_apply(scale=True)
    context.view_layer.objects.active = chassis
    chassis.select_set(True)
    bpy.ops.lsd.add_bone()
    chassis_bone = f"Bone_{chassis.name.replace('.', '_')}"
    # Set chassis as base
    pbone = rig.pose.bones.get(chassis_bone)
    if pbone:
        pbone.lsd_pg_kinematic_props.joint_type = 'base'
        update_single_bone_gizmo(pbone, True)
        apply_native_constraints(pbone)
    # --- ACTION: Create Wheels ---
    wheel_radius = 0.08 * scale_factor
    wheel_width = 0.04 * scale_factor
    # Calculate positions
    # Simple layout: Rows on left and right
    rows = math.ceil(num_wheels / 2)
    x_spacing = chassis_len / rows
    x_start = -(chassis_len / 2) + (x_spacing / 2)
    wheel_positions = []
    if num_wheels == 3:
        # Tricycle: 1 front, 2 back
        wheel_positions.append((chassis_len/2, 0)) # Front Center
        wheel_positions.append((-chassis_len/2, chassis_width/2 + wheel_width/2)) # Back Left
        wheel_positions.append((-chassis_len/2, -chassis_width/2 - wheel_width/2)) # Back Right
    else:
        # Standard rows
        for i in range(rows):
            x = x_start + i * x_spacing
            # Left
            wheel_positions.append((x, chassis_width/2 + wheel_width/2))
            # Right
            if len(wheel_positions) < num_wheels:
                wheel_positions.append((x, -chassis_width/2 - wheel_width/2))
    for i, (x, y) in enumerate(wheel_positions):
        # Wheel center Z is at radius height (touching ground)
        pos = cursor_loc + mathutils.Vector((x, y, wheel_radius))
        # AI Editor Note: Use parametric wheel generator for robustness
        wheel = create_parametric_part_object(context, 'WHEEL', 'WHEEL_OFFROAD', pos, scale_factor=scale_factor, radius=wheel_radius, length=wheel_width, teeth=12)
        wheel.name = get_unique_name(f"Wheel_{i+1}")
        # Parametric wheels are generated Y-aligned (rolling X), so rotate 90 Z to roll Y? No, rover rolls X.
        # Standard wheel gen is Y-axle. Rover moves X. So wheels need to be Y-axle.
        # The generator does `bmesh.ops.rotate(..., matrix=Rotation(90, 'X'))` making it Y-axle.
        # So no extra rotation needed if we want them to roll along X.
        context.view_layer.objects.active = wheel
        wheel.select_set(True)
        bpy.ops.lsd.add_bone()
        w_bone = f"Bone_{wheel.name.replace('.', '_')}"
        # Parent to chassis
        context.view_layer.objects.active = rig
        rig.select_set(True)
        bpy.ops.object.mode_set(mode='EDIT', toggle=False)
        rig.data.edit_bones[w_bone].parent = rig.data.edit_bones[chassis_bone]
        bpy.ops.object.mode_set(mode='POSE', toggle=False)
        pbone = rig.pose.bones.get(w_bone)
        if pbone:
            pbone.lsd_pg_kinematic_props.joint_type = 'continuous'
            pbone.lsd_pg_kinematic_props.axis_alignment = 'Y'
            update_single_bone_gizmo(pbone, True)
            apply_native_constraints(pbone)
    # --- ACTION: Add Sensors ---
    if 'LIDAR' in config['components']:
        # Place on top front of chassis
        # Top Z = chassis_center_z + chassis_height/2
        lidar_z = chassis_center_z + (chassis_height / 2.0) + (0.02 * scale_factor * s) # Slight offset
        lidar_pos = cursor_loc + mathutils.Vector(((chassis_len/3), 0, lidar_z)) # chassis_len is already scaled
        # AI Editor Note: Use parametric sensor
        lidar = create_parametric_part_object(context, 'ELECTRONICS', 'SENSOR_LIDAR', lidar_pos, scale_factor=scale_factor, radius=0.03*scale_factor, length=0.04*scale_factor)
        lidar.name = get_unique_name("Lidar")
        context.view_layer.objects.active = lidar
        lidar.select_set(True)
        bpy.ops.lsd.add_bone()
        l_bone = f"Bone_{lidar.name.replace('.', '_')}"
        context.view_layer.objects.active = rig
        rig.select_set(True)
        bpy.ops.object.mode_set(mode='EDIT', toggle=False)
        rig.data.edit_bones[l_bone].parent = rig.data.edit_bones[chassis_bone]
        bpy.ops.object.mode_set(mode='POSE', toggle=False)
        pbone = rig.pose.bones.get(l_bone)
        if pbone:
            pbone.lsd_pg_kinematic_props.joint_type = 'fixed'
            update_single_bone_gizmo(pbone, True)
            apply_native_constraints(pbone)
    # --- ACTION: Add Arm ---
    if 'ARM' in config['components']:
        # AI Editor Note: Force update to ensure chassis matrix_world is correct before attaching arm.
        # This ensures the arm base is placed relative to the chassis's final position.
        context.view_layer.update()
        # Calculate mount point relative to chassis center
        # We want the arm base to sit exactly on top of the chassis.
        # Chassis Top Z (relative to center) = chassis_height / 2
        arm_z_offset = (chassis_height / 2.0)
        _build_procedural_arm(context, config, scale_factor, base_obj=chassis, start_z=arm_z_offset)
def build_generative_robot(context: bpy.types.Context, prompt: str, scale_factor: float = 1.0):
    """
    Builds a robot based on the interpreted prompt using procedural logic.
    Structure: Context -> Foundation -> Perception -> Cognition -> Action -> Guard
    """
    # --- 1. CONTEXT: Setup environment ---
    if context.active_object and context.active_object.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    bpy.ops.object.select_all(action='DESELECT')
    # --- 2. FOUNDATION: Create Rig ---
    bpy.ops.lsd.create_rig()
    rig = context.scene.lsd_active_rig
    if not rig:
        print("URDF AI Error: Failed to create rig foundation.")
        return
    # --- 3. PERCEPTION: Parse Prompt ---
    config = parse_natural_language_prompt(prompt)
    print(f"URDF AI: Generating {config['type']} with params {config['params']} and components {config['components']}")
    # --- 4. COGNITION & ACTION: Dispatch to Builders ---
    try:
        if config['type'] == 'ROVER':
            _build_procedural_rover(context, config, scale_factor)
        elif config['type'] == 'ARM':
            _build_procedural_arm(context, config, scale_factor)
        elif config['type'] == 'MOBILE_BASE':
            build_mobile_base_diff_drive(context, scale_factor=scale_factor)
        elif config['type'] == 'QUADRUPED':
            build_quadruped_spider(context, scale_factor=scale_factor)
        elif config['type'] == 'DRONE':
            _build_procedural_drone(context, config, scale_factor)
        elif config['type'] == 'PLANE':
            _build_procedural_plane(context, config, scale_factor)
        elif config['type'] == 'FURNITURE':
            _build_procedural_furniture(context, config, scale_factor)
        elif config['type'] == 'CONVEYOR':
            _build_procedural_conveyor(context, config, scale_factor)
        elif config['type'] == 'HUMANOID':
            _build_procedural_humanoid(context, config, scale_factor)
        elif config['type'] in ['BOX', 'CYLINDER', 'SPHERE']:
            _build_simple_shape(context, config['type'], scale_factor)
        elif config['type'] == 'PARTS_ONLY':
            _build_procedural_parts(context, config, scale_factor)
        else:
            # Fallback
            _build_procedural_rover(context, config, scale_factor)
        # Final update to ensure everything is attached correctly
        context.view_layer.update()
    except Exception as e:
        # --- 5. GUARD: Error Handling ---
        print(f"URDF AI Error during generation: {e}")
        import traceback
        traceback.print_exc()
    # --- 6. FINALIZATION ---
    bpy.ops.object.mode_set(mode='OBJECT')
    bpy.ops.object.select_all(action='DESELECT')
    rig.select_set(True)
    context.view_layer.objects.active = rig
# ------------------------------------------------------------------------

#   PART 1.5: UI COLLAPSE LOGIC

# ------------------------------------------------------------------------

def get_all_children_objects(bone: bpy.types.PoseBone, context: bpy.types.Context) -> List[bpy.types.Object]:
    """
    Finds all objects (meshes, empties, etc.) that are effectively parented to a given bone,
    including deeply nested children hierarchies.
    """
    rig = bone.id_data
    all_objs = set()
    def gather_recursive(obj):
        all_objs.add(obj)
        for child in obj.children:
            gather_recursive(child)
    # Find all objects parented directly to this bone and start recursion.
    for obj in context.scene.objects:
        if obj.parent == rig and obj.parent_type == 'BONE' and obj.parent_bone == bone.name:
            gather_recursive(obj)
    return list(all_objs)
def calculate_bone_mesh_radius(bone: bpy.types.PoseBone, scene: bpy.types.Scene) -> float:
    """
    Calculates the physical radius (in meters) of a bone based on the high-precision
    perpendicular distance from mesh vertices to the bone's axis.
    """
    # 1. Gather all meshes associated with this bone's mechanical hierarchy
    armature_obj = bone.id_data
    # Find direct children of the bone (could be meshes or empties/groups)
    bone_direct_children = [o for o in armature_obj.children if o.parent_type == 'BONE' and o.parent_bone == bone.name]
    all_meshes = []
    for root in bone_direct_children:
        if root.type == 'MESH':
            all_meshes.append(root)
        # Recursively find all meshes under these roots
        all_meshes.extend([c for c in root.children_recursive if c.type == 'MESH'])
    local_min = mathutils.Vector((0.0, 0.0, 0.0))
    local_max = mathutils.Vector((0.0, 0.0, 0.0))
    has_meshes = False
    # Normalize our head/tail to world space for calculation
    head_world = armature_obj.matrix_world @ bone.head
    tail_world = armature_obj.matrix_world @ bone.tail
    bone_axis_world = (tail_world - head_world).normalized()
    max_radius_bu = 0.0
    for mesh_obj in all_meshes:
        has_meshes = True
        # We iterate over world-space bounding box corners for performance,
        # mirroring the logic used in the AddBone generator.
        for point in mesh_obj.bound_box:
            v_world = mesh_obj.matrix_world @ mathutils.Vector(point)
            # Perpendicular distance from world point to infinite world line (head->tail)
            vec_to_point = v_world - head_world
            proj_len = vec_to_point.dot(bone_axis_world)
            proj_vec = proj_len * bone_axis_world
            dist = (vec_to_point - proj_vec).length
            if dist > max_radius_bu:
                max_radius_bu = dist
    if not has_meshes:
        return 0.0
    # 2. Convert BU to Meters (CAD Standard)
    unit_scale = scene.unit_settings.scale_length
    if unit_scale <= 0: unit_scale = 1.0
    return max_radius_bu * unit_scale
def update_single_bone_gizmo(bone: bpy.types.PoseBone, show_gizmos: bool = True, style: str = 'DEFAULT') -> None:
    """
    Updates the custom shape (widget or "gizmo") for a single bone to visually
    represent its joint properties.
    Args:
        bone: The `PoseBone` whose gizmo needs to be updated.
        show_gizmos: A boolean indicating whether gizmos should be visible.
        style: The visual style of the gizmo ('DEFAULT', '3D').
    """
    props = bone.lsd_pg_kinematic_props
    if not show_gizmos or props.joint_type == 'none':
        bone.custom_shape = None
        return
    # 1. Access visual settings and ensure the bone is in the correct rotation mode.
    bone.rotation_mode = 'XYZ'
    gizmo_type = 'ROTATION'
    if props.joint_type == 'prismatic':
        gizmo_type = 'SLIDER'
    elif props.joint_type == 'spherical':
        gizmo_type = 'SPHERICAL'
    elif props.joint_type == 'fixed':
        gizmo_type = 'FIXED'
    elif props.joint_type == 'base':
        gizmo_type = 'BASE'
    # 2. Determine the physical alignment axis.
    raw_axis = props.axis_alignment.replace("-", "")
    if raw_axis == 'X': target_axis = 'Z'
    elif raw_axis == 'Y': target_axis = 'X'
    else: target_axis = 'Y'
    if gizmo_type == 'BASE': target_axis = 'Z'
    # 3. Apply the widget shape.
    wgt = create_flat_gizmo(gizmo_type, target_axis, style)
    if wgt:
        bone.custom_shape = wgt
        # 4. Physical Scale Normalization.
        # props.joint_radius is stored in Meters (CAD-standard).
        # We MUST normalize to scene scale (Meters per BU) to get the correct visual size.
        search_ctx = bpy.context if bpy.context and hasattr(bpy.context, "scene") else None
        target_scene = search_ctx.scene if search_ctx else (bone.id_data.users_scene[0] if bone.id_data.users_scene else bpy.data.scenes[0])
        unit_scale = target_scene.unit_settings.scale_length
        if unit_scale <= 0: unit_scale = 1.0
        r_bu = props.joint_radius / unit_scale
        final_scale = r_bu * props.visual_gizmo_scale
        # Guard against invisible scales
        if final_scale < 0.001: final_scale = 0.001
        bone.custom_shape_scale_xyz = (final_scale, final_scale, final_scale)
        bone.use_custom_shape_bone_size = False
        bone.custom_shape_translation = (0, 0, 0)
        bone.custom_shape_rotation_euler = (0, 0, 0)
def get_mapped_axis_index(ui_axis_alignment: str) -> int:
    """
    Converts the UI axis alignment string (e.g., 'X', '-Y', 'Z') to a numerical
    index (0, 1, 2) for use with Blender's vector and array properties.
    Args:
        ui_axis_alignment: The string representation of the axis from the UI.
    Returns:
        The corresponding integer index (0 for X, 1 for Y, 2 for Z).
    """
    axis = ui_axis_alignment.replace("-", "")
    if axis == 'X': return 0
    if axis == 'Y': return 1
    # Default to Z for safety, although the enum should prevent other values.
    return 2
def clean_conflicting_mechanics(bone: bpy.types.PoseBone) -> None:
    """
    Prevents mechanical conflicts by removing obsolete drivers from a bone when
    its joint type is changed.
    For example, if a user changes a joint from 'revolute' to 'prismatic', this
    function will find and remove any old drivers that were controlling the bone's
    rotation, as they are no longer valid for a prismatic joint. This is crucial
    for preventing unexpected behavior and ensuring a clean state.
    Args:
        bone: The `PoseBone` to clean.
    """
    props = bone.lsd_pg_kinematic_props
    is_rot = props.joint_type in ['revolute', 'continuous', 'spherical']
    is_lin = props.joint_type == 'prismatic'
    # Check if the bone's armature has any animation data (and thus, any drivers).
    if not (bone.id_data and bone.id_data.animation_data and bone.id_data.animation_data.drivers):
        return
    drivers = bone.id_data.animation_data.drivers
    drivers_to_remove = []
    for d in drivers:
        # Check if the driver exactly targets the current bone.
        expected_prefix = f'pose.bones["{bone.name}"].'
        if d.data_path.startswith(expected_prefix):
            # If the new type is rotational, remove any old location drivers.
            if is_rot and "location" in d.data_path:
                drivers_to_remove.append(d)
            # If the new type is linear, remove any old rotation drivers.
            elif is_lin and "rotation" in d.data_path:
                drivers_to_remove.append(d)
            # If the new type is fixed or none, remove all drivers.
            elif props.joint_type in ['fixed', 'none', 'base']:
                drivers_to_remove.append(d)
    for d in drivers_to_remove:
        drivers.remove(d)
def remove_all_lsd_constraints(bone: bpy.types.PoseBone) -> None:
    """Helper to thoroughly remove all limit constraints created by the addon."""
    from .config import MOD_PREFIX
    for c in list(bone.constraints):
        if c.name.startswith(f"{MOD_PREFIX}Limit_Rot") or c.name.startswith(f"{MOD_PREFIX}Limit_Loc"):
            bone.constraints.remove(c)
def apply_native_constraints(bone: bpy.types.PoseBone) -> None:
    """
    Applies all native Blender constraints (FK locks, IK limits, and Limit
    constraints) to a bone based on its URDF properties.
    This function is the heart of the addon's kinematics system. It translates
    the high-level URDF joint properties into a set of standard Blender
    constraints, ensuring that the rig behaves correctly during posing and
    animation.
    It uses a robust "calculate-then-apply" strategy. It first determines the
    desired final state of all relevant properties in local variables, then
    applies that state to the bone's properties all at once. This avoids
    intermediate states and potential dependency issues.
    Args:
        bone: The `PoseBone` to apply the constraints to.
    """
    props = bone.lsd_pg_kinematic_props
    # Get the numerical index (0, 1, 2) corresponding to the UI selection.
    ui_idx = get_mapped_axis_index(props.axis_alignment)
    # This block maps the UI axis to the bone's local axis.
    # AI Editor Note: Correcting axis misalignment.
    # The user reported a cyclic permutation where UI 'X' affected Blender's Y-axis,
    # 'Y' affected 'Z', and 'Z' affected 'X'. To correct this, we apply an
    # inverse permutation to the axis index before it's used for constraints.
    # AI Editor Note: DO NOT CHANGE THIS MAPPING. It is notoriously tricky to align.
    axis_map = {0: 2, 1: 0, 2: 1}
    unlocked_idx = axis_map.get(ui_idx, ui_idx)
    # AI Editor Note: Avoid using global bpy.context where possible for timer safety.
    # Get the scene from the bone's armature object.
    scene = bone.id_data.users_scene[0] if bone.id_data.users_scene else bpy.context.scene
    is_placing = scene.lsd_placement_mode
    # --- 1. Handle Special Modes (Placement / None) ---
    if is_placing:
        # AI Editor Note: In placement mode, respect the 'Connected' parent-child relationship.
        # If a bone is 'Connected' to its parent, its location will remain locked.
        # This allows users to move chains of connected bones as a single unit.
        should_lock_location = bone.parent is not None and bone.bone.use_connect
        bone.lock_location = (should_lock_location, should_lock_location, should_lock_location)
        bone.lock_rotation = (False, False, False)
        bone.lock_scale = (True, True, True)  # Scale is always locked for stability.
        # Unlock all IK axes and disable limits.
        bone.lock_ik_x = False
        bone.lock_ik_y = False
        bone.lock_ik_z = False
        bone.use_ik_limit_x = False
        bone.use_ik_limit_y = False
        bone.use_ik_limit_z = False
        remove_all_lsd_constraints(bone)
        return
    if props.joint_type in ['none', 'base']:
        bone.lock_location = (False, False, False)
        bone.lock_rotation = (False, False, False)
        bone.lock_scale = (True, True, True)
        bone.lock_ik_x = False
        bone.lock_ik_y = False
        bone.lock_ik_z = False
        bone.use_ik_limit_x = False
        bone.use_ik_limit_y = False
        bone.use_ik_limit_z = False
        remove_all_lsd_constraints(bone)
        return
    # --- 2. Calculate the Desired Constraint State ---
    # Start by assuming a fully locked 'fixed' joint, then unlock axes as needed.
    fk_lock_loc = [True, True, True]
    fk_lock_rot = [True, True, True]
    ik_lock_loc = [True, True, True]
    ik_use_limit_rot = [True, True, True]
    ik_rot_limits = [(0, 0), (0, 0), (0, 0)]
    remove_all_lsd_constraints(bone)
    if props.joint_type in ['revolute', 'continuous']:
        # For rotational joints, unlock the appropriate rotation axis for both FK and IK.
        fk_lock_rot[unlocked_idx] = False
        ik_use_limit_rot[unlocked_idx] = False  # Allow free rotation for IK by default.
        if props.joint_type == 'revolute':
            # For 'revolute' joints, apply limits.
            min_rad = math.radians(props.lower_limit)
            max_rad = math.radians(props.upper_limit)
            # Invert limits for Y and Z axes to achieve the final composite rotation
            if unlocked_idx in [1, 2]: # Y or Z axis
                min_rad, max_rad = -max_rad, -min_rad
            # Re-enable the IK limit for the unlocked axis with the specified values.
            ik_use_limit_rot[unlocked_idx] = True
            ik_rot_limits[unlocked_idx] = (min_rad, max_rad)
            # Add a 'LIMIT_ROTATION' constraint for FK posing.
            con = bone.constraints.new('LIMIT_ROTATION')
            con.name = f"{MOD_PREFIX}Limit_Rot"
            con.owner_space = 'LOCAL'
            if unlocked_idx == 0: con.use_limit_x = True; con.min_x = min_rad; con.max_x = max_rad
            elif unlocked_idx == 1: con.use_limit_y = True; con.min_y = min_rad; con.max_y = max_rad
            elif unlocked_idx == 2: con.use_limit_z = True; con.min_z = min_rad; con.max_z = max_rad
    elif props.joint_type == 'spherical':
        # Spherical joints are free to rotate on all axes
        fk_lock_rot = [False, False, False]
        # For IK, unlock all rotation axes and apply limits
        ik_use_limit_rot = [True, True, True]
        # Radians conversion
        lx, ux = math.radians(props.lower_limit), math.radians(props.upper_limit)
        ly, uy = math.radians(props.lower_limit_y), math.radians(props.upper_limit_y)
        lz, uz = math.radians(props.lower_limit_z), math.radians(props.upper_limit_z)
        ik_rot_limits[0] = (lx, ux)
        ik_rot_limits[1] = (-uy, -ly) # Invert for axis map consistency
        ik_rot_limits[2] = (-uz, -lz)
        # Add a 'LIMIT_ROTATION' constraint for FK posing.
        con = bone.constraints.new('LIMIT_ROTATION')
        con.name = f"{MOD_PREFIX}Limit_Rot"
        con.owner_space = 'LOCAL'
        con.use_limit_x = True; con.min_x = lx; con.max_x = ux
        con.use_limit_y = True; con.min_y = -uy; con.max_y = -ly
        con.use_limit_z = True; con.min_z = -uz; con.max_z = -lz
    elif props.joint_type == 'prismatic':
        # For linear joints, unlock the appropriate location axis for both FK and IK.
        fk_lock_loc[unlocked_idx] = False
        ik_lock_loc[unlocked_idx] = False
        # Add a 'LIMIT_LOCATION' constraint for FK posing.
        min_val = props.lower_limit
        max_val = props.upper_limit
        # Invert limits for Y and Z axes to achieve the final composite rotation
        if unlocked_idx in [1, 2]: # Y or Z axis
            min_val, max_val = -max_val, -min_val
        con = bone.constraints.new('LIMIT_LOCATION')
        con.name = f"{MOD_PREFIX}Limit_Loc"
        con.owner_space = 'LOCAL'
        if unlocked_idx == 0: con.use_min_x = True; con.use_max_x = True; con.min_x = min_val; con.max_x = max_val
        elif unlocked_idx == 1: con.use_min_y = True; con.use_max_y = True; con.min_y = min_val; con.max_y = max_val
        elif unlocked_idx == 2: con.use_min_z = True; con.use_max_z = True; con.min_z = min_val; con.max_z = max_val
    # --- 3. Apply All Calculated Properties to the Bone at Once ---
    # This atomic update ensures a clean and predictable state change.
    bone.lock_location = fk_lock_loc
    bone.lock_rotation = fk_lock_rot
    bone.lock_scale = (True, True, True)
    bone.lock_ik_x = ik_lock_loc[0]
    bone.lock_ik_y = ik_lock_loc[1]
    bone.lock_ik_z = ik_lock_loc[2]
    # --- AI Editor Note: Apply rotation IK limits where applicable ---
    bone.use_ik_limit_x = ik_use_limit_rot[0]
    bone.use_ik_limit_y = ik_use_limit_rot[1]
    bone.use_ik_limit_z = ik_use_limit_rot[2]
    bone.ik_min_x, bone.ik_max_x = ik_rot_limits[0]
    bone.ik_min_y, bone.ik_max_y = ik_rot_limits[1]
    bone.ik_min_z, bone.ik_max_z = ik_rot_limits[2]
# --- AI Editor Note: Guard and Handler for Local Cursor Tool ---

def update_local_cursor_from_tool(self, context):
    """
    Update callback for the local cursor tool property.
    When the user edits the local coordinates in the UI, this function
    calculates the new world position for the 3D cursor and applies it.
    """
    global _local_cursor_update_guard
    # Guard to prevent this from running when the property is updated by the draw function
    if _local_cursor_update_guard:
        return
    if context.active_object:
        obj = context.active_object
        # 'self' is the scene here
        local_co = self.lsd_cursor_local_pos
        world_co = obj.matrix_world @ mathutils.Vector(local_co)
        # Check to prevent feedback loops if the value hasn't changed significantly
        if (context.scene.cursor.location - world_co).length > 0.0001:
            context.scene.cursor.location = world_co
@persistent

def local_cursor_depsgraph_handler(scene: bpy.types.Scene, depsgraph: bpy.types.Depsgraph) -> None:
    """
    A persistent handler that runs on dependency graph updates to keep the
    local cursor tool synchronized with the 3D cursor's actual position.
    This avoids writing data from within a draw function, which is forbidden.
    """
    # This handler can run in many contexts, so we need to be careful.
    context = bpy.context
    if not (context and context.active_object):
        return
    obj = context.active_object
    # Avoid ValueError: Matrix.invert(ed): matrix does not have an inverse
    if abs(obj.matrix_world.determinant()) < 1e-6:
        return
    cursor_local_vec = obj.matrix_world.inverted() @ scene.cursor.location
    # AI Editor Note: Attributes may be missing during add-on registration/unregistration.
    if not hasattr(scene, "lsd_cursor_local_pos"):
        return
    global _local_cursor_update_guard
    # Convert property array to mathutils.Vector before subtraction
    actual_pos = mathutils.Vector(getattr(scene, "lsd_cursor_local_pos", (0,0,0)))
    if (actual_pos - cursor_local_vec).length > 0.0001:
        _local_cursor_update_guard = True
        try:
            scene.lsd_cursor_local_pos = cursor_local_vec
        finally:
            _local_cursor_update_guard = False
def lsd_prop_update(self, context, prop_name: str):
    """
    Generic update callback for URDF properties on PoseBones.
    This function handles multi-object editing for URDF properties. When a
    property is changed on the active bone, this function propagates that
    single property's new value to all other selected bones. It then updates
    the visual and mechanical state of all selected bones to reflect the change.
    A global guard is used to prevent recursive updates, which can occur when
    setting properties on other objects from within an update callback.
    """
    global _prop_update_guard
    if _prop_update_guard:
        return
    # The bone this property group instance belongs to
    this_bone = self.id_data
    if not isinstance(this_bone, bpy.types.PoseBone):
        # AI Editor Note: For properties on PoseBones, id_data is the Armature Object.
        # We must parse the bone name from the data path to identify which bone changed.
        if isinstance(this_bone, bpy.types.Object) and this_bone.type == 'ARMATURE':
            try:
                # AI Editor Note: path_from_id() can fail in certain context transition states.
                # We wrap it in a try-except to prevent the entire handler from crashing.
                path = self.path_from_id()
                # Expected path format: pose.bones["BoneName"].lsd_pg_kinematic_props
                match = re.search(r'pose\.bones\["([^"]+)"\]', path)
                if match:
                    bone_name = match.group(1)
                    this_bone = this_bone.pose.bones.get(bone_name)
            except (ValueError, RuntimeError):
                # Fallback to identify bone via name if path parsing fails
                pass
    if not isinstance(this_bone, bpy.types.PoseBone):
        return
    # --- Part 1: State Update ---
    # AI Editor Note: Always update the state of the bone whose property was changed.
    # This ensures that when a property is set on a non-active bone (e.g. during
    # propagation), its visual and mechanical state is correctly updated.
    clean_conflicting_mechanics(this_bone)
    update_single_bone_gizmo(this_bone, context.scene.lsd_viz_gizmos)
    apply_native_constraints(this_bone)
    # Special handling for relationships and IK
    if prop_name in {'ratio_value', 'ratio_invert'} and self.ratio_target_bone:
        add_native_driver_relation(this_bone, self.ratio_target_bone, self.ratio_value, self.ratio_invert)
    elif prop_name == 'ik_chain_length':
        update_ik_chain_length(this_bone, context)
    # --- Part 2: Propagation for Multi-Object Editing (with guard) ---
    # Guard is already checked at the top, but we'll use a local one for safety here if needed.
    # We only propagate from the active bone to others.
    active_bone = context.active_pose_bone
    if active_bone and this_bone == active_bone:
        _prop_update_guard = True
        try:
            new_value = getattr(self, prop_name)
            # Use safe bone gathering for multi-object editing.
            # Compatibility with Blender 3.2+ and fallback for older versions.
            bones_to_update = getattr(context, 'selected_pose_bones_from_active_object', context.selected_pose_bones)
            for bone in bones_to_update:
                if bone != active_bone:
                    # Setting this property will call lsd_prop_update(bone, ...)
                    # which will trigger Part 1 for that bone, and then return because of _prop_update_guard.
                    if getattr(bone.lsd_pg_kinematic_props, prop_name) != new_value:
                        setattr(bone.lsd_pg_kinematic_props, prop_name, new_value)
        finally:
            _prop_update_guard = False
@persistent

def active_bone_change_handler(scene: bpy.types.Scene, depsgraph: bpy.types.Depsgraph) -> None:
    """
    A persistent handler that runs on dependency graph updates to detect when
    the active pose bone changes. When it does, it updates the global Joint
    Editor tool with the properties of the newly selected bone.
    """
    global _last_active_bone_key, _joint_editor_update_guard
    # This handler can run in many contexts, so we need to be careful.
    context = bpy.context
    # Identify the target bone using a more robust context-safe approach.
    # We prioritize the evaluated object from the depsgraph to get accurate selection state.
    target_bone = None
    active_obj = depsgraph.view_layer.objects.active
    if active_obj:
        if active_obj.type == 'ARMATURE' and active_obj.mode == 'POSE':
            bone = active_obj.data.bones.active
            if bone:
                target_bone = active_obj.pose.bones.get(bone.name)
        elif active_obj.parent and active_obj.parent.type == 'ARMATURE' and active_obj.parent_type == 'BONE':
            # Support clicking a child mesh in Object Mode to detect its parent joint
            target_bone = active_obj.parent.pose.bones.get(active_obj.parent_bone)
    if not target_bone:
        _last_active_bone_key = None
        return
    # Robust selection key: Armature_Name + Bone_Name
    active_bone_key = f"{target_bone.id_data.name}:{target_bone.name}"
    if active_bone_key != _last_active_bone_key:
        _last_active_bone_key = active_bone_key
        # 1. Perform Automatic Radius Detection only if uninitialized (0.0).
        # This ensures that once a user manual sets a radius, it is PERSISTENT and UNIQUE.
        current_radius = target_bone.lsd_pg_kinematic_props.joint_radius
        if current_radius < 1e-6: # effectively zero
            mesh_radius = calculate_bone_mesh_radius(target_bone, scene)
            if mesh_radius > 0:
                target_bone.lsd_pg_kinematic_props.joint_radius = mesh_radius
        # 2. Force Refresh of UI
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()
    elif not target_bone:
        _last_active_bone_key = None
def get_mimic_ratio(bone: bpy.types.PoseBone, target_name: str) -> float:
    """Calculates the physical mechanical ratio between the bone and its driver."""
    rig = bone.id_data
    target = rig.pose.bones.get(target_name)
    if not target: return 1.0
    props = bone.lsd_pg_kinematic_props
    props_tgt = target.lsd_pg_kinematic_props
    is_rot_self = props.joint_type in ['revolute', 'continuous', 'spherical']
    is_rot_tgt = props_tgt.joint_type in ['revolute', 'continuous', 'spherical']
    len_driver = props_tgt.joint_radius if is_rot_tgt else target.length
    len_follower = props.joint_radius if is_rot_self else bone.length
    # Robust fallbacks
    if len_follower < 0.00001: len_follower = 0.05
    if len_driver < 0.00001: len_driver = 0.05
    if is_rot_self and not is_rot_tgt: # Follower ROT, Driver LIN (Rack & Pinion)
        return 1.0 / len_follower
    elif not is_rot_self and is_rot_tgt: # Follower LIN, Driver ROT
        return len_driver
    elif is_rot_self and is_rot_tgt: # Both ROT (Gears)
        return len_driver / len_follower
    return 1.0
def auto_calculate_gear_ratio(bone: bpy.types.PoseBone) -> None:
    """Updates the 'Staging Buffer' ratio for the active bone."""
    props = bone.lsd_pg_kinematic_props
    props.ratio_value = get_mimic_ratio(bone, props.ratio_target_bone)
def auto_calculate_mimic_item(bone: bpy.types.PoseBone, item: bpy.types.PropertyGroup) -> None:
    """Updates a SPECIFIC relationship in the mimic list."""
    item.ratio = get_mimic_ratio(bone, item.target_bone)
def reapply_mimic_drivers(bone: bpy.types.PoseBone) -> None:
    """Builds multi-axis drivers for the bone based on its mimic_drivers collection."""
    props = bone.lsd_pg_kinematic_props
    if not props.mimic_drivers: return
    rig = bone.id_data
    # 1. Base Setup
    is_rot_tgt = props.joint_type in ['revolute', 'continuous', 'spherical']
    is_1dof_tgt = props.joint_type in ['revolute', 'continuous', 'prismatic']
    data_path = "rotation_euler" if is_rot_tgt else "location"
    # Mapping and unit conversion
    axis_map = {0: 2, 1: 0, 2: 1} # LSD UI to Blender Bone
    ui_idx_tgt = get_mapped_axis_index(props.axis_alignment)
    idx_tgt = axis_map.get(ui_idx_tgt, ui_idx_tgt)
    unit_scale = rig.users_scene[0].unit_settings.scale_length if (rig.users_scene and len(rig.users_scene) > 1) else 1.0
    # Process axes (X, Y, Z)
    for i_axis in range(3):
        # 1-DOF Follower constraint: Only drive its primary active axis
        if is_1dof_tgt and i_axis != idx_tgt:
            continue
        expected_path = f'pose.bones["{bone.name}"].{data_path}'
        # Determine if this axis should be cleared or driven
        # Find or Create
        driver_fcurve = None
        if rig.animation_data:
            for d in rig.animation_data.drivers:
                if d.data_path == expected_path and d.array_index == i_axis:
                    driver_fcurve = d
                    break
        has_active_vars = False
        expr_parts = []
        drv = None
        # Build expression from all mimic sources
        for i_var, item in enumerate(props.mimic_drivers):
            source_bone = rig.pose.bones.get(item.target_bone)
            if not source_bone: continue
            props_src = source_bone.lsd_pg_kinematic_props
            ui_idx_src = get_mapped_axis_index(props_src.axis_alignment)
            idx_src = axis_map.get(ui_idx_src, ui_idx_src)
            is_rot_src = props_src.joint_type in ['revolute', 'continuous', 'spherical']
            is_1dof_src = props_src.joint_type in ['revolute', 'continuous', 'prismatic']
            # Axis Filtering Logic (Fix for ghosting)
            # 1. Spherical Follower: Drive multiple axes simultaneously if toggled by user
            # 2. 1-DOF Follower: ONLY drive its single physical axis
            should_drive = False
            mapped_src_idx = i_axis
            if is_1dof_tgt:
                 should_drive = True # We already filtered for idx_tgt in the outer loop
                 mapped_src_idx = idx_src if is_1dof_src else i_axis
            else: # Spherical Follower (multi-axis)
                # Check the specific toggle for THIS axis from the relationship list
                if i_axis == 0 and item.drive_x: should_drive = True
                elif i_axis == 1 and item.drive_y: should_drive = True
                elif i_axis == 2 and item.drive_z: should_drive = True
                # For Spherical-to-Spherical or Spherical-to-1DOF:
                # If we are driving this axis, we map for 1:1 follow if driver is multi-axis,
                # or use driver absolute channel if driver is 1-DOF.
                if should_drive:
                    mapped_src_idx = idx_src if is_1dof_src else i_axis
            if not should_drive: continue
            # Initialize driver if we found a valid source for this axis
            if not drv:
                if not driver_fcurve:
                    driver_fcurve = bone.driver_add(data_path, i_axis)
                drv = driver_fcurve.driver
                drv.type = 'SCRIPTED'
                for v in list(drv.variables): drv.variables.remove(v)
            var_name = f"var_{i_var:03d}"
            var = drv.variables.new()
            var.name = var_name
            var.type = 'SINGLE_PROP' if is_rot_src else 'TRANSFORMS'
            var.targets[0].id = rig
            factor = -1.0 if item.invert else 1.0
            if is_rot_src:
                var.targets[0].data_path = f'pose.bones["{item.target_bone}"].rotation_euler[{mapped_src_idx}]'
                expr_parts.append(f"({var_name} * {item.ratio:.6f} * {factor})")
            else:
                t = var.targets[0]
                t.bone_target = item.target_bone
                t.transform_space = 'LOCAL_SPACE'
                t.transform_type = ['LOC_X', 'LOC_Y', 'LOC_Z'][mapped_src_idx]
                expr_parts.append(f"({var_name} * {unit_scale:.6f} * {item.ratio:.6f} * {factor})")
            has_active_vars = True
        # Final Assembly / Cleanup
        if drv:
            if not expr_parts:
                drv.expression = "0.0"
            else:
                full_expr = " + ".join(expr_parts)
                drv.expression = f"({full_expr}) / {unit_scale:.6f}" if not is_rot_tgt else full_expr
        # Lock property if driven (or leave unlocked if no drivers exist on this axis)
        if has_active_vars:
            if is_rot_tgt: bone.lock_rotation[i_axis] = True
            else: bone.lock_location[i_axis] = True
def add_native_driver_relation(target_bone: bpy.types.PoseBone, source_bone_name: str, ratio: float, invert: bool = False) -> None:
    # Legacy wrapper for older calls, now uses the collection re-application logic
    reapply_mimic_drivers(target_bone)
def invert_ratio_update(self: 'LSD_Properties', context: bpy.types.Context) -> None:
    """
    Update callback for the 'Invert' checkbox in the gear ratio UI.
    This function is triggered when the user toggles the 'Invert' checkbox for a
    gear/mimic relationship. It immediately calls `add_native_driver_relation`
    to update the driver with the new inversion setting.
    Args:
        self: The `LSD_Properties` instance that was changed.
        context: The current Blender context.
    """
    if context.active_pose_bone and self.ratio_target_bone:
        add_native_driver_relation(context.active_pose_bone, self.ratio_target_bone, self.ratio_value, self.ratio_invert)
def lsd_joint_editor_update_callback(self, context):
    """
    Update callback for the global joint editor tool properties.
    Automatically calls the apply operator to push changes to the selected bones.
    """
    # --- AI Editor Note: Add guard to prevent unwanted mode switching ---
    # This prevents the operator from being called when properties are set
    # programmatically, such as during the creation of a new joint part.
    global _joint_editor_update_guard
    if _joint_editor_update_guard:
        return None
    # Using a timer ensures the operator runs in a clean context after the update.
    # The operator's poll method will handle context checks (e.g., pose mode).
    bpy.app.timers.register(lambda: (bpy.ops.lsd.apply_joint_settings() and None), first_interval=0.01)
    return None
def update_ratio_live(self: 'LSD_Properties', context: bpy.types.Context) -> None:
    """
    Update callback for relationship properties in the bone editor.
    Propagates changes to other selected bones via the standard update logic.
    """
    lsd_prop_update(self, context, 'ratio_value')
def update_ratio_invert(self: 'LSD_Properties', context: bpy.types.Context) -> None:
    """
    Update callback for the ratio inversion toggle in the bone editor.
    """
    lsd_prop_update(self, context, 'ratio_invert')
def cleanup_unused_gizmos(context: bpy.types.Context) -> None:
    """
    Removes gizmo objects from the LSD_Widgets collection that are no longer
    assigned to any bone in any armature.
    """
    widgets_coll = bpy.data.collections.get(WIDGETS_COLLECTION_NAME)
    if not widgets_coll:
        return
    # 1. Collect all gizmo objects currently in use.
    used_widgets = set()
    for obj in bpy.data.objects:
        if obj.type == 'ARMATURE':
            for pb in obj.pose.bones:
                if pb.custom_shape:
                    used_widgets.add(pb.custom_shape)
    # 2. Identify and remove unused widgets from the collection.
    # We iterate over a list copy to safely modify the collection/scene.
    widgets_to_remove = [obj for obj in widgets_coll.objects if obj not in used_widgets]
    for obj in widgets_to_remove:
        try:
            bpy.data.objects.remove(obj, do_unlink=True)
        except:
            pass
def update_all_gizmos(self: bpy.types.Scene, context: bpy.types.Context) -> None:
    """
    Update callback for the global 'Show Gizmos' toggle in the UI.
    Triggers a full refresh of visual gizmos and mechanical constraints.
    """
    rig = context.scene.lsd_active_rig
    if not rig:
        return
    # AI Editor Note: Robustness update.
    # Retrieve settings once to pass to all bones, avoiding Context errors in loop.
    show = context.scene.lsd_viz_gizmos
    style = context.scene.lsd_gizmo_style
    for bone in rig.pose.bones:
        # 1. Update the visual gizmo
        update_single_bone_gizmo(bone, show, style)
        # 2. Re-apply constraints to ensure physics match visuals.
        clean_conflicting_mechanics(bone)
        apply_native_constraints(bone)
    # 3. Garbage collect unused gizmos to prevent "left behind" objects.
    cleanup_unused_gizmos(context)
def update_ik_chain_length(bone: bpy.types.PoseBone, context: bpy.types.Context) -> None:
    """
    Update callback for the IK chain length property on a bone.
    Synchronizes the IK constraint's chain_count with the property value.
    """
    if not bone:
        return
    props = bone.lsd_pg_kinematic_props
    ik_con = bone.constraints.get(IK_CONSTRAINT_NAME)
    if not ik_con:
        return
    # Calculate the maximum possible chain length
    max_len = 0
    current = bone
    while current:
        max_len += 1
        current = current.parent
    # Clamp to valid range
    new_length = min(props.ik_chain_length, max_len)
    ik_con.chain_count = new_length
    # Update the property to reflect clamping
    if props.ik_chain_length != new_length:
        props.ik_chain_length = new_length
# ------------------------------------------------------------------------

# AI Editor Note: Consolidated registration to the bottom of the file for architectural clarity.

def create_parametric_chain(context: bpy.types.Context, chain_type: str) -> bpy.types.Object:
    """
    Creates a complete, native, parametric chain setup.
    """
    coll_name = "Mechanical_Parts"
    coll = bpy.data.collections.get(coll_name)
    if not coll:
        coll = bpy.data.collections.new(coll_name)
        context.scene.collection.children.link(coll)
    cursor_loc = context.scene.cursor.location
    # --- 1. Generate unique names to allow for multiple chains in the scene ---
    base_name = chain_type.capitalize() # "Roller" or "Belt"
    i = 1
    while f"{base_name}_Chain_{i:03d}" in bpy.data.objects:
        i += 1
    path_name = f"{base_name}_Chain_{i:03d}"
    link_name = f"{base_name}_Link_{i:03d}"
    # --- 2. Create the hidden "Link" object that will be instanced ---
    link_mesh_data = bpy.data.meshes.new(f"{link_name}_Data")
    link_obj = bpy.data.objects.new(link_name, link_mesh_data)
    coll.objects.link(link_obj)
    link_obj.location = (0, 0, 0)
    link_obj.hide_viewport = True
    link_obj.hide_render = True
    # --- 3. Create the main Path Curve object ---
    curve = bpy.data.curves.new(f"{path_name}_Data", type='CURVE')
    curve.dimensions = '3D'
    path_obj = bpy.data.objects.new(path_name, curve)
    coll.objects.link(path_obj)
    path_obj.location = cursor_loc
    spline = curve.splines.new('BEZIER')
    spline.bezier_points.add(3) # 4 points total for a circle
    radius = 0.2
    handle_len = radius * (4 * (math.sqrt(2) - 1) / 3)
    points_data = [
        ((radius, 0, 0), (radius, -handle_len, 0), (radius, handle_len, 0)),
        ((0, radius, 0), (handle_len, radius, 0), (-handle_len, radius, 0)),
        ((-radius, 0, 0), (-radius, handle_len, 0), (-radius, -handle_len, 0)),
        ((0, -radius, 0), (-handle_len, -radius, 0), (handle_len, -radius, 0)),
    ]
    for i, (co, h_left, h_right) in enumerate(points_data):
        p = spline.bezier_points[i]
        p.co = co
        p.handle_left = h_left
        p.handle_right = h_right
        p.handle_left_type = 'AUTO'
        p.handle_right_type = 'AUTO'
    spline.use_cyclic_u = True
    # --- 4. Assign Parametric Properties to the Path object ---
    props = path_obj.lsd_pg_mech_props
    props.is_part = True
    props.category = 'CHAIN'
    props.type_chain = chain_type
    props.instanced_link_obj = link_obj
    path_obj["lsd_native_chain_pitch"] = props.length
    path_obj["lsd_native_chain_res"] = props.chain_curve_res
    path_obj["lsd_native_anim_offset"] = 0.0
    return path_obj

def ensure_smart_skin_data():
    """Systematically ensures and returns the hidden FloatCurve node for transitions."""
    node_group_name = "LSD_SmartSkin_Data"
    if node_group_name not in bpy.data.node_groups:
        nt = bpy.data.node_groups.new(node_group_name, 'ShaderNodeTree')
        node = nt.nodes.new('ShaderNodeFloatCurve')
        node.name = "SkinCurve"
        curve = node.mapping.curves[0]
        curve.points[0].location = (0.0, 1.0)
        curve.points[1].location = (1.0, 1.0)
        node.mapping.update()
    nt = bpy.data.node_groups[node_group_name]
    if "SkinCurve" not in nt.nodes:
        node = nt.nodes.new('ShaderNodeFloatCurve')
        node.name = "SkinCurve"
    return nt.nodes["SkinCurve"]


@bpy.app.handlers.persistent
def lsd_anim_layer_handler(scene, depsgraph=None):
    settings = scene.lsd_anim_settings
    if not settings.layers_enabled or not settings.layers or settings.active_layer_index < 0:
        return
    
    obj = bpy.context.active_object
    if not obj or not obj.animation_data or not obj.animation_data.nla_tracks:
        return
        
    try:
        active_layer = settings.layers[settings.active_layer_index]
        active_track = obj.animation_data.nla_tracks.get(active_layer.track_name if active_layer.track_name else active_layer.name)
        if active_track and active_track.strips:
            strip = active_track.strips[0]
            if strip.blend_type != active_layer.blend_type:
                strip.blend_type = active_layer.blend_type
            if not active_layer.is_muted and strip.influence != active_layer.influence:
                strip.influence = active_layer.influence
    except Exception:
        pass


_color_refresh_timer_active = False

def _color_refresh_loop():
    global _color_refresh_timer_active
    if not _color_refresh_timer_active:
        return None
        
    scene = bpy.context.scene
    interval = scene.lsd_dim_color_refresh_timer
    if interval <= 0.0:
        _color_refresh_timer_active = False
        return None
        
    for obj in bpy.data.objects:
        if obj.get("lsd_is_dimension"):
            sync_dimension_assembly_material(obj)
        elif getattr(obj, "lsd_is_standalone_offset", False):
            line_mat = get_or_create_line_material(obj)
            if obj.active_material != line_mat:
                obj.active_material = line_mat
            if any(abs(a - b) > 0.001 for a, b in zip(list(obj.color), list(line_mat.diffuse_color))):
                obj.color = line_mat.diffuse_color
                
    return interval

def toggle_color_refresh_timer(interval):
    global _color_refresh_timer_active
    if interval > 0.0:
        if not _color_refresh_timer_active:
            _color_refresh_timer_active = True
            bpy.app.timers.register(_color_refresh_loop, first_interval=interval)
    else:
        _color_refresh_timer_active = False

@persistent
def lsd_dimension_hook_cleanup_handler(scene, depsgraph=None):
    global _in_depsgraph_handler
    if not depsgraph or _in_depsgraph_handler: return
    _in_depsgraph_handler = True
    try:
        for obj in scene.objects:
            if obj.get("lsd_is_dimension_hook") or obj.get("lsd_is_offset_hook"):
                con = next((c for c in obj.constraints if c.type == 'COPY_LOCATION' and c.name != "Auto_Offset_Follow"), None)
                if con:
                    is_dead = False
                    if not con.target:
                        is_dead = True
                    elif con.target.get("lsd_is_dimension_anchor") == "MASTER":
                        if not con.target.parent or con.target.parent.name not in scene.objects:
                            is_dead = True
                            
                    # --- BBOX BUG CLEANUP ---
                    # If this object is a MESH, it was infected by the old BBox bug.
                    # Meshes should never be dimension hooks. Forcefully purge it!
                    if obj.type == 'MESH' and obj.get("lsd_is_dimension_hook"):
                        is_dead = True
                            
                    if is_dead:
                        if "lsd_saved_world_loc" in obj:
                            loc = obj["lsd_saved_world_loc"]
                            obj.matrix_world.translation = (loc[0], loc[1], loc[2])
                            del obj["lsd_saved_world_loc"]
                        obj.constraints.remove(con)
                        if "lsd_is_dimension_hook" in obj:
                            del obj["lsd_is_dimension_hook"]
                        obj.update_tag()
                    elif con.target and con.target.get("lsd_is_dimension_anchor") == "MASTER":
                        obj["lsd_saved_world_loc"] = list(obj.matrix_world.translation)
    finally:
        _in_depsgraph_handler = False

@persistent
def lsd_standalone_offset_sync(scene, depsgraph=None):
    global _in_depsgraph_handler
    if not depsgraph: return
    _in_depsgraph_handler = True
    try:
        # Check if we need to update anything (any object changed)
        if not depsgraph.updates: return
        
        # Always evaluate all standalone offset lines in the scene
        # because moving a hook does not always put the offset line into depsgraph.updates.
        for obj in scene.objects:
            if getattr(obj, "lsd_is_standalone_offset", False) or "Standalone_Offset_Line" in obj.name:
                target = getattr(obj, "lsd_standalone_mimic_target", None)
                if target:
                    try:
                        host = get_dimension_host(target)
                        if host:
                            target_thick = host.lsd_pg_dim_props.line_thickness
                            if abs(obj.lsd_standalone_thickness - target_thick) > 0.0001:
                                obj.lsd_standalone_thickness = target_thick
                    except: pass
                thick = obj.lsd_standalone_thickness
                if abs(obj.scale.x - thick) > 0.0001 or abs(obj.scale.y - thick) > 0.0001:
                    obj.scale.x = thick
                    obj.scale.y = thick
                    obj.update_tag()
                    
                # Dynamic Z-Scale (Length) calculation
                track_con = next((c for c in obj.constraints if c.type == 'TRACK_TO'), None)
                if track_con and track_con.target:
                    # Calculate distance between object location and target location in world space
                    p1 = obj.matrix_world.translation
                    p2 = track_con.target.matrix_world.translation
                    dist = (p2 - p1).length
                    # The mesh itself is 1m long, so scale = distance
                    if abs(obj.scale.z - dist) > 0.0001:
                        obj.scale.z = dist
                        obj.update_tag()
    finally:
        _in_depsgraph_handler = False

def update_offset_anchors_edit_mode(self, context):
    """
    Callback for lsd_edit_offset_anchors toggle.
    When True: Temporarily unparents objects constrained to offset anchors so anchors can be moved freely.
    When False: Restores the parent-child relationships securely.
    """
    scene = context.scene
    is_editing = scene.lsd_edit_offset_anchors
    
    if is_editing:
        # Enable Edit Mode: Unparent children of hooks and store the relationship
        for obj in context.scene.objects:
            # Skip the offset line mesh itself so it still follows the hook!
            if getattr(obj, "lsd_is_standalone_offset", False) or "Standalone_Offset_Line" in obj.name:
                continue
            parent = obj.parent
            if parent and (parent.get("lsd_is_offset_hook") or "Offset_EndHook" in parent.name or "StartHook" in parent.name):
                # Store the hook's name
                obj["lsd_temp_hook_parent"] = parent.name
                # Unparent while keeping world transform
                mw = obj.matrix_world.copy()
                obj.parent = None
                obj.matrix_world = mw
    else:
        # Disable Edit Mode: Restore parent relationships
        for obj in context.scene.objects:
            if "lsd_temp_hook_parent" in obj:
                hook_name = obj["lsd_temp_hook_parent"]
                hook = context.scene.objects.get(hook_name)
                if hook:
                    mw = obj.matrix_world.copy()
                    obj.parent = hook
                    obj.matrix_parent_inverse = hook.matrix_world.inverted()
                    obj.matrix_world = mw
                # Clean up the custom property
                del obj["lsd_temp_hook_parent"]

_lsd_arrow_prev_mode = 'OBJECT'
_lsd_arrow_prev_active = None

import bpy
@bpy.app.handlers.persistent
def lsd_onion_arrow_selection_handler(scene, depsgraph):
    """Detects selection of an Onion Skin Arrow and instantly scrubs the timeline."""
    global _lsd_arrow_prev_mode, _lsd_arrow_prev_active
    active = bpy.context.active_object
    
    if not active or "lsd_onion_frame_target" not in active:
        if active:
            _lsd_arrow_prev_mode = bpy.context.mode
            _lsd_arrow_prev_active = active
        return
    
    if active.select_get():
        target_frame = active["lsd_onion_frame_target"]
        
        # Deselect arrow instantly
        active.select_set(False)
        
        # Restore previous active object
        if _lsd_arrow_prev_active and _lsd_arrow_prev_active.name in scene.objects:
            bpy.context.view_layer.objects.active = _lsd_arrow_prev_active
            _lsd_arrow_prev_active.select_set(True)
            
            # Restore previous mode safely
            try:
                if _lsd_arrow_prev_mode != bpy.context.mode:
                    bpy.ops.object.mode_set(mode=_lsd_arrow_prev_mode)
            except Exception:
                pass
        else:
            bpy.context.view_layer.objects.active = None
            
        # Jump timeline
        if scene.frame_current != target_frame:
            scene.frame_set(target_frame)
            
import time

CLASSES = [
    LSD_OT_Core_DisablePanel, LSD_OT_Core_SnapCursorToActive
]

_lsd_previous_mode = 'OBJECT'

@bpy.app.handlers.persistent
def lsd_edit_mode_bake_handler(scene, depsgraph):
    """
    Detects transition from Object Mode to Edit Mode. If the active object
    has dimension hook modifiers, it temporarily switches back to Object Mode,
    bakes the modifiers, and returns to Edit Mode. This prevents the 'cage'
    from appearing un-deformed.
    """
    global _lsd_previous_mode
    if not bpy.context: return
    current_mode = getattr(bpy.context, 'mode', 'OBJECT')
    
    if current_mode == 'EDIT_MESH' and _lsd_previous_mode != 'EDIT_MESH':
        _lsd_previous_mode = current_mode
        obj = bpy.context.active_object
        if not obj or obj.type != 'MESH':
            return
            
        if obj.get("lsd_is_baking_edit", False):
            return

        has_dim_hook = False
        for mod in obj.modifiers:
            if mod.type == 'HOOK' and mod.object:
                if mod.object.name.startswith("Hook_Dim") or mod.object.get("lsd_anchor"):
                    has_dim_hook = True
                    break
                    
        if not has_dim_hook:
            return
            
        obj["lsd_is_baking_edit"] = True
        
        def deferred_bake():
            try:
                # Re-verify context
                if bpy.context.active_object != obj:
                    return None
                    
                # Temporarily switch back to OBJECT mode to apply modifiers
                if bpy.context.mode != 'OBJECT':
                    bpy.ops.object.mode_set(mode='OBJECT')
                    
                mods_to_apply = []
                for mod in obj.modifiers:
                    if mod.type == 'HOOK' and mod.object:
                        if mod.object.name.startswith("Hook_Dim") or mod.object.get("lsd_anchor"):
                            mods_to_apply.append(mod.name)
                
                for mod_name in mods_to_apply:
                    try:
                        bpy.ops.object.modifier_apply(modifier=mod_name)
                    except:
                        pass
                        
            except Exception as e:
                print(f"[LSD] Edit-Mode Auto-Bake Failed: {e}")
            finally:
                try:
                    # Return to EDIT mode to seamlessly continue the workflow
                    if bpy.context.mode != 'EDIT':
                        bpy.ops.object.mode_set(mode='EDIT')
                    obj["lsd_is_baking_edit"] = False
                except:
                    pass
            return None
            
        bpy.app.timers.register(deferred_bake, first_interval=0.01)
    elif current_mode != 'EDIT_MESH':
        _lsd_previous_mode = current_mode

def register():
    # 1. Register Classes
    for cls in CLASSES:
        if hasattr(cls, 'bl_rna'):
            try:
                bpy.utils.register_class(cls)
            except Exception:
                pass
    # 2. Append Handlers (Set-like behavior to prevent duplicates)
    if lsd_edit_mode_bake_handler not in bpy.app.handlers.depsgraph_update_post: bpy.app.handlers.depsgraph_update_post.append(lsd_edit_mode_bake_handler)
    if sync_light_props_handler not in bpy.app.handlers.depsgraph_update_post: bpy.app.handlers.depsgraph_update_post.append(sync_light_props_handler)
    if lsd_placement_handler not in bpy.app.handlers.depsgraph_update_post: bpy.app.handlers.depsgraph_update_post.append(lsd_placement_handler)
    if auto_set_active_rig_handler not in bpy.app.handlers.load_post: bpy.app.handlers.load_post.append(auto_set_active_rig_handler)
    if load_panel_order_handler not in bpy.app.handlers.load_post: bpy.app.handlers.load_post.append(load_panel_order_handler)

    if active_bone_change_handler not in bpy.app.handlers.depsgraph_update_post: bpy.app.handlers.depsgraph_update_post.append(active_bone_change_handler)
    if local_cursor_depsgraph_handler not in bpy.app.handlers.depsgraph_update_post: bpy.app.handlers.depsgraph_update_post.append(local_cursor_depsgraph_handler)
    if lsd_dimension_sync_handler not in bpy.app.handlers.depsgraph_update_post: bpy.app.handlers.depsgraph_update_post.append(lsd_dimension_sync_handler)
    if lsd_dimension_hook_cleanup_handler not in bpy.app.handlers.depsgraph_update_post: bpy.app.handlers.depsgraph_update_post.append(lsd_dimension_hook_cleanup_handler)
    if lsd_standalone_offset_sync not in bpy.app.handlers.depsgraph_update_post: bpy.app.handlers.depsgraph_update_post.append(lsd_standalone_offset_sync)
    if lsd_onion_arrow_selection_handler not in bpy.app.handlers.depsgraph_update_post: bpy.app.handlers.depsgraph_update_post.append(lsd_onion_arrow_selection_handler)
    
    # Lambda with context safety
    def safe_dimension_update(dummy):
        if bpy.context and bpy.context.scene:
            update_dimensions(bpy.context.scene)
    if safe_dimension_update not in bpy.app.handlers.load_post: bpy.app.handlers.load_post.append(safe_dimension_update)

    # 3. Timers
    if not getattr(bpy.types.Scene, "lsd_dim_color_refresh_timer", None):
        if not bpy.app.timers.is_registered(_color_refresh_loop):
            bpy.app.timers.register(_color_refresh_loop, first_interval=1.0)
    
def unregister():
    # 1. Unregister Classes
    for cls in reversed(CLASSES):
        if hasattr(cls, 'bl_rna'):
            try:
                bpy.utils.unregister_class(cls)
            except Exception:
                pass
                
    # 2. Remove Handlers safely
    if lsd_edit_mode_bake_handler in bpy.app.handlers.depsgraph_update_post: bpy.app.handlers.depsgraph_update_post.remove(lsd_edit_mode_bake_handler)
    if sync_light_props_handler in bpy.app.handlers.depsgraph_update_post: bpy.app.handlers.depsgraph_update_post.remove(sync_light_props_handler)
    if lsd_placement_handler in bpy.app.handlers.depsgraph_update_post: bpy.app.handlers.depsgraph_update_post.remove(lsd_placement_handler)
    if auto_set_active_rig_handler in bpy.app.handlers.load_post: bpy.app.handlers.load_post.remove(auto_set_active_rig_handler)
    if load_panel_order_handler in bpy.app.handlers.load_post: bpy.app.handlers.load_post.remove(load_panel_order_handler)

    if active_bone_change_handler in bpy.app.handlers.depsgraph_update_post: bpy.app.handlers.depsgraph_update_post.remove(active_bone_change_handler)
    if local_cursor_depsgraph_handler in bpy.app.handlers.depsgraph_update_post: bpy.app.handlers.depsgraph_update_post.remove(local_cursor_depsgraph_handler)
    if lsd_dimension_sync_handler in bpy.app.handlers.depsgraph_update_post: bpy.app.handlers.depsgraph_update_post.remove(lsd_dimension_sync_handler)
    if lsd_dimension_hook_cleanup_handler in bpy.app.handlers.depsgraph_update_post: bpy.app.handlers.depsgraph_update_post.remove(lsd_dimension_hook_cleanup_handler)
    if lsd_standalone_offset_sync in bpy.app.handlers.depsgraph_update_post: bpy.app.handlers.depsgraph_update_post.remove(lsd_standalone_offset_sync)
    if lsd_onion_arrow_selection_handler in bpy.app.handlers.depsgraph_update_post: bpy.app.handlers.depsgraph_update_post.remove(lsd_onion_arrow_selection_handler)

    if _color_refresh_timer_active:
        toggle_color_refresh_timer(0.0)
