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
    bl_description = "Select folder for Animation Layer Library"
    
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
    bl_description = "Scan library folder and reload animation items and previews"
    
    def execute(self, context):
        load_previews()
        
        settings = context.scene.lsd_anim_settings
        settings.library_items.clear()
        
        lib_path = get_library_path()
        if os.path.exists(lib_path) and os.path.isdir(lib_path):
            for item in sorted(os.listdir(lib_path)):
                if item.endswith(".blend") and not item.startswith("."):
                    layer_name = item[:-6]
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
                        except Exception:
                            pass
                            
        count = len(settings.library_items)
        if count > 0:
            if settings.active_library_index >= count or settings.active_library_index < 0:
                settings.active_library_index = 0
            self.report({'INFO'}, f"Library refreshed: {count} item{'s' if count != 1 else ''} found")
        else:
            self.report({'INFO'}, "Library refreshed: No .blend files found in selected folder")
            
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
    bl_description = "Recapture the animated preview for the selected library item based on the active scene layer or action"
    
    def execute(self, context):
        import layouts_systems_draftsman_toolkit.anim_core as anim_core
        settings = context.scene.lsd_anim_settings
        if not settings.library_items:
            self.report({'WARNING'}, "No animation items in library to preview")
            return {'CANCELLED'}
            
        if settings.active_library_index < 0 or settings.active_library_index >= len(settings.library_items):
            settings.active_library_index = 0
            
        item = settings.library_items[settings.active_library_index]
        obj = anim_core.get_active_object(context)
        if not obj:
            obj = context.active_object
        if not obj and context.selected_objects:
            obj = context.selected_objects[0]
            
        if not obj:
            self.report({'ERROR'}, "Please select an object in the 3D Viewport to render preview")
            return {'CANCELLED'}
            
        layer_data = getattr(obj, 'lsd_anim_layers_data', None)
        layer = None
        track = None
        action = None
        
        if layer_data and len(layer_data.layers) > 0 and 0 <= layer_data.active_layer_index < len(layer_data.layers):
            layer = layer_data.layers[layer_data.active_layer_index]
            if obj.animation_data:
                track = obj.animation_data.nla_tracks.get(layer.track_name if layer.track_name else layer.name)
                if track and track.strips:
                    action = track.strips[0].action
                else:
                    action = obj.animation_data.action
                    
        if not action and obj.animation_data:
            if obj.animation_data.action:
                action = obj.animation_data.action
            elif obj.animation_data.nla_tracks:
                for t in obj.animation_data.nla_tracks:
                    if not t.mute and t.strips:
                        track = t
                        action = t.strips[0].action
                        break
                if not action:
                    for t in obj.animation_data.nla_tracks:
                        if t.strips:
                            track = t
                            action = t.strips[0].action
                            break
                            
        temp_loaded_action = None
        orig_action = None
        orig_action_slot = None
        
        if not action:
            # Fallback: Load the action from the selected library item's blend file
            if hasattr(item, 'filepath') and item.filepath and os.path.exists(item.filepath):
                try:
                    with bpy.data.libraries.load(item.filepath) as (data_from, data_to):
                        data_to.actions = data_from.actions
                    if data_to.actions:
                        temp_loaded_action = data_to.actions[0]
                        action = temp_loaded_action
                except Exception as e:
                    print(f"Failed to load action from library item file: {e}")
                    
        if not action:
            self.report({'ERROR'}, "No active animation found to preview")
            return {'CANCELLED'}
            
        if temp_loaded_action and obj:
            if not obj.animation_data:
                obj.animation_data_create()
            orig_action = obj.animation_data.action
            orig_action_slot = getattr(obj.animation_data, 'action_slot', None)
            try:
                obj.animation_data.action = temp_loaded_action
                anim_core.ensure_action_slot(obj, temp_loaded_action)
            except Exception:
                pass
                
        try:
            preview_dir = get_preview_dir(item.name)
            
            # If upload_selection is KEYFRAMES and user has keyframes selected in the active action, limit preview range
            start_f = None
            end_f = None
            if getattr(settings, 'upload_selection', 'LAYER') == 'KEYFRAMES' and not temp_loaded_action:
                try:
                    source_fcurves = anim_core.get_action_fcurves(obj, action)
                    sel_frames = set()
                    for fc in source_fcurves:
                        for kp in fc.keyframe_points:
                            if getattr(kp, 'select_control_point', False) or getattr(kp, 'select_left_handle', False) or getattr(kp, 'select_right_handle', False):
                                sel_frames.add(kp.co.x)
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
                except Exception:
                    pass
                    
            _render_preview_sequence(context, obj, track, action, preview_dir, start_f=start_f, end_f=end_f)
        finally:
            if temp_loaded_action:
                if obj and obj.animation_data:
                    try:
                        obj.animation_data.action = orig_action
                        if orig_action_slot:
                            obj.animation_data.action_slot = orig_action_slot
                    except Exception:
                        pass
                try:
                    bpy.data.actions.remove(temp_loaded_action)
                except Exception:
                    pass
                    
        bpy.ops.lsd.anim_library_refresh()
        self.report({'INFO'}, f"Updated preview for {item.name}")
        return {'FINISHED'}

