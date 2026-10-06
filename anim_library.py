import bpy
import os
import shutil
import glob
import bpy.utils.previews

# Global dictionary to store preview collections
preview_collections = {}

# Global variable to track the current animation frame for the UI
_preview_frame_index = 0

def get_library_path():
    """Returns the absolute path to the project-specific or custom anim_library directory."""
    settings = None
    try:
        settings = getattr(bpy.context.scene, 'lsd_anim_settings', None)
    except AttributeError:
        # Context is restricted during Addon Registration, fallback to default path
        pass
        
    if settings and settings.custom_library_path and os.path.exists(bpy.path.abspath(settings.custom_library_path)):
        return bpy.path.abspath(settings.custom_library_path)
        
    try:
        blend_path = bpy.data.filepath
    except AttributeError:
        blend_path = ""
        
    if not blend_path:
        import tempfile
        base_dir = os.path.join(tempfile.gettempdir(), "LSD_Unsaved_Projects")
    else:
        base_dir = os.path.dirname(blend_path)
        
    lib_path = os.path.join(base_dir, "anim_library")
    if not os.path.exists(lib_path):
        os.makedirs(lib_path)
    return lib_path

def get_preview_dir(layer_name):
    """Returns the path to a specific layer's preview image sequence."""
    lib_path = get_library_path()
    preview_dir = os.path.join(lib_path, f"{layer_name}_preview")
    if not os.path.exists(preview_dir):
        os.makedirs(preview_dir)
    return preview_dir

def _delayed_remove_pcoll(pcoll):
    try:
        bpy.utils.previews.remove(pcoll)
    except:
        pass
    return None

def load_previews():
    """Scans the anim_library and loads all image sequences into bpy.utils.previews."""
    global preview_collections
    
    # Safely clear existing previews by deferring deletion so the UI doesn't crash while redrawing
    for pcoll in preview_collections.values():
        bpy.app.timers.register(lambda p=pcoll: _delayed_remove_pcoll(p), first_interval=2.0)
    preview_collections.clear()
    
    pcoll = bpy.utils.previews.new()
    preview_collections["main"] = pcoll
    
    lib_path = get_library_path()
    
    # Scan for directories ending in _preview
    for item in os.listdir(lib_path):
        item_path = os.path.join(lib_path, item)
        if os.path.isdir(item_path) and item.endswith("_preview"):
            layer_name = item.replace("_preview", "")
            
            # Find all PNGs in this directory
            pngs = glob.glob(os.path.join(item_path, "*.png"))
            pngs.sort()
            
            # Load each PNG into the preview collection with a numbered key
            for i, png_path in enumerate(pngs):
                icon_name = f"{layer_name}_{i}"
                if icon_name not in pcoll:
                    pcoll.load(icon_name, png_path, 'IMAGE')
                    
            # Also store the total frame count for this layer as a custom property
            setattr(pcoll, f"{layer_name}_count", len(pngs))
            
            # Load metadata for time-based playback
            import json
            meta_path = os.path.join(item_path, "meta.json")
            if os.path.exists(meta_path):
                try:
                    with open(meta_path, "r") as f:
                        meta = json.load(f)
                        setattr(pcoll, f"{layer_name}_meta", meta)
                except:
                    pass
            
def get_animated_icon_id(layer_name):
    """Returns the icon_id for the current frame of the animation loop based on real-time duration."""
    import time
    pcoll = preview_collections.get("main")
    if not pcoll: return 0
    
    count = getattr(pcoll, f"{layer_name}_count", 0)
    if count == 0: return 0
    
    # Time-based playback based on captured metadata
    meta = getattr(pcoll, f"{layer_name}_meta", None)
    if meta and "fps" in meta and "frame_range" in meta:
        fps = float(meta["fps"])
        frame_range = float(meta["frame_range"])
        duration = frame_range / max(1.0, fps)
        if duration <= 0: duration = 1.0
        
        time_elapsed = time.time() % duration
        progress = time_elapsed / duration
        frame = int(progress * count)
        frame = min(max(0, frame), count - 1)
    else:
        # Fallback to old global index
        global _preview_frame_index
        frame = _preview_frame_index % count
        
    icon_name = f"{layer_name}_{frame}"
    
    if icon_name in pcoll:
        return pcoll[icon_name].icon_id
    return 0

def preview_timer_update():
    """Timer callback to increment the preview frame and force UI redraws."""
    global _preview_frame_index
    _preview_frame_index += 1
    
    # Tag 3D View and Properties UI for redraw to animate the icon
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type in {'VIEW_3D', 'PROPERTIES', 'NLA_EDITOR'}:
                area.tag_redraw()
                
    # Run the UI redraw loop constantly at ~30 FPS to allow time.time() to perfectly match speed
    return 1.0 / 30.0

from bpy_extras.io_utils import ImportHelper

