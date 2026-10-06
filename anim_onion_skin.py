import bpy
import gpu
from gpu_extras.batch import batch_for_shader
import time
import mathutils

_draw_handler = None
_cached_data = {}
_last_selection_state = None
_last_bone_matrices = {}
_is_dragging = False
_last_scene_object_count = -1

def get_bone_lines(obj, depsgraph, bone_names=None):
    lines = []
    if obj.type != 'ARMATURE': return lines
    
    eval_obj = obj.evaluated_get(depsgraph)
    mat = eval_obj.matrix_world
    
    for bone in eval_obj.pose.bones:
        if bone_names is not None and bone.name not in bone_names:
            continue
        lines.extend([mat @ bone.head, mat @ bone.tail])
            
    return lines

def get_mesh_lines(obj, depsgraph, display_type='BOUNDS'):
    lines = []
    if obj.type != 'MESH': return lines
    
    if display_type == 'BOUNDS':
        bbox = obj.bound_box
        if not bbox: return lines
        
        eval_obj = obj.evaluated_get(depsgraph)
        mat = eval_obj.matrix_world
        
        edges = [
            (0,1), (1,2), (2,3), (3,0),
            (4,5), (5,6), (6,7), (7,4),
            (0,4), (1,5), (2,6), (3,7)
        ]
        
        for e1, e2 in edges:
            v1 = mathutils.Vector(bbox[e1])
            v2 = mathutils.Vector(bbox[e2])
            lines.extend([mat @ v1, mat @ v2])
            
    else: # MESH mode
        eval_obj = obj.evaluated_get(depsgraph)
        mesh = eval_obj.to_mesh()
        mat = eval_obj.matrix_world
        
        mesh.transform(mat)
        
        settings = getattr(bpy.context.scene, 'lsd_anim_settings', None)
        resolution = settings.onion_skin_mesh_resolution if settings else 1.0
        step = max(1, int(1.0 / max(0.01, resolution)))
        
        verts = mesh.vertices
        for i, edge in enumerate(mesh.edges):
            if i % step != 0: continue
            v1 = verts[edge.vertices[0]].co
            v2 = verts[edge.vertices[1]].co
            lines.extend(((v1.x, v1.y, v1.z), (v2.x, v2.y, v2.z)))
            
        eval_obj.to_mesh_clear()
        
    return lines
    