class LSD_OT_Anim_Library_Export(bpy.types.Operator):
    bl_idname = "lsd.anim_library_export"
    bl_label = "Export Active Layer"
    bl_description = "Export active animation layer or selected keyframes to the Animation Layer Library"
    
    def execute(self, context):
        import layouts_systems_draftsman_toolkit.anim_core as anim_core
        obj = anim_core.get_active_object(context)
        if not obj:
            obj = context.active_object
        if not obj and context.selected_objects:
            obj = context.selected_objects[0]
            
        if not obj:
            self.report({'ERROR'}, "No active object selected")
            return {'CANCELLED'}
            
        if not obj.animation_data:
            self.report({'ERROR'}, "No animation data found on selected object")
            return {'CANCELLED'}
            
        layer_data = getattr(obj, 'lsd_anim_layers_data', None)
        layer = None
        track = None
        action = None
        
        if layer_data and len(layer_data.layers) > 0 and 0 <= layer_data.active_layer_index < len(layer_data.layers):
            layer = layer_data.layers[layer_data.active_layer_index]
            track = obj.animation_data.nla_tracks.get(layer.track_name if layer.track_name else layer.name)
            if track and track.strips:
                action = track.strips[0].action
            else:
                action = obj.animation_data.action
                
        if not action:
            if obj.animation_data.action:
                action = obj.animation_data.action
            elif obj.animation_data.nla_tracks:
                for t in obj.animation_data.nla_tracks:
                    if not t.mute and t.strips:
                        track = t
                        action = t.strips[0].action
                        break
                if not action:
                    for t in obj.animation_data.nla_tracks:
                        if t.strips:
                            track = t
                            action = t.strips[0].action
                            break
                            
        if not action:
            self.report({'ERROR'}, "No active animation or action found to export")
            return {'CANCELLED'}
            
        settings = context.scene.lsd_anim_settings
        
        # Determine the timeline frame range to capture from the target layer (or selection)
        start_f = float('inf')
        end_f = float('-inf')
        
        # Safely retrieve F-curves from the source action (supporting Blender 4.3+ / 5.x Slotted Actions)
        source_fcurves = anim_core.get_action_fcurves(obj, action)
        
        # Calculate the physical timeline frame range from action keyframes + strip transformation
        sel_frames = set()
        if getattr(settings, 'upload_selection', 'LAYER') == 'KEYFRAMES':
            try:
                for fc in source_fcurves:
                    for kp in fc.keyframe_points:
                        if getattr(kp, 'select_control_point', False) or getattr(kp, 'select_left_handle', False) or getattr(kp, 'select_right_handle', False):
                            sel_frames.add(kp.co.x)
            except Exception:
                pass
            
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
                self.report({'WARNING'}, "No keyframes selected in timeline. Exporting entire animation range.")
                
        if start_f == float('inf'):
            try:
                for fc in source_fcurves:
                    if fc.keyframe_points:
                        start_f = min(start_f, fc.keyframe_points[0].co.x)
                        end_f = max(end_f, fc.keyframe_points[-1].co.x)
            except Exception:
                pass
            
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

        # Clone the layer's action with 100% fidelity (all slots, channelbags, fcurves, handles, and keyframes)
        export_action = action.copy()
        
        # If user selected KEYFRAMES only, cull unselected keyframe points
        if getattr(settings, 'upload_selection', 'LAYER') == 'KEYFRAMES' and sel_frames:
            for exp_fc in anim_core.get_action_fcurves(obj, export_action):
                for i in range(len(exp_fc.keyframe_points) - 1, -1, -1):
                    kp = exp_fc.keyframe_points[i]
                    is_sel = getattr(kp, 'select_control_point', False) or getattr(kp, 'select_left_handle', False) or getattr(kp, 'select_right_handle', False) or (kp.co.x in sel_frames)
                    if not is_sel:
                        exp_fc.keyframe_points.remove(kp)
                exp_fc.update()
                
        # Normalize time so the exported action begins at frame 1.0
        exp_fcurves = anim_core.get_action_fcurves(obj, export_action)
        exp_min_frame = float('inf')
        for fc in exp_fcurves:
            for kp in fc.keyframe_points:
                if kp.co.x < exp_min_frame:
                    exp_min_frame = kp.co.x
                    
        if exp_min_frame != float('inf') and abs(exp_min_frame - 1.0) > 0.0001:
            time_shift = 1.0 - exp_min_frame
            for fc in exp_fcurves:
                for kp in fc.keyframe_points:
                    kp.co.x += time_shift
                    kp.handle_left.x += time_shift
                    kp.handle_right.x += time_shift
                fc.update()
                
        # Account for NLA strip scale if user stretched or compressed the strip
        if track and track.strips and abs(track.strips[0].scale - 1.0) > 0.0001:
            strip_scale = track.strips[0].scale
            for fc in exp_fcurves:
                for kp in fc.keyframe_points:
                    kp.co.x = 1.0 + ((kp.co.x - 1.0) * strip_scale)
                    kp.handle_left.x = 1.0 + ((kp.handle_left.x - 1.0) * strip_scale)
                    kp.handle_right.x = 1.0 + ((kp.handle_right.x - 1.0) * strip_scale)
                fc.update()

        anim_core.ensure_action_slot(obj, export_action)
        export_action.use_fake_user = True
                
        lib_path = get_library_path()
        if layer:
            raw_name = layer.name
        elif action and action.name and action.name != "Action":
            raw_name = action.name
        elif getattr(settings, 'upload_selection', 'LAYER') == 'KEYFRAMES':
            raw_name = f"{obj.name}_Keyframes"
        else:
            raw_name = f"{obj.name}_Layer"
            
        base_name = raw_name
        out_filepath = os.path.join(lib_path, f"{base_name}.blend")
        counter = 1
        while os.path.exists(out_filepath):
            base_name = f"{raw_name}_{counter:03d}"
            out_filepath = os.path.join(lib_path, f"{base_name}.blend")
            counter += 1
            
        if layer:
            layer.name = base_name
        export_action.name = base_name
        
        # Save action to blend file with fake_user=True
        bpy.data.libraries.write(out_filepath, {export_action}, fake_user=True)
        try:
            bpy.data.actions.remove(export_action)
        except Exception:
            pass
        
        # Render preview images using OpenGL Viewport render of the full composite viewport
        preview_dir = get_preview_dir(base_name)
        _render_preview_sequence(context, obj, track, action, preview_dir, start_f=start_f, end_f=end_f)
        
        # Save metadata without erasing fps/frame_range/step
        import json
        meta_filepath = os.path.join(preview_dir, "meta.json")
        
        meta = {}
        try:
            if os.path.exists(meta_filepath):
                with open(meta_filepath, 'r') as f:
                    meta = json.load(f)
        except Exception:
            pass
        
        meta["target_type"] = obj.type
        meta["target_name"] = obj.name
        meta["blend_type"] = layer.blend_type if layer else getattr(settings, 'import_blend_type', 'COMBINE')
        
        try:
            with open(meta_filepath, 'w') as f:
                json.dump(meta, f)
        except Exception as e:
            print(f"Failed to save metadata for library item: {e}")
            
        bpy.ops.lsd.anim_library_refresh()
        self.report({'INFO'}, f"Successfully exported layer '{base_name}' to Library")
        return {'FINISHED'}