class LSD_OT_Anim_Library_Select_Dir(bpy.types.Operator):
    bl_idname = "lsd.anim_library_select_dir"
    bl_label = "Select Animation Layer Folder"
    
    directory: bpy.props.StringProperty(
        name="Folder Path",
        subtype='DIR_PATH',
        default=""
    )
    
    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}
        
    def execute(self, context):
        settings = context.scene.lsd_anim_settings
        dir_path = self.directory
        if not os.path.isdir(dir_path):
            dir_path = os.path.dirname(dir_path)
            
        settings.custom_library_path = dir_path
        bpy.ops.lsd.anim_library_refresh()
        return {'FINISHED'}

class LSD_OT_Anim_Library_Refresh(bpy.types.Operator):
    bl_idname = "lsd.anim_library_refresh"
    bl_label = "Refresh Library"
    
    def execute(self, context):
        load_previews()
        
        settings = context.scene.lsd_anim_settings
        settings.library_items.clear()
        
        lib_path = get_library_path()
        for item in os.listdir(lib_path):
            if item.endswith(".blend"):
                layer_name = item.replace(".blend", "")
                if os.path.isdir(os.path.join(lib_path, f"{layer_name}_preview")):
                    new_item = settings.library_items.add()
                    new_item.name = layer_name
                    new_item.filepath = os.path.join(lib_path, item)
                    
                    import json
                    meta_filepath = os.path.join(lib_path, f"{layer_name}_preview", "meta.json")
                    if os.path.exists(meta_filepath):
                        try:
                            with open(meta_filepath, 'r') as f:
                                meta = json.load(f)
                                new_item.target_type = meta.get("target_type", "")
                                new_item.target_name = meta.get("target_name", "")
                        except: pass
                
        return {'FINISHED'}

class LSD_OT_Anim_Library_Delete(bpy.types.Operator):
    bl_idname = "lsd.anim_library_delete"
    bl_label = "Delete Layer"
    bl_description = "Delete the selected animation layer from the library"
    
    @classmethod
    def poll(cls, context):
        settings = context.scene.lsd_anim_settings
        return len(settings.library_items) > 0

    def execute(self, context):
        settings = context.scene.lsd_anim_settings
        if not settings.library_items or settings.active_library_index >= len(settings.library_items):
            return {'CANCELLED'}
            
        item = settings.library_items[settings.active_library_index]
        layer_name = item.name
        lib_path = get_library_path()
        
        blend_file = os.path.join(lib_path, f"{layer_name}.blend")
        preview_dir = os.path.join(lib_path, f"{layer_name}_preview")
        
        try:
            import shutil
            if os.path.exists(blend_file):
                os.remove(blend_file)
            if os.path.exists(preview_dir):
                shutil.rmtree(preview_dir)
        except Exception as e:
            self.report({'ERROR'}, f"Failed to delete files: {e}")
            return {'CANCELLED'}
            
        bpy.ops.lsd.anim_library_refresh()
        
        if settings.active_library_index >= len(settings.library_items):
            settings.active_library_index = max(0, len(settings.library_items) - 1)
            
        return {'FINISHED'}