def draw_callback():
    if not _cached_data: return
    
    context = bpy.context
    settings = getattr(context.scene, 'lsd_anim_settings', None)
    if not settings or not settings.onion_skin_enabled:
        return
        
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')
    gpu.state.line_width_set(2.0)
    
    current_frame = context.scene.frame_current
    
    for frame, data in _cached_data.items():
        if isinstance(data, list):
            if not data: continue
            batch = batch_for_shader(shader, 'LINES', {"pos": data})
            _cached_data[frame] = batch
        elif isinstance(data, dict):
            if 'full' in data and isinstance(data['full'], list):
                data['full'] = batch_for_shader(shader, 'LINES', {"pos": data['full']}) if data['full'] else None
            if 'filtered' in data and isinstance(data['filtered'], list):
                data['filtered'] = batch_for_shader(shader, 'LINES', {"pos": data['filtered']}) if data['filtered'] else None
            _cached_data[frame] = data
            batch = data.get('full')
        else:
            batch = data
            
        is_keyframe = frame in _cached_keyframe_frames
        
        batches_to_draw = []
        
        # 1. Evaluate Present (Keyframe Highlight)
        if getattr(settings, 'onion_skin_show_present', False) and is_keyframe and frame != current_frame:
            if isinstance(_cached_data[frame], dict):
                present_batch = _cached_data[frame].get('filtered')
                if present_batch is None: present_batch = _cached_data[frame].get('full')
            else:
                present_batch = _cached_data[frame]
                
            try:
                sorted_keys = sorted(_cached_keyframe_frames)
                idx = sorted_keys.index(frame)
                if idx % 2 == 0:
                    color = list(settings.onion_skin_color_present) + [1.0]
                else:
                    color = list(settings.onion_skin_color_present_2) + [1.0]
            except Exception:
                color = list(settings.onion_skin_color_present) + [1.0]
                
            if present_batch:
                batches_to_draw.append((present_batch, color))
                
        # 2. Evaluate Past or Future (Full Armature/Meshes)
        if frame < current_frame:
            if getattr(settings, 'onion_skin_show_past', True):
                if isinstance(_cached_data[frame], dict): past_batch = _cached_data[frame].get('full')
                else: past_batch = _cached_data[frame]
                
                base_opacity = settings.onion_skin_opacity_before
                if getattr(settings, 'onion_skin_fade', False) and settings.onion_skin_count_before >= max(1, settings.onion_skin_frame_distance):
                    frame_dist = max(1, settings.onion_skin_frame_distance)
                    n = (abs(current_frame - frame) + frame_dist - 1) // frame_dist
                    max_n = settings.onion_skin_count_before // frame_dist
                    if max_n > 1:
                        base_opacity *= max(0.0, 1.0 - (n - 1) / (max_n - 1))
                color = list(settings.onion_skin_color_past) + [base_opacity]
                
                if past_batch:
                    batches_to_draw.append((past_batch, color))
                    
        elif frame > current_frame:
            if getattr(settings, 'onion_skin_show_future', True):
                if isinstance(_cached_data[frame], dict): future_batch = _cached_data[frame].get('full')
                else: future_batch = _cached_data[frame]
                
                base_opacity = settings.onion_skin_opacity_after
                if getattr(settings, 'onion_skin_fade', False) and settings.onion_skin_count_after >= max(1, settings.onion_skin_frame_distance):
                    frame_dist = max(1, settings.onion_skin_frame_distance)
                    n = (abs(current_frame - frame) + frame_dist - 1) // frame_dist
                    max_n = settings.onion_skin_count_after // frame_dist
                    if max_n > 1:
                        base_opacity *= max(0.0, 1.0 - (n - 1) / (max_n - 1))
                color = list(settings.onion_skin_color_future) + [base_opacity]
                
                if future_batch:
                    batches_to_draw.append((future_batch, color))
                    
        # 3. Draw all assigned batches for this frame
        for b, c in batches_to_draw:
            shader.bind()
            gpu.state.line_width_set(2.0)
            shader.uniform_float("color", c)
            b.draw(shader)
        
    gpu.state.blend_set('NONE')

_is_building = False
_cached_keyframe_frames = set()
_cached_keyframe_bone_data = {} # frame: set of bone names / object names