class LSD_OT_Anim_Library_Import(bpy.types.Operator):
    bl_idname = "lsd.anim_library_import"
    bl_label = "Import Layer"
    
    def execute(self, context):
        import_frame = float(context.scene.frame_current)
        settings = context.scene.lsd_anim_settings
        if not settings.library_items or settings.active_library_index >= len(settings.library_items):
            return {'CANCELLED'}
            
        item = settings.library_items[settings.active_library_index]
        
        # Load metadata if present
        item_meta = {}
        try:
            preview_dir = get_preview_dir(item.name)
            meta_path = os.path.join(preview_dir, "meta.json")
            if os.path.exists(meta_path):
                with open(meta_path, 'r') as f:
                    item_meta = json.load(f)
        except Exception:
            pass
        
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
        target_slot = None
        if obj and hasattr(action, 'slots'):
            target_slot = anim_core.ensure_action_slot(obj, action)
            
            # If action has legacy fcurves and slot was created, migrate them into the channelbag
            if target_slot and hasattr(action, 'fcurves') and len(action.fcurves) > 0:
                try:
                    from bpy_extras import anim_utils
                    cb = anim_utils.action_get_channelbag_for_slot(action, target_slot)
                    if cb is None and hasattr(action, 'layers'):
                        act_layer = action.layers[0] if len(action.layers) > 0 else action.layers.new("Layer")
                        try:
                            cb = act_layer.channelbags.new(target_slot)
                        except Exception:
                            try:
                                cb = act_layer.channelbags.new(slot=target_slot)
                            except Exception:
                                pass
                    if cb and hasattr(cb, 'fcurves') and len(cb.fcurves) == 0:
                        for old_fc in list(action.fcurves):
                            new_fc = None
                            try:
                                new_fc = cb.fcurves.new(old_fc.data_path, old_fc.array_index)
                            except Exception:
                                try:
                                    new_fc = cb.fcurves.new(old_fc.data_path, index=old_fc.array_index)
                                except Exception:
                                    pass
                            if new_fc:
                                for kp in old_fc.keyframe_points:
                                    nkp = new_fc.keyframe_points.insert(kp.co.x, kp.co.y, options={'FAST'})
                                    nkp.handle_left = kp.handle_left
                                    nkp.handle_right = kp.handle_right
                                    nkp.handle_left_type = kp.handle_left_type
                                    nkp.handle_right_type = kp.handle_right_type
                                    nkp.interpolation = kp.interpolation
                                new_fc.update()
                except Exception as e:
                    print(f"Failed migrating legacy curves to slotted channelbag: {e}")
        
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
            
            # Detect preceding layers for continuous evaluation and coordinate continuity
            existing_layers = list(obj.lsd_anim_layers_data.layers) if (obj and hasattr(obj, 'lsd_anim_layers_data')) else []
            preceding_combine_layer = None
            last_combine_kf_frame = None
            if existing_layers:
                for lyr in reversed(existing_layers):
                    if lyr.blend_type == 'COMBINE':
                        preceding_combine_layer = lyr
                        break

            if preceding_combine_layer and obj and obj.animation_data:
                comb_track = obj.animation_data.nla_tracks.get(preceding_combine_layer.track_name)
                if comb_track and comb_track.strips:
                    comb_strip = comb_track.strips[0]
                    comb_act = comb_strip.action
                    if comb_act:
                        c_fcs = anim_core.get_action_fcurves(obj, comb_act)
                        comb_kfs = []
                        for c_fc in c_fcs:
                            for kp in c_fc.keyframe_points:
                                comb_kfs.append(comb_strip.frame_start + (kp.co.x - comb_strip.action_frame_start) * comb_strip.scale)
                        if comb_kfs:
                            last_combine_kf_frame = max(comb_kfs)
                        else:
                            last_combine_kf_frame = comb_strip.frame_end

            # Ensure preceding REPLACE strip holds forward (so COMBINE layer evaluated on top does not snap/collapse)
            preceding_replace_layer = None
            if existing_layers:
                for lyr in reversed(existing_layers):
                    if lyr.blend_type == 'REPLACE':
                        preceding_replace_layer = lyr
                        break
            if preceding_replace_layer and obj and obj.animation_data:
                rep_track = obj.animation_data.nla_tracks.get(preceding_replace_layer.track_name)
                if rep_track and rep_track.strips:
                    rep_strip = rep_track.strips[0]
                    rep_strip.extrapolation = 'HOLD_FORWARD' if rep_strip.frame_start > 1.0 else 'HOLD'

            # Sample evaluated transforms for REPLACE mode import at current frame
            eval_obj_loc = None
            eval_bone_locs = {}
            if settings.import_blend_type == 'REPLACE':
                orig_scene_frame = context.scene.frame_current
                try:
                    context.scene.frame_set(int(round(import_frame)))
                    context.view_layer.update()
                    eval_obj_loc = obj.location.copy()
                    if obj.type == 'ARMATURE' and obj.pose:
                        for pb in obj.pose.bones:
                            eval_bone_locs[pb.name] = pb.location.copy()
                finally:
                    context.scene.frame_set(int(round(orig_scene_frame)))
                    context.view_layer.update()

            # True Delta Engine Conversion: In COMBINE mode, always normalize curves to delta starting at frame 1.0
            import mathutils
            groups = {}
            for fc in fcurves:
                if fc.data_path not in groups:
                    groups[fc.data_path] = []
                groups[fc.data_path].append(fc)
                
            for data_path, fcs in groups.items():
                if "rotation_quaternion" in data_path and settings.import_blend_type in {'COMBINE', 'ADD'}:
                    # Complex Delta Conversion for Quaternions (Blender NLA COMBINE evaluates base_q @ delta_q)
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
                            
                            # Insert clean delta keyframes starting at identity (1, 0, 0, 0)
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
                elif settings.import_blend_type in {'COMBINE', 'ADD'}:
                    if "scale" in data_path:
                        for fc in fcs:
                            if not fc.keyframe_points:
                                continue
                            fc.extrapolation = 'CONSTANT'
                            action_start_val = fc.evaluate(1.0)
                            if abs(action_start_val) > 0.0001:
                                scale_ratio = 1.0 / action_start_val
                                for kp in fc.keyframe_points:
                                    kp.co.y *= scale_ratio
                                    kp.handle_left.y *= scale_ratio
                                    kp.handle_right.y *= scale_ratio
                                    kp.handle_left_type = 'AUTO_CLAMPED'
                                    kp.handle_right_type = 'AUTO_CLAMPED'
                            fc.update()
                    else:
                        for fc in fcs:
                            if not fc.keyframe_points:
                                continue
                                
                            fc.extrapolation = 'CONSTANT'
                            action_start_val = fc.evaluate(1.0)
                            spatial_offset = 0.0 - action_start_val
                            
                            for kp in fc.keyframe_points:
                                kp.co.y = (kp.co.y + spatial_offset)
                                kp.handle_left.y = (kp.handle_left.y + spatial_offset)
                                kp.handle_right.y = (kp.handle_right.y + spatial_offset)
                                kp.handle_left_type = 'AUTO_CLAMPED'
                                kp.handle_right_type = 'AUTO_CLAMPED'
                                
                            fc.update()
                else:
                    # REPLACE mode: Start animation at the current object/bone location at the current frame
                    if "location" in data_path:
                        is_bone = "pose.bones[" in data_path
                        bone_name = None
                        if is_bone:
                            try:
                                bone_name = data_path.split('"')[1]
                            except Exception:
                                pass
                        
                        target_start_vector = None
                        if is_bone and bone_name and bone_name in eval_bone_locs:
                            target_start_vector = eval_bone_locs[bone_name]
                        elif not is_bone and eval_obj_loc is not None:
                            target_start_vector = eval_obj_loc
                            
                        for fc in fcs:
                            fc.extrapolation = 'CONSTANT'
                            if not fc.keyframe_points:
                                continue
                            if target_start_vector is not None and fc.array_index < len(target_start_vector):
                                action_start_val = fc.evaluate(1.0)
                                target_val = target_start_vector[fc.array_index]
                                spatial_offset = target_val - action_start_val
                                for kp in fc.keyframe_points:
                                    kp.co.y += spatial_offset
                                    kp.handle_left.y += spatial_offset
                                    kp.handle_right.y += spatial_offset
                                    kp.handle_left_type = 'AUTO_CLAMPED'
                                    kp.handle_right_type = 'AUTO_CLAMPED'
                            fc.update()
                    else:
                        for fc in fcs:
                            fc.extrapolation = 'CONSTANT'
                            fc.update()
        
        # Create a new layer in our UI
        bpy.ops.lsd.anim_layer_add()
        
        # Apply chosen import blend mode
        layer_data = obj.lsd_anim_layers_data
        curr_frame = import_frame
        if layer_data.active_layer_index >= 0 and layer_data.active_layer_index < len(layer_data.layers):
            layer = layer_data.layers[layer_data.active_layer_index]
            layer.blend_type = settings.import_blend_type
            if obj and obj.animation_data:
                track = obj.animation_data.nla_tracks.get(layer.track_name)
                if track and track.strips:
                    track.strips[0].blend_type = settings.import_blend_type
                    track.strips[0].extrapolation = 'HOLD' if ("Base_Layer" in layer.name or "Base Layer" in layer.name) else ('HOLD_FORWARD' if curr_frame > 1.0 else 'HOLD')
        
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
                    old_strip = track.strips[0]
                    old_action = old_strip.action
                    strip_name = old_strip.name
                    
                    track.strips.remove(old_strip)
                    if old_action:
                        try: bpy.data.actions.remove(old_action)
                        except: pass
                    
                    # Create the strip starting exactly at the current timeline frame
                    curr_frame = import_frame
                    strip = track.strips.new(name=strip_name, start=int(curr_frame), action=action)
                    anim_core.bind_strip_slot(obj, strip, target_slot)
                    
                    if hasattr(strip, 'use_sync_length'):
                        strip.use_sync_length = False
                    strip.action_frame_start = 1.0
                    strip.action_frame_end = max_frame if has_keys else 100.0
                    strip.frame_start = curr_frame
                    strip.scale = 1.0
                    strip.frame_end = curr_frame + (strip.action_frame_end - strip.action_frame_start)
                    strip.scale = 1.0
                    
                    # Set zero blend-in to avoid fractional frame interpolation / backtracking / downward offsets
                    strip.blend_in = 0.0
                    strip.blend_out = 0.0
                        
                    strip.blend_type = settings.import_blend_type
                    layer.blend_type = settings.import_blend_type
                    strip.extrapolation = 'HOLD' if ("Base_Layer" in layer.name or "Base Layer" in layer.name) else ('HOLD_FORWARD' if curr_frame > 1.0 else 'HOLD')
                        
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
                if obj and obj.animation_data and hasattr(obj.animation_data, 'action_slot') and target_slot:
                    try: obj.animation_data.action_slot = target_slot
                    except Exception: pass
                if obj:
                    obj.update_tag(refresh={'OBJECT', 'DATA', 'TIME'})
                if context.view_layer:
                    context.view_layer.update()
                context.scene.frame_set(int(import_frame))
                    
                # Ensure timeline displays strictly in frames and Graph Editor shows curves
                anim_core.ensure_timeline_frame_display(context)
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