def _render_preview_sequence(context, obj, track, action, preview_dir, start_f=None, end_f=None):
    """Helper to render the final viewport preview sequence without muting other layers."""
    # We DO NOT mute other tracks because the user wants to see the exact composite animation as displayed in the viewport!
    context.view_layer.update()
                
    orig_filepath = context.scene.render.filepath
    orig_format = context.scene.render.image_settings.file_format
    orig_res_x = context.scene.render.resolution_x
    orig_res_y = context.scene.render.resolution_y
    
    try:
        # Clear out any old PNG files from previous captures to avoid ghost frames
        import glob
        old_pngs = glob.glob(os.path.join(preview_dir, "*.png"))
        for p in old_pngs:
            try: os.remove(p)
            except: pass
            
        context.scene.render.image_settings.file_format = 'PNG'
        context.scene.render.resolution_x = 200
        context.scene.render.resolution_y = 200
        
        # Calculate frame boundaries if not explicitly provided
        if start_f is None or end_f is None:
            if action:
                s_f = float('inf')
                e_f = float('-inf')
                try:
                    from . import anim_core
                    fcs = anim_core.get_action_fcurves(obj, action)
                    if fcs:
                        for fc in fcs:
                            if fc.keyframe_points:
                                s_f = min(s_f, fc.keyframe_points[0].co.x)
                                e_f = max(e_f, fc.keyframe_points[-1].co.x)
                except:
                    pass
                    
                if s_f == float('inf'):
                    s_f = action.frame_range[0]
                    e_f = action.frame_range[1]
                    
                if track and track.strips:
                    strip = track.strips[0]
                    offset = strip.frame_start - (strip.action_frame_start * strip.scale)
                    start_f = (s_f * strip.scale) + offset
                    end_f = (e_f * strip.scale) + offset
                else:
                    start_f = s_f
                    end_f = e_f
            else:
                start_f = context.scene.frame_start
                end_f = context.scene.frame_end
            
        frame_range = max(1, int(end_f - start_f))
        
        # Calculate step based on capture interval and scene FPS
        try:
            settings = getattr(context.scene, 'lsd_anim_settings', None)
            interval = float(settings.preview_capture_interval) if settings else 0.5
            if interval <= 0.001:
                interval = 0.5
        except:
            interval = 0.5
            
        fps = context.scene.render.fps or 24
        if fps < 12:
            fps = 24
        
        step = max(1, int(round(interval * fps)))
        
        # Absolute failsafe: Prevent Blender from rendering more than 120 PNGs total
        max_capture_frames = 120
        expected_captures = frame_range / step
        if expected_captures > max_capture_frames:
            import math
            step = max(1, math.ceil(frame_range / max_capture_frames))
            
        import json
        meta_path = os.path.join(preview_dir, "meta.json")
        
        meta = {}
        try:
            if os.path.exists(meta_path):
                with open(meta_path, "r") as f:
                    meta = json.load(f)
        except: pass
        
        meta["fps"] = fps
        meta["frame_range"] = frame_range
        meta["step"] = step
        
        try:
            with open(meta_path, "w") as f:
                json.dump(meta, f)
        except:
            pass
            
        orig_frame = context.scene.frame_current
        
        img_idx = 0
        for f in range(int(start_f), int(end_f) + 1, step):
            context.scene.frame_set(f)
            context.scene.render.filepath = os.path.join(preview_dir, f"prev_{img_idx:04d}.png")
            bpy.ops.render.opengl(write_still=True, view_context=True)
            img_idx += 1
            
        context.scene.frame_set(orig_frame)
    finally:
        context.scene.render.filepath = orig_filepath
        context.scene.render.image_settings.file_format = orig_format
        context.scene.render.resolution_x = orig_res_x
        context.scene.render.resolution_y = orig_res_y

class LSD_OT_Anim_Library_Update_Preview(bpy.types.Operator):
    bl_idname = "lsd.anim_library_update_preview"
    bl_label = "Update Preview"
    bl_description = "Recapture the animated preview for the selected library item based on the active scene layer"
    
    def execute(self, context):
        settings = context.scene.lsd_anim_settings
        if not settings.library_items or settings.active_library_index >= len(settings.library_items):
            self.report({'ERROR'}, "No library item selected")
            return {'CANCELLED'}
            
        item = settings.library_items[settings.active_library_index]
        obj = context.active_object
        
        if not obj or not obj.animation_data:
            self.report({'ERROR'}, "No object/animation data selected")
            return {'CANCELLED'}
            
        layer_data = obj.lsd_anim_layers_data
        
        if layer_data.active_layer_index < 0 or layer_data.active_layer_index >= len(layer_data.layers):
            self.report({'ERROR'}, "No active layer selected")
            return {'CANCELLED'}
            
        layer = layer_data.layers[layer_data.active_layer_index]
        track = obj.animation_data.nla_tracks.get(layer.track_name if layer.track_name else layer.name)
        if not track or not track.strips:
            action = obj.animation_data.action
            if not action:
                self.report({'ERROR'}, "No active animation found to preview")
                return {'CANCELLED'}
        else:
            action = track.strips[0].action
            
        preview_dir = get_preview_dir(item.name)
        _render_preview_sequence(context, obj, track, action, preview_dir)
        
        bpy.ops.lsd.anim_library_refresh()
        self.report({'INFO'}, f"Updated preview for {item.name}")
        return {'FINISHED'}