def build_onion_cache(context=None):
    global _is_building
    if _is_building: return
    
    if context is None:
        context = bpy.context
        
    settings = getattr(context.scene, 'lsd_anim_settings', None)
    if not settings or not settings.onion_skin_enabled: return
    
    _is_building = True
    try:
        target_objs = []
        armature_selected_bones = {}
        if settings.onion_skin_target == 'SELECTED':
            for o in context.selected_objects:
                if o.type in {'ARMATURE', 'MESH'} and not o.name.startswith("LSD_OnionArrow_"):
                    if o.type == 'ARMATURE' and getattr(o, 'mode', '') == 'POSE':
                        sel_bones = {b.name for b in context.selected_pose_bones if b.id_data == o} if getattr(context, 'selected_pose_bones', None) else set()
                        if not sel_bones:
                            continue # If in pose mode and no bones are selected, it doesn't count as a valid target
                        armature_selected_bones[o.name] = sel_bones
                    target_objs.append(o)
                
            if settings.onion_skin_near_count > 0 and len(target_objs) > 0:
                center_loc = target_objs[0].matrix_world.translation
                
                # If target is an armature in pose mode, calculate distance from the selected bones rather than the armature root
                if target_objs[0].type == 'ARMATURE' and getattr(target_objs[0], 'mode', '') == 'POSE':
                    _sel_bones = getattr(context, 'selected_pose_bones', [])
                    _valid_bones = [b for b in _sel_bones if b.id_data == target_objs[0]] if _sel_bones else []
                    if _valid_bones:
                        import mathutils
                        avg_loc = sum(((target_objs[0].matrix_world @ b.head) for b in _valid_bones), mathutils.Vector((0,0,0))) / len(_valid_bones)
                        center_loc = avg_loc
                        
                all_vis = [o for o in context.visible_objects if o.type in {'ARMATURE', 'MESH'} and o not in target_objs and not o.name.startswith("LSD_OnionArrow_")]
                all_vis.sort(key=lambda o: (o.matrix_world.translation - center_loc).length)
                target_objs.extend(all_vis[:settings.onion_skin_near_count])
        else:
            target_objs = [o for o in context.visible_objects if o.type in {'ARMATURE', 'MESH'} and not o.name.startswith("LSD_OnionArrow_")]
            
        if not target_objs:
            _cached_data.clear()
            _cached_keyframe_frames.clear()
            _cached_keyframe_bone_data.clear()
            
            arrow_col = bpy.data.collections.get("LSD_Onion_Arrows")
            if arrow_col:
                for obj in list(arrow_col.objects):
                    try: bpy.data.objects.remove(obj, do_unlink=True)
                    except: pass
                try: bpy.data.collections.remove(arrow_col)
                except: pass
            return
        
        orig_frame = context.scene.frame_current
        frame_step = max(1, settings.onion_skin_frame_distance)
        frames_before = settings.onion_skin_count_before
        frames_after = settings.onion_skin_count_after
        
        start_frame = orig_frame
        if getattr(settings, 'onion_skin_show_past', True):
            start_frame -= frames_before
            
        end_frame = orig_frame
        if getattr(settings, 'onion_skin_show_future', True):
            end_frame += frames_after
            
        frames_to_cache = []
        _cached_keyframe_frames.clear()
        _cached_keyframe_bone_data.clear()
        
        if getattr(settings, 'onion_skin_show_present', False):
            for obj in target_objs:
                if not obj.animation_data: continue
                
                active_action = None
                strip_frame_start = 0
                strip_action_frame_start = 0
                strip_scale = 1.0
                
                if obj.animation_data.action:
                    active_action = obj.animation_data.action
                    if getattr(obj.animation_data, 'use_tweak_mode', False):
                        for track in obj.animation_data.nla_tracks:
                            for strip in track.strips:
                                if strip.action == active_action:
                                    strip_frame_start = strip.frame_start
                                    strip_action_frame_start = strip.action_frame_start
                                    strip_scale = strip.scale
                                    break
                                    
                elif settings.layers_enabled and hasattr(obj, 'lsd_anim_layers_data') and obj.lsd_anim_layers_data.active_layer_index >= 0 and obj.lsd_anim_layers_data.active_layer_index < len(obj.lsd_anim_layers_data.layers):
                    layer = obj.lsd_anim_layers_data.layers[obj.lsd_anim_layers_data.active_layer_index]
                    track = obj.animation_data.nla_tracks.get(layer.track_name)
                    if track and track.strips:
                        active_strip = track.strips[0]
                        active_action = active_strip.action
                        strip_frame_start = active_strip.frame_start
                        strip_action_frame_start = active_strip.action_frame_start
                        strip_scale = active_strip.scale
                        
                if not active_action: continue
                
                try:
                    from . import anim_core
                    fcurves_list = anim_core.get_action_fcurves(obj, active_action)
                except Exception:
                    fcurves_list = []
                
                for fcurve in fcurves_list:
                    if getattr(fcurve, "mute", False):
                        continue
                    
                    target_name = obj.name
                    if fcurve.data_path.startswith("pose.bones["):
                        try:
                            target_name = fcurve.data_path.split('"')[1]
                        except:
                            try: target_name = fcurve.data_path.split("'")[1]
                            except: pass
                            
                    if hasattr(fcurve, "keyframe_points"):
                        for kp in fcurve.keyframe_points:
                            scene_frame = round(strip_frame_start + (kp.co.x - strip_action_frame_start) * strip_scale)
                            f = int(scene_frame)
                            if getattr(settings, 'onion_skin_delete_outer_keyframes', False):
                                if start_frame <= f <= end_frame:
                                    _cached_keyframe_frames.add(f)
                                    if f not in _cached_keyframe_bone_data: _cached_keyframe_bone_data[f] = set()
                                    _cached_keyframe_bone_data[f].add(target_name)
                            else:
                                _cached_keyframe_frames.add(f)
                                if f not in _cached_keyframe_bone_data: _cached_keyframe_bone_data[f] = set()
                                _cached_keyframe_bone_data[f].add(target_name)
                                
        first_mult = start_frame - (start_frame % frame_step)
        if first_mult < start_frame:
            first_mult += frame_step
            
        curr = first_mult
        while curr <= end_frame:
            frames_to_cache.append(curr)
            curr += frame_step
                
        if getattr(settings, 'onion_skin_show_present', False):
            if orig_frame not in frames_to_cache:
                frames_to_cache.append(orig_frame)
            for f in _cached_keyframe_frames:
                if f not in frames_to_cache:
                    frames_to_cache.append(f)
            
        depsgraph = context.evaluated_depsgraph_get()
        
        frames_to_remove = [f for f in list(_cached_data.keys()) if f not in frames_to_cache]
        for f in frames_to_remove:
            del _cached_data[f]
            
        arrow_col = bpy.data.collections.get("LSD_Onion_Arrows")
        if arrow_col:
            for obj in list(arrow_col.objects):
                try: bpy.data.objects.remove(obj, do_unlink=True)
                except: pass
            try: bpy.data.collections.remove(arrow_col)
            except: pass
            
        frames_to_build = [f for f in frames_to_cache if f not in _cached_data]
        
        changed_frame = False
        if frames_to_build:
            global _is_auto_syncing
            _is_auto_syncing = True
            try:
                for f in frames_to_build:
                    if f != orig_frame:
                        context.scene.frame_set(f)
                        changed_frame = True
                    
                    # Fetch fresh evaluated depsgraph for this specific frame
                    current_deps = context.evaluated_depsgraph_get()
                        
                    full_lines = []
                    filtered_lines = []
                    
                    for obj in target_objs:
                        obj_keyframed = False
                        bone_names = None
                        
                        if f in _cached_keyframe_bone_data:
                            # If this object's name is in the set, the whole object is keyframed
                            # If any bone names are in the set, we filter to those
                            if obj.name in _cached_keyframe_bone_data[f]:
                                obj_keyframed = True
                            else:
                                bone_names = _cached_keyframe_bone_data[f]
                                if any(b in bone_names for b in [pb.name for pb in obj.pose.bones]) if obj.type == 'ARMATURE' else False:
                                    obj_keyframed = True
                        
                        if obj.type == 'ARMATURE':
                            active_bone_names = None
                            if obj.name in armature_selected_bones:
                                active_bone_names = armature_selected_bones[obj.name]
                                if not active_bone_names:
                                    continue # If in Pose Mode and no bones are selected, skip drawing this armature completely
                                    
                            full_lines.extend(get_bone_lines(obj, current_deps, None))
                            if obj_keyframed and bone_names:
                                # Intersect the active selection with the keyframed bones filter
                                if active_bone_names is not None:
                                    filtered = active_bone_names.intersection(set(bone_names))
                                    filtered_lines.extend(get_bone_lines(obj, current_deps, filtered))
                                else:
                                    filtered_lines.extend(get_bone_lines(obj, current_deps, bone_names))
                            elif obj_keyframed:
                                filtered_lines.extend(get_bone_lines(obj, current_deps, active_bone_names))
                        elif obj.type == 'MESH':
                            m_lines = get_mesh_lines(obj, current_deps, settings.onion_skin_display_type)
                            full_lines.extend(m_lines)
                            if obj_keyframed:
                                filtered_lines.extend(m_lines)
                                
                    if f in _cached_keyframe_frames and getattr(settings, 'onion_skin_keyframe_filter', 'TARGETS') == 'TARGETS':
                        _cached_data[f] = {'full': full_lines, 'filtered': filtered_lines}
                    else:
                        _cached_data[f] = full_lines
                        
            finally:
                if changed_frame:
                    context.scene.frame_set(orig_frame)
                _is_auto_syncing = False
    finally:
        _is_building = False