class LSD_OT_Anim_Library_Export(bpy.types.Operator):
    bl_idname = "lsd.anim_library_export"
    bl_label = "Export Active Layer"
    
    def execute(self, context):
        import layouts_systems_draftsman_toolkit.anim_core as anim_core
        obj = anim_core.get_active_object(context)
        
        if not obj or not hasattr(obj, 'lsd_anim_layers_data'):
            return {'CANCELLED'}
            
        layer_data = obj.lsd_anim_layers_data
        
        if layer_data.active_layer_index < 0 or layer_data.active_layer_index >= len(layer_data.layers):
            self.report({'ERROR'}, "No active layer to export")
            return {'CANCELLED'}
            
        layer = layer_data.layers[layer_data.active_layer_index]
        
        if not obj or not obj.animation_data:
            self.report({'ERROR'}, "No object/animation data selected")
            return {'CANCELLED'}
            
        # Get action from the track
        track = obj.animation_data.nla_tracks.get(layer.track_name if layer.track_name else layer.name)
        if not track or not track.strips:
            action = obj.animation_data.action
            if not action:
                self.report({'ERROR'}, "No animation data found on this layer")
                return {'CANCELLED'}
        else:
            action = track.strips[0].action
            
        settings = context.scene.lsd_anim_settings
        
        # Determine the timeline frame range to capture from the target layer (or selection)
        start_f = float('inf')
        end_f = float('-inf')
        
        # Safely retrieve F-curves from the source action (supporting Blender 4.3+ / 5.x Slotted Actions)
        source_fcurves = anim_core.get_action_fcurves(obj, action)
        
        # Calculate the physical timeline frame range from action keyframes + strip transformation
        if getattr(settings, 'upload_selection', 'LAYER') == 'KEYFRAMES':
            sel_frames = set()
            try:
                for fc in source_fcurves:
                    for kp in fc.keyframe_points:
                        if kp.select_control_point:
                            sel_frames.add(kp.co.x)
            except: pass
            
            if sel_frames:
                s_action_f = min(sel_frames)
                e_action_f = max(sel_frames)
                if track and track.strips:
                    strip = track.strips[0]
                    offset = strip.frame_start - (strip.action_frame_start * strip.scale)
                    start_f = int(round((s_action_f * strip.scale) + offset))
                    end_f = int(round((e_action_f * strip.scale) + offset))
                else:
                    start_f = int(round(s_action_f))
                    end_f = int(round(e_action_f))
            else:
                self.report({'WARNING'}, "No keyframes selected in timeline. Exporting entire layer range.")
                
        if start_f == float('inf'):
            try:
                for fc in source_fcurves:
                    if fc.keyframe_points:
                        start_f = min(start_f, fc.keyframe_points[0].co.x)
                        end_f = max(end_f, fc.keyframe_points[-1].co.x)
            except: pass
            
            if start_f == float('inf'):
                start_f = context.scene.frame_start
                end_f = context.scene.frame_end
            else:
                if track and track.strips:
                    strip = track.strips[0]
                    offset = strip.frame_start - (strip.action_frame_start * strip.scale)
                    start_f = int(round((start_f * strip.scale) + offset))
                    end_f = int(round((end_f * strip.scale) + offset))
                else:
                    start_f = int(round(start_f))
                    end_f = int(round(end_f))
                    
        if end_f < start_f:
            end_f = start_f

        # Identify animated bones / objects referenced in this layer's action
        animated_bones = set()
        animated_object_paths = set()
        for fc in source_fcurves:
            if fc.data_path.startswith('pose.bones["'):
                bone_name = fc.data_path.split('pose.bones["')[1].split('"]')[0]
                animated_bones.add(bone_name)
            else:
                animated_object_paths.add(fc.data_path)

        # Bake capture the evaluated composite transforms strictly from start_f to end_f
        orig_current_frame = context.scene.frame_current
        baked_action = bpy.data.actions.new(name=f"{layer.name}_BakeCapture")
        
        # Helper to get or create an F-curve on baked_action regardless of Blender version (legacy vs Slotted Actions)
        def _get_or_create_fc(act, target_obj, data_path, index):
            # Try legacy root fcurves first if available
            if hasattr(act, "fcurves") and not hasattr(act, "slots"):
                fc = act.fcurves.find(data_path, index=index)
                if not fc:
                    fc = act.fcurves.new(data_path, index=index)
                return fc
            elif hasattr(act, "slots"):
                try:
                    slot_name = target_obj.id_data.name if hasattr(target_obj, 'id_data') else target_obj.name
                    slot = act.slots.get(slot_name)
                    if not slot:
                        slot = act.slots.new(name=slot_name)
                        
                    from bpy_extras import anim_utils
                    cb = anim_utils.action_get_channelbag_for_slot(act, slot)
                    if cb is None:
                        # Ensure channelbag exists for this slot
                        try:
                            cb = act.layers[0].channelbags.new(slot=slot)
                        except Exception:
                            cb = anim_utils.action_get_channelbag_for_slot(act, slot)
                            
                    if cb and hasattr(cb, "fcurves"):
                        fc = cb.fcurves.find(data_path, index=index)
                        if not fc:
                            fc = cb.fcurves.new(data_path, index=index)
                        return fc
                except Exception as e:
                    print(f"Error creating slotted fcurve for {data_path}[{index}]: {e}")
                    
            # Fallback if slots failed or fcurves is direct
            if hasattr(act, "fcurves"):
                fc = act.fcurves.find(data_path, index=index)
                if not fc:
                    fc = act.fcurves.new(data_path, index=index)
                return fc
            return None

        # Determine bake channels per animated target
        try:
            for f in range(start_f, end_f + 1):
                context.scene.frame_set(f)
                context.view_layer.update()
                
                # Relative frame index in the new action (starts at frame 1)
                action_frame = float(f - start_f + 1)
                
                if obj.type == 'ARMATURE' and animated_bones and obj.pose:
                    for bname in animated_bones:
                        pbone = obj.pose.bones.get(bname)
                        if not pbone: continue
                        
                        bone_prefix = f'pose.bones["{bname}"]'
                        
                        # Bake location
                        for idx, val in enumerate(pbone.location):
                            fc = _get_or_create_fc(baked_action, obj, f"{bone_prefix}.location", idx)
                            if fc: fc.keyframe_points.insert(action_frame, val, options={'FAST'})
                            
                        # Bake rotation
                        rot_mode = getattr(pbone, "rotation_mode", "QUATERNION")
                        if rot_mode == 'QUATERNION':
                            for idx, val in enumerate(pbone.rotation_quaternion):
                                fc = _get_or_create_fc(baked_action, obj, f"{bone_prefix}.rotation_quaternion", idx)
                                if fc: fc.keyframe_points.insert(action_frame, val, options={'FAST'})
                        elif rot_mode == 'AXIS_ANGLE':
                            for idx, val in enumerate(pbone.rotation_axis_angle):
                                fc = _get_or_create_fc(baked_action, obj, f"{bone_prefix}.rotation_axis_angle", idx)
                                if fc: fc.keyframe_points.insert(action_frame, val, options={'FAST'})
                        else:
                            for idx, val in enumerate(pbone.rotation_euler):
                                fc = _get_or_create_fc(baked_action, obj, f"{bone_prefix}.rotation_euler", idx)
                                if fc: fc.keyframe_points.insert(action_frame, val, options={'FAST'})
                                
                        # Bake scale
                        for idx, val in enumerate(pbone.scale):
                            fc = _get_or_create_fc(baked_action, obj, f"{bone_prefix}.scale", idx)
                            if fc: fc.keyframe_points.insert(action_frame, val, options={'FAST'})
                else:
                    # Object level transforms
                    for idx, val in enumerate(obj.location):
                        fc = _get_or_create_fc(baked_action, obj, "location", idx)
                        if fc: fc.keyframe_points.insert(action_frame, val, options={'FAST'})
                        
                    rot_mode = getattr(obj, "rotation_mode", "QUATERNION")
                    if rot_mode == 'QUATERNION':
                        for idx, val in enumerate(obj.rotation_quaternion):
                            fc = _get_or_create_fc(baked_action, obj, "rotation_quaternion", idx)
                            if fc: fc.keyframe_points.insert(action_frame, val, options={'FAST'})
                    elif rot_mode == 'AXIS_ANGLE':
                        for idx, val in enumerate(obj.rotation_axis_angle):
                            fc = _get_or_create_fc(baked_action, obj, "rotation_axis_angle", idx)
                            if fc: fc.keyframe_points.insert(action_frame, val, options={'FAST'})
                    else:
                        for idx, val in enumerate(obj.rotation_euler):
                            fc = _get_or_create_fc(baked_action, obj, "rotation_euler", idx)
                            if fc: fc.keyframe_points.insert(action_frame, val, options={'FAST'})
                            
                    for idx, val in enumerate(obj.scale):
                        fc = _get_or_create_fc(baked_action, obj, "scale", idx)
                        if fc: fc.keyframe_points.insert(action_frame, val, options={'FAST'})
                        
            for fc in anim_core.get_action_fcurves(obj, baked_action):
                fc.update()
        finally:
            context.scene.frame_set(orig_current_frame)
            
        export_action = baked_action
        export_action.use_fake_user = True
                
        lib_path = get_library_path()
        base_name = layer.name
        out_filepath = os.path.join(lib_path, f"{base_name}.blend")
        counter = 1
        while os.path.exists(out_filepath):
            base_name = f"{layer.name}_{counter:03d}"
            out_filepath = os.path.join(lib_path, f"{base_name}.blend")
            counter += 1
            
        layer.name = base_name
        export_action.name = base_name
        
        # Save action to blend file with fake_user=True
        bpy.data.libraries.write(out_filepath, {export_action}, fake_user=True)
        
        # Render preview images using OpenGL Viewport render of the full composite viewport
        preview_dir = get_preview_dir(layer.name)
        _render_preview_sequence(context, obj, track, action, preview_dir, start_f=start_f, end_f=end_f)
        
        # Save metadata without erasing fps/frame_range/step
        import json
        meta_filepath = os.path.join(preview_dir, "meta.json")
        
        meta = {}
        try:
            if os.path.exists(meta_filepath):
                with open(meta_filepath, 'r') as f:
                    meta = json.load(f)
        except: pass
        
        meta["target_type"] = obj.type
        meta["target_name"] = obj.name
        
        try:
            with open(meta_filepath, 'w') as f:
                json.dump(meta, f)
        except Exception as e:
            print(f"Failed to save metadata for library item: {e}")
            
        bpy.ops.lsd.anim_library_refresh()
        return {'FINISHED'}

class LSD_OT_Anim_Library_Import(bpy.types.Operator):
    bl_idname = "lsd.anim_library_import"
    bl_label = "Import Layer"
    
    def execute(self, context):
        settings = context.scene.lsd_anim_settings
        if not settings.library_items or settings.active_library_index >= len(settings.library_items):
            return {'CANCELLED'}
            
        item = settings.library_items[settings.active_library_index]
        
        # Append action
        with bpy.data.libraries.load(item.filepath) as (data_from, data_to):
            data_to.actions = data_from.actions
            
        if not data_to.actions:
            self.report({'ERROR'}, "No action found in library file")
            return {'CANCELLED'}
            
        action = data_to.actions[0]
        
        import layouts_systems_draftsman_toolkit.anim_core as anim_core
        obj = anim_core.get_active_object(context)
        if obj is None:
            with bpy.data.libraries.load(item.filepath) as (data_from_rig, data_to_rig):
                if data_from_rig.objects:
                    data_to_rig.objects = data_from_rig.objects
                    
            if hasattr(data_to_rig, 'objects') and data_to_rig.objects:
                imported_armature = None
                for new_obj in data_to_rig.objects:
                    if new_obj and new_obj.type == 'ARMATURE':
                        imported_armature = new_obj
                        break
                        
                if imported_armature:
                    context.scene.collection.objects.link(imported_armature)
                    for o in context.selected_objects:
                        o.select_set(False)
                    imported_armature.select_set(True)
                    context.view_layer.objects.active = imported_armature
                    imported_armature.location = context.scene.cursor.location
                    obj = imported_armature
                else:
                    self.report({'WARNING'}, "No target armature found or importable from library. Please select the target rig first.")
                    return {'CANCELLED'}
            else:
                self.report({'WARNING'}, "No target armature found or importable from library. Please select the target rig first.")
                return {'CANCELLED'}
        
        # Ensure slots match the active object so the poses evaluate correctly on different rigs
        if obj and hasattr(action, 'slots'):
            try:
                target_slot_name = obj.id_data.name if hasattr(obj, 'id_data') else obj.name
                for slot in action.slots:
                    slot.name = target_slot_name
            except Exception as e:
                print(f"Failed to rename imported action slot: {e}")
        
        # Calculate keyframe bounds
        try:
            from . import anim_core
            fcurves = anim_core.get_action_fcurves(obj, action)
        except Exception:
            fcurves = []
            
        min_frame = float('inf')
        max_frame = float('-inf')
        has_keys = False
        if fcurves:
            for fc in fcurves:
                if fc.keyframe_points:
                    has_keys = True
                    for kp in fc.keyframe_points:
                        if kp.co.x < min_frame:
                            min_frame = kp.co.x
                        if kp.co.x > max_frame:
                            max_frame = kp.co.x
                            
        if has_keys:
            # Shift all keyframes in the imported action so the animation starts at frame 1.0
            time_shift = 1.0 - min_frame
            if abs(time_shift) > 0.0001:
                for fc in fcurves:
                    for kp in fc.keyframe_points:
                        kp.co.x += time_shift
                        kp.handle_left.x += time_shift
                        kp.handle_right.x += time_shift
                    fc.update()
            
            # Action duration
            action_duration = max(1.0, max_frame - min_frame)
            min_frame = 1.0
            max_frame = 1.0 + action_duration
            
            # True Delta Engine Conversion
            import mathutils
            groups = {}
            for fc in fcurves:
                if fc.data_path not in groups:
                    groups[fc.data_path] = []
                groups[fc.data_path].append(fc)
                
            for data_path, fcs in groups.items():
                if "rotation_quaternion" in data_path and settings.import_blend_type in {'COMBINE', 'ADD'}:
                    # Complex Delta Conversion for Quaternions (only if combining/adding)
                    w_fc = next((f for f in fcs if f.array_index == 0), None)
                    x_fc = next((f for f in fcs if f.array_index == 1), None)
                    y_fc = next((f for f in fcs if f.array_index == 2), None)
                    z_fc = next((f for f in fcs if f.array_index == 3), None)
                    
                    if w_fc and x_fc and y_fc and z_fc:
                        frames = set()
                        for f in (w_fc, x_fc, y_fc, z_fc):
                            for kp in f.keyframe_points:
                                frames.add(kp.co.x)
                                
                        w_fc.extrapolation = 'CONSTANT'
                        x_fc.extrapolation = 'CONSTANT'
                        y_fc.extrapolation = 'CONSTANT'
                        z_fc.extrapolation = 'CONSTANT'
                        if frames:
                            local_min_frame = sorted(frames)[0]
                            base_w = w_fc.evaluate(local_min_frame)
                            base_x = x_fc.evaluate(local_min_frame)
                            base_y = y_fc.evaluate(local_min_frame)
                            base_z = z_fc.evaluate(local_min_frame)
                            base_q = mathutils.Quaternion((base_w, base_x, base_y, base_z))
                            try:
                                base_q_inv = base_q.inverted()
                            except ValueError:
                                base_q_inv = mathutils.Quaternion((1.0, 0.0, 0.0, 0.0))
                            
                            # Store evaluated deltas to avoid duplicate keyframe conflicts
                            deltas = []
                            prev_delta = None
                            for frame in sorted(frames):
                                curr_w = w_fc.evaluate(frame)
                                curr_x = x_fc.evaluate(frame)
                                curr_y = y_fc.evaluate(frame)
                                curr_z = z_fc.evaluate(frame)
                                curr_q = mathutils.Quaternion((curr_w, curr_x, curr_y, curr_z))
                                
                                # True 4D Delta calculation (Blender NLA Combine is base @ delta)
                                delta_q = base_q_inv @ curr_q
                                
                                # Ensure continuous hemisphere to prevent 180-degree flip/backtracking
                                if prev_delta is not None:
                                    if delta_q.dot(prev_delta) < 0:
                                        delta_q.negate()
                                prev_delta = delta_q.copy()
                                
                                deltas.append((frame, delta_q))
                                
                            # Clear old absolute keyframes to prevent Blender interpolation crashes
                            w_fc.keyframe_points.clear()
                            x_fc.keyframe_points.clear()
                            y_fc.keyframe_points.clear()
                            z_fc.keyframe_points.clear()
                            
                            # Insert clean delta keyframes
                            for frame, delta_q in deltas:
                                w_fc.keyframe_points.insert(frame, delta_q.w, options={'FAST'})
                                x_fc.keyframe_points.insert(frame, delta_q.x, options={'FAST'})
                                y_fc.keyframe_points.insert(frame, delta_q.y, options={'FAST'})
                                z_fc.keyframe_points.insert(frame, delta_q.z, options={'FAST'})
                                
                            # Force AUTO_CLAMPED on recreated quaternions to prevent flipping
                            for f in (w_fc, x_fc, y_fc, z_fc):
                                for kp in f.keyframe_points:
                                    kp.handle_left_type = 'AUTO_CLAMPED'
                                    kp.handle_right_type = 'AUTO_CLAMPED'
                                f.update()
                else:
                    for fc in fcs:
                        if not fc.keyframe_points:
                            continue
                            
                        fc.extrapolation = 'CONSTANT'
                        
                        base_val = 0.0
                        if "scale" in data_path:
                            base_val = 1.0
                        elif settings.import_blend_type == 'REPLACE':
                            try:
                                prop = obj.path_resolve(data_path)
                                if hasattr(prop, '__getitem__'):
                                    base_val = prop[fc.array_index]
                                else:
                                    base_val = prop
                            except:
                                base_val = 0.0
                                
                        action_start_val = fc.keyframe_points[0].co.y
                        
                        if settings.import_blend_type == 'ADD' and data_path.endswith("location"):
                            action_start_val = 0.0
                            
                        spatial_offset = base_val - action_start_val
                        
                        base_scale_factor = 1.0
                        scale_fcs = []
                        if settings.import_blend_type in {'COMBINE', 'ADD'} and data_path.endswith("location"):
                            scale_path = data_path.replace("location", "scale")
                            try:
                                scale_prop = obj.path_resolve(scale_path)
                                if hasattr(scale_prop, '__getitem__'):
                                    base_scale_factor = scale_prop[fc.array_index]
                                else:
                                    base_scale_factor = scale_prop
                            except:
                                pass
                                
                            if base_scale_factor == 0: base_scale_factor = 1.0
                            
                            # Find if the imported action ALSO animates scale
                            scale_fcs = [f for f in fcurves if f.data_path == scale_path and f.array_index == fc.array_index]
                        
                        for kp in fc.keyframe_points:
                            # Dynamically calculate the scale factor at this exact frame if the combine layer scales it
                            dyn_scale_factor = base_scale_factor
                            if scale_fcs:
                                dyn_scale_factor *= scale_fcs[0].evaluate(kp.co.x)
                                
                            kp.co.y = (kp.co.y + spatial_offset) * dyn_scale_factor
                            kp.handle_left.y = (kp.handle_left.y + spatial_offset) * dyn_scale_factor
                            kp.handle_right.y = (kp.handle_right.y + spatial_offset) * dyn_scale_factor
                            
                            kp.handle_left_type = 'AUTO_CLAMPED'
                            kp.handle_right_type = 'AUTO_CLAMPED'
                            
                        fc.update()
        
        # Create a new layer in our UI
        bpy.ops.lsd.anim_layer_add()
        
        # Apply chosen import blend mode
        layer_data = obj.lsd_anim_layers_data
        if layer_data.active_layer_index >= 0 and layer_data.active_layer_index < len(layer_data.layers):
            layer = layer_data.layers[layer_data.active_layer_index]
            layer.blend_type = settings.import_blend_type
            if obj and obj.animation_data:
                track = obj.animation_data.nla_tracks.get(layer.track_name)
                if track and track.strips:
                    track.strips[0].blend_type = settings.import_blend_type
                    track.strips[0].extrapolation = 'NOTHING' if settings.import_blend_type == 'REPLACE' else 'HOLD_FORWARD'
        
        # Assign action
        obj = anim_core.get_active_object(context)
        
        if obj and obj.animation_data:
            try:
                from . import anim_core
                # Step 1: Force exit tweak mode so the action isn't locked as read-only by the dependency graph!
                anim_core.invisible_tweakmode_swap(context, exit_first=True, enter_second=False)
                
                # Step 2: Swap the action on the newly created layer's NLA strip
                layer = layer_data.layers[-1]
                track = obj.animation_data.nla_tracks.get(layer.track_name)
                if track and track.strips:
                    strip = track.strips[0]
                    old_action = strip.action
                    
                    strip_name = strip.name
                    
                    import time
                    old_track = track
                    new_track = obj.animation_data.nla_tracks.new()
                    new_track.name = f"{old_track.name}_imported"
                    layer.track_name = new_track.name
                    
                    # Create the strip starting exactly at the current timeline frame
                    curr_frame = context.scene.frame_current
                    strip = new_track.strips.new(name=strip_name, start=int(curr_frame), action=action)
                    
                    old_track.name = f"DELETED_{old_track.name}_{int(time.time()*1000)}"
                    old_track.mute = True
                    for s in old_track.strips: s.mute = True
                    
                    track = new_track  # Reassign track so it gets properly renamed at the end
                    
                    def safe_delete():
                        try:
                            if old_action: bpy.data.actions.remove(old_action)
                            if obj.animation_data and old_track.name in obj.animation_data.nla_tracks:
                                obj.animation_data.nla_tracks.remove(old_track)
                        except: pass
                    bpy.app.timers.register(safe_delete, first_interval=2.0)
                    
                    if hasattr(strip, 'use_sync_length'):
                        strip.use_sync_length = False
                    try:
                        strip.action_frame_start = 1.0
                        strip.action_frame_end = max_frame if has_keys else 100.0
                    except: pass
                    strip.frame_start = curr_frame
                    strip.frame_end = curr_frame + (max_frame - 1.0 if has_keys else 100.0)
                    strip.scale = 1.0
                    
                    # Set zero blend-in to avoid fractional frame interpolation / backtracking / downward offsets
                    strip.blend_in = 0.0
                    strip.blend_out = 0.0
                        
                    strip.blend_type = settings.import_blend_type
                    layer.blend_type = settings.import_blend_type
                    strip.extrapolation = 'HOLD_FORWARD' if settings.import_blend_type in {'COMBINE', 'ADD'} else 'NOTHING'
                    
                    # (Action deletion is safely deferred via the background timer above)
                        
                base_name = item.name
                action_name = base_name
                existing_names = [l.name for l in layer_data.layers if l != layer]
                counter = 1
                while action_name in existing_names:
                    action_name = f"{base_name}.{counter:03d}"
                    counter += 1
                    
                layer.name = action_name
                layer.track_name = action_name
                if action:
                    action.name = action_name
                if track:
                    track.name = action_name
                    
                # Step 4: Re-enter tweak mode so the newly imported action is loaded into the Action Editor and actively keyed!
                anim_core.invisible_tweakmode_swap(context, exit_first=False, enter_second=True)
            except Exception as e:
                self.report({'ERROR'}, f"Failed to assign imported layer: {e}")
                
        bpy.ops.lsd.anim_library_refresh()
        return {'FINISHED'}

CLASSES = (
    LSD_OT_Anim_Library_Select_Dir,
    LSD_OT_Anim_Library_Refresh,
    LSD_OT_Anim_Library_Update_Preview,
    LSD_OT_Anim_Library_Export,
    LSD_OT_Anim_Library_Import,
    LSD_OT_Anim_Library_Delete,
)

def register():
    try:
        load_previews()
    except Exception as e:
        print(f"Failed to load anim library previews: {e}")
        
    try:
        if not bpy.app.timers.is_registered(preview_timer_update):
            bpy.app.timers.register(preview_timer_update)
    except: pass
    
    for cls in CLASSES:
        try:
            bpy.utils.register_class(cls)
        except Exception as e:
            print(f"Failed to register anim library class {cls}: {e}")

def unregister():
    try:
        if bpy.app.timers.is_registered(preview_timer_update):
            bpy.app.timers.unregister(preview_timer_update)
    except: pass
        
    try:
        for pcoll in preview_collections.values():
            bpy.utils.previews.remove(pcoll)
        preview_collections.clear()
    except: pass
    
    for cls in reversed(CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except: pass