def onion_skin_timer_update():
    try:
        settings = getattr(bpy.context.scene, 'lsd_anim_settings', None)
    except:
        return 1.0
        
    if not settings: return 1.0
    
    if settings.onion_skin_enabled and settings.onion_skin_auto_refresh:
        curr_frame = bpy.context.scene.frame_current
        
        global _last_selection_state, _last_bone_matrices, _is_dragging, _last_scene_object_count
        
        # Auto-disable Timeline Onion Skinning when copying or duplicating objects
        curr_obj_count = len(bpy.context.scene.objects)
        if _last_scene_object_count != -1 and curr_obj_count > _last_scene_object_count:
            _last_scene_object_count = curr_obj_count
            settings.onion_skin_enabled = False
            return 1.0
        _last_scene_object_count = curr_obj_count
        
        # 1. Active Transform Intercept: Monitor bone matrices to detect dragging vs dropping
        is_currently_moving = False
        obj = bpy.context.active_object
        if obj and obj.type == 'ARMATURE' and bpy.context.mode == 'POSE':
            sel_bones = [b for b in bpy.context.selected_pose_bones if b.id_data == obj] if getattr(bpy.context, 'selected_pose_bones', None) else []
            
            # Clean up unselected bones from matrix cache
            current_bone_names = {b.name for b in sel_bones}
            _last_bone_matrices = {name: mat for name, mat in _last_bone_matrices.items() if name in current_bone_names}
            
            for b in sel_bones:
                if b.name in _last_bone_matrices:
                    diff = b.matrix_basis - _last_bone_matrices[b.name]
                    if any(abs(v) > 0.0001 for row in diff for v in row):
                        if not bpy.context.screen.is_animation_playing:
                            is_currently_moving = True
                        _last_bone_matrices[b.name] = b.matrix_basis.copy()
                else:
                    _last_bone_matrices[b.name] = b.matrix_basis.copy()
        else:
            _last_bone_matrices.clear()
            
        if is_currently_moving:
            _is_dragging = True
            # User is actively dragging a bone. Halt full cache rebuild to prevent massive lag.
            for window in bpy.context.window_manager.windows:
                for area in window.screen.areas:
                    if area.type == 'VIEW_3D':
                        area.tag_redraw()
            return max(0.1, settings.onion_skin_refresh_interval)
            
        elif _is_dragging:
            # Bone just stopped moving (dropped or keyed). Flush entire cache to recalculate past/future ghosts!
            _cached_data.clear()
            _is_dragging = False
            
        # 2. Monitor absolute object selection changes
        if settings.onion_skin_target == 'SELECTED':
            current_sel = set()
            for o in bpy.context.selected_objects:
                current_sel.add(o.name)
                if o.type == 'ARMATURE' and getattr(o, 'mode', '') == 'POSE':
                    if getattr(bpy.context, 'selected_pose_bones', None):
                        for b in bpy.context.selected_pose_bones:
                            if b.id_data == o:
                                current_sel.add(f"{o.name}:{b.name}")
            
            if _last_selection_state != current_sel:
                _cached_data.clear()
                _last_selection_state = current_sel
        else:
            _last_selection_state = None
            
        # Always force refresh the current frame so the active pose is never stale (unless playing)
        if not bpy.context.screen.is_animation_playing:
            if curr_frame in _cached_data:
                del _cached_data[curr_frame]
                
        build_onion_cache(bpy.context)
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()
        return max(0.1, settings.onion_skin_refresh_interval)
    else:
        return 1.0

def update_onion_skin(self, context):
    global _draw_handler
    settings = context.scene.lsd_anim_settings
    
    if settings.onion_skin_enabled:
        if _draw_handler is None:
            _draw_handler = bpy.types.SpaceView3D.draw_handler_add(draw_callback, (), 'WINDOW', 'POST_VIEW')
        _cached_data.clear()  # Force a full rebuild so arrows and meshes synchronize instantly
        build_onion_cache(context)
    else:
        if _draw_handler is not None:
            bpy.types.SpaceView3D.draw_handler_remove(_draw_handler, 'WINDOW')
            _draw_handler = None
        _cached_data.clear()
        
    if context.area:
        context.area.tag_redraw()

class LSD_OT_Calculate_Onion_Skin(bpy.types.Operator):
    bl_idname = "lsd.calculate_onion_skin"
    bl_label = "Calculate Ghosts"
    bl_description = "Bake the onion skin frames based on the current timeline position"
    
    def execute(self, context):
        global _cached_data
        _cached_data.clear()
        build_onion_cache(context)
        if context.area:
            context.area.tag_redraw()
        return {'FINISHED'}

def register():
    bpy.utils.register_class(LSD_OT_Calculate_Onion_Skin)
    if not bpy.app.timers.is_registered(onion_skin_timer_update):
        bpy.app.timers.register(onion_skin_timer_update)

def unregister():
    if bpy.app.timers.is_registered(onion_skin_timer_update):
        bpy.app.timers.unregister(onion_skin_timer_update)
    bpy.utils.unregister_class(LSD_OT_Calculate_Onion_Skin)
    global _draw_handler
    if _draw_handler is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(_draw_handler, 'WINDOW')
        except:
            pass
        _draw_handler = None

