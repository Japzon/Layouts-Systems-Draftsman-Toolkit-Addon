import bpy

def reset_auto_keyframe_cache():
    """Resets the auto-keyframe cache so UI toggles don't falsely trigger snapshots."""
    try:
        from . import core
        if getattr(core, '_ak_cache', None) is not None:
            core._ak_dirty = False
            core._ak_dirty_time = 0.0
            obj = bpy.context.active_object
            if obj and obj.type == 'ARMATURE':
                core._ak_cache.clear()
                for bone in obj.pose.bones:
                    core._ak_cache[bone.name] = bone.matrix_basis.copy()
    except Exception:
        pass

def get_anim_settings(context):
    return context.scene.lsd_anim_settings

def get_active_object(context):
    obj = context.active_object
    if obj and obj.type == 'ARMATURE':
        return obj
    # If a mesh is selected, check its parent or modifiers
    if obj and obj.parent and obj.parent.type == 'ARMATURE':
        return obj.parent
    return obj

def ensure_nla_track(obj, track_name):
    if not obj.animation_data:
        obj.animation_data_create()
    for track in obj.animation_data.nla_tracks:
        if track.name == track_name:
            return track
    
    # If the track doesn't exist, create it
    track = obj.animation_data.nla_tracks.new()
    track.name = track_name
    return track

def get_action_fcurves(obj, action):
    """Safely retrieves F-Curves from both legacy Actions and Blender 4.3+ Slotted Actions."""
    fcurves = []
    if not action:
        return fcurves
    
    # 1. Slotted Actions (Blender 4.3+ / 5.x)
    # Check all layers and channelbags directly
    if hasattr(action, "layers"):
        try:
            for layer in action.layers:
                for cb in getattr(layer, "channelbags", []):
                    if hasattr(cb, "fcurves") and cb.fcurves:
                        for fc in cb.fcurves:
                            if fc not in fcurves:
                                fcurves.append(fc)
        except Exception:
            pass
            
    # Also check via action.slots using anim_utils if layers direct access was empty
    if not fcurves and hasattr(action, "slots") and len(action.slots) > 0:
        try:
            from bpy_extras import anim_utils
            for slot in action.slots:
                cb = anim_utils.action_get_channelbag_for_slot(action, slot)
                if cb and hasattr(cb, "fcurves") and cb.fcurves:
                    for fc in cb.fcurves:
                        if fc not in fcurves:
                            fcurves.append(fc)
        except Exception:
            pass

    # 2. Legacy Actions (Blender 4.2 and earlier or unslotted actions)
    if not fcurves and hasattr(action, "fcurves") and len(action.fcurves) > 0:
        try:
            fcurves.extend(action.fcurves)
        except Exception:
            pass

    return fcurves

def ensure_action_slot(obj, action):
    """Ensures that the action has a valid slot for the object in Blender 4.3+/5.x Slotted Actions,
    and returns that slot."""
    if not action or not hasattr(action, 'slots'):
        return None
        
    target_slot_name = obj.id_data.name if (obj and hasattr(obj, 'id_data')) else (obj.name if obj else "Object")
    id_type = 'OBJECT'
    if obj:
        if hasattr(obj, 'id_type') and obj.id_type in {'OBJECT', 'ARMATURE', 'KEY'}:
            id_type = obj.id_type
        elif hasattr(obj, 'id_data') and hasattr(obj.id_data, 'id_type'):
            id_type = obj.id_data.id_type
    
    # 1. First priority: Check all channelbags directly on action.layers!
    # If a channelbag has F-curves and a valid slot, that slot contains the actual animation!
    best_slot = None
    if hasattr(action, "layers"):
        for layer in action.layers:
            for cb in getattr(layer, "channelbags", []):
                if hasattr(cb, "fcurves") and len(cb.fcurves) > 0 and getattr(cb, "slot", None):
                    best_slot = cb.slot
                    break
            if best_slot:
                break

    # 2. Check via anim_utils if any slot has channelbag curves
    if not best_slot:
        try:
            from bpy_extras import anim_utils
            for s in action.slots:
                cb = anim_utils.action_get_channelbag_for_slot(action, s)
                if cb and hasattr(cb, 'fcurves') and len(cb.fcurves) > 0:
                    best_slot = s
                    break
        except Exception:
            pass

    # 3. If a slot with curves was found:
    if best_slot:
        existing = action.slots.get(target_slot_name)
        if existing and existing != best_slot:
            # Check if existing has curves; if not, remove or rename it so best_slot can take the name
            existing_has_curves = False
            if hasattr(action, "layers"):
                for l in action.layers:
                    for c in getattr(l, "channelbags", []):
                        if getattr(c, "slot", None) == existing and len(getattr(c, "fcurves", [])) > 0:
                            existing_has_curves = True
                            break
            if not existing_has_curves:
                try:
                    action.slots.remove(existing)
                except Exception:
                    try:
                        existing.name = f"{target_slot_name}_unused"
                    except Exception:
                        pass
        try:
            best_slot.name = target_slot_name
        except Exception:
            pass
        return best_slot

    # 4. If a slot with matching name exists, return it
    slot = action.slots.get(target_slot_name)
    if slot:
        return slot

    # 5. If action already has any slot, rename the first one and return it
    if len(action.slots) > 0:
        slot = action.slots[0]
        try:
            slot.name = target_slot_name
        except Exception:
            pass
        return slot

    # 6. If action has 0 slots, create a new one with id_type
    try:
        slot = action.slots.new(name=target_slot_name, id_type=id_type)
        return slot
    except Exception:
        try:
            slot = action.slots.new(target_slot_name, id_type)
        except Exception:
            try:
                slot = action.slots.new(name=target_slot_name, id_type='OBJECT')
            except Exception:
                try:
                    slot = action.slots.new(target_slot_name)
                except Exception:
                    try:
                        slot = action.slots.new(name=target_slot_name)
                    except Exception:
                        return None

def bind_strip_slot(obj, strip, slot=None):
    """Binds action_slot on the NlaStrip and obj.animation_data for Blender 4.3+/5.x."""
    if not strip or not strip.action:
        return
    if not slot:
        slot = ensure_action_slot(obj, strip.action)
    if not slot:
        return
        
    if hasattr(strip, 'action_slot'):
        try:
            strip.action_slot = slot
        except Exception:
            pass
            
    if obj and obj.animation_data and hasattr(obj.animation_data, 'action_slot'):
        try:
            obj.animation_data.action_slot = slot
        except Exception:
            pass

    # Ensure channelbag exists for this slot on layer 0 so keyframing immediately works
    if hasattr(strip.action, 'layers'):
        if len(strip.action.layers) == 0:
            try: strip.action.layers.new(name="Layer")
            except Exception: pass
        if len(strip.action.layers) > 0:
            layer = strip.action.layers[0]
            has_cb = False
            for cb in getattr(layer, 'channelbags', []):
                if getattr(cb, 'slot', None) == slot:
                    has_cb = True
                    break
            if not has_cb:
                try:
                    layer.channelbags.new(slot)
                except Exception:
                    try:
                        layer.channelbags.new(slot=slot)
                    except Exception:
                        pass

def ensure_timeline_frame_display(context):
    """Enforces frame display (disabling seconds/minutes/milliseconds) and sets proper dopesheet/graph filters."""
    if not context or not hasattr(context, "window_manager") or not context.window_manager:
        return
    try:
        for w in context.window_manager.windows:
            if not w.screen:
                continue
            for a in w.screen.areas:
                if a.type in {'DOPESHEET_EDITOR', 'GRAPH_EDITOR', 'NLA_EDITOR'}:
                    # Strictly enforce Frames instead of seconds/minutes/milliseconds
                    if hasattr(a.spaces.active, 'show_seconds'):
                        try:
                            a.spaces.active.show_seconds = False
                        except Exception:
                            pass
                    if a.type == 'DOPESHEET_EDITOR':
                        # Keep Timeline / Dopesheet filtered to selected bones/objects to preserve proportionality
                        if hasattr(a.spaces.active, 'dopesheet'):
                            try:
                                a.spaces.active.dopesheet.show_nla = True
                                a.spaces.active.dopesheet.show_hidden = True
                                a.spaces.active.dopesheet.show_only_selected = True
                            except Exception:
                                pass
                    elif a.type == 'GRAPH_EDITOR':
                        # Allow Graph Editor to display active layer channel curves even if unselected
                        if hasattr(a.spaces.active, 'dopesheet'):
                            try:
                                a.spaces.active.dopesheet.show_nla = True
                                a.spaces.active.dopesheet.show_hidden = True
                                a.spaces.active.dopesheet.show_only_selected = False
                            except Exception:
                                pass
    except Exception:
        pass

def ensure_strip_bounds_and_scale(strip, scene_frame_end, is_base=False):
    """Safely maintains 1:1 scale (scale=1.0) and proper strip boundaries without stretching or jumping."""
    if not strip or not strip.action:
        return
        
    if hasattr(strip, 'use_sync_length'):
        strip.use_sync_length = False
        
    act = strip.action
    has_range = hasattr(act, 'frame_range') and (act.frame_range[1] > act.frame_range[0])
    
    if has_range:
        act_start = float(act.frame_range[0])
        act_end = float(act.frame_range[1])
        eff_act_start = min(1.0, act_start) if strip.frame_start <= 1.0 else act_start
        eff_act_end = max(act_end, eff_act_start + 1.0)
        act_duration = eff_act_end - eff_act_start
        
        try:
            strip.action_frame_start = eff_act_start
            strip.action_frame_end = eff_act_end
            strip.scale = 1.0
            strip.frame_end = strip.frame_start + act_duration
            strip.scale = 1.0
        except Exception:
            pass
    else:
        # Empty authoring strip without keyframes yet:
        # Default to full scene range starting at frame 1.0 so keyframing can occur anywhere
        try:
            strip.action_frame_start = 1.0
            strip.action_frame_end = scene_frame_end
            strip.frame_start = 1.0
            strip.frame_end = scene_frame_end
            strip.scale = 1.0
        except Exception:
            pass

def sync_layer_light(context):
    """Fast, synchronous update for influence and blend type. Preserves active layer animation."""
    settings = get_anim_settings(context)
    if not settings.layers_enabled:
        return
        
    obj = get_active_object(context)
    if not obj or not hasattr(obj, 'lsd_anim_layers_data') or not obj.animation_data:
        return
        
    layer_data = obj.lsd_anim_layers_data
    if not layer_data.layers: return
    
    scene_frame_end = max(float(context.scene.frame_end), 250.0) if context and context.scene else 250.0
    
    # Apply to the active object ONLY
    for i, layer in enumerate(layer_data.layers):
        track = None
        for t in obj.animation_data.nla_tracks:
            if t.name == layer.name or t.name == layer.track_name:
                track = t
                break
        
        if track:
            is_base = ("Base_Layer" in layer.name or "Base Layer" in layer.name or "Base_Layer" in track.name or "Base Layer" in track.name)
            for strip in track.strips:
                strip.blend_type = layer.blend_type
                strip.influence = 0.0 if layer.is_muted else layer.influence
                strip.mute = layer.is_muted
                
                if strip.action:
                    bind_strip_slot(obj, strip)
                    ensure_strip_bounds_and_scale(strip, scene_frame_end, is_base=is_base)
                    for fcurve in get_action_fcurves(obj, strip.action):
                        fcurve.mute = layer.is_muted
                        
                if is_base:
                    strip.extrapolation = 'HOLD'
                elif layer.blend_type == 'REPLACE':
                    strip.extrapolation = 'NOTHING'
                else:
                    strip.extrapolation = 'HOLD_FORWARD' if strip.frame_start > 1.0 else 'HOLD'
    
    context.view_layer.update()
    
    # Ensure active layer action is retained and not wiped!
    if layer_data.active_layer_index < len(layer_data.layers):
        active_layer = layer_data.layers[layer_data.active_layer_index]
        active_track = None
        for t in obj.animation_data.nla_tracks:
            if t.name == active_layer.name or t.name == active_layer.track_name:
                active_track = t
                break
        active_strip = active_track.strips[0] if (active_track and active_track.strips) else None

        if getattr(obj.animation_data, 'use_tweak_mode', False):
            if not active_layer.is_muted and not obj.animation_data.action and active_strip:
                try:
                    obj.animation_data.action = active_strip.action
                    bind_strip_slot(obj, active_strip)
                except Exception: pass
            if active_track:
                active_track.mute = active_layer.is_muted
        else:
            if active_strip and not active_layer.is_muted:
                if active_strip.frame_start <= 1.0:
                    try:
                        if obj.animation_data.action != active_strip.action:
                            obj.animation_data.action = active_strip.action
                    except Exception: pass
                    bind_strip_slot(obj, active_strip)
                    if active_track:
                        active_track.mute = True  # Muted in NLA so action evaluates strictly once
                    try:
                        obj.animation_data.action_blend_type = active_layer.blend_type
                        obj.animation_data.action_extrapolation = 'HOLD'
                    except Exception: pass
                else:
                    if active_track:
                        active_track.mute = False
                    bind_strip_slot(obj, active_strip)
                    try:
                        obj.animation_data.action = None
                    except Exception: pass
            elif active_layer.is_muted:
                try:
                    obj.animation_data.action = None
                except Exception: pass
                if active_track:
                    active_track.mute = True
                
        if obj.animation_data and obj.animation_data.action:
            for fcurve in get_action_fcurves(obj, obj.animation_data.action):
                fcurve.mute = active_layer.is_muted
    
    context.view_layer.update()
    
    # CRITICAL: Force NLA cache invalidation
    if obj and obj.animation_data:
        for t in obj.animation_data.nla_tracks:
            orig_mute = t.mute
            t.mute = not orig_mute
            t.mute = orig_mute
            
    context.view_layer.update()
    reset_auto_keyframe_cache()
    ensure_timeline_frame_display(context)

def execute_sync_logic(context, enter_tweak_mode=True):
    """Core logic to restructure NLA tracks."""
    settings = get_anim_settings(context)
    if not settings.layers_enabled:
        return
        
    obj = get_active_object(context)
    if not obj or not obj.animation_data or not hasattr(obj, 'lsd_anim_layers_data'):
        return
        
    layer_data = obj.lsd_anim_layers_data
    if not layer_data.layers: return

    scene_frame_end = max(float(context.scene.frame_end), 250.0) if context and context.scene else 250.0

    # Aggressively repair existing broken files: forcibly unmute the base track so the base animation is never frozen,
    # and repair base strip extrapolation and frame boundaries
    for track in obj.animation_data.nla_tracks:
        if "Base_Layer" in track.name or "Base Layer" in track.name:
            track.mute = False
            for strip in track.strips:
                strip.extrapolation = 'HOLD'
                if strip.frame_start <= -99990:
                    strip.frame_start = 1.0
                ensure_strip_bounds_and_scale(strip, scene_frame_end, is_base=True)

    # 1. Apply muting and blending settings to the active object ONLY
    for i, layer in enumerate(layer_data.layers):
        track = None
        for t in obj.animation_data.nla_tracks:
            if t.name == layer.name or t.name == layer.track_name:
                track = t
                break
        
        # Ensure the track exists
        if not track:
            track = ensure_nla_track(obj, layer.track_name if layer.track_name else layer.name)
        
        if track:
            layer.track_name = track.name
            if layer.name == track.name:
                track.name = layer.name
                
            track.mute = layer.is_muted
            track.lock = layer.is_locked
            
            is_base = ("Base_Layer" in layer.name or "Base Layer" in layer.name or "Base_Layer" in track.name or "Base Layer" in track.name)
            # In NLA, strips hold the influence and blend_type
            for strip in track.strips:
                strip.blend_type = layer.blend_type
                strip.influence = 0.0 if layer.is_muted else layer.influence
                strip.mute = layer.is_muted
                
                if strip.action:
                    bind_strip_slot(obj, strip)
                    ensure_strip_bounds_and_scale(strip, scene_frame_end, is_base=is_base)
                    for fcurve in get_action_fcurves(obj, strip.action):
                        fcurve.mute = layer.is_muted
                
                if is_base:
                    strip.extrapolation = 'HOLD'
                elif layer.blend_type == 'REPLACE':
                    strip.extrapolation = 'NOTHING'
                else:
                    strip.extrapolation = 'HOLD_FORWARD' if strip.frame_start > 1.0 else 'HOLD'
                        
    def safe_enter_tweak_mode(context_ref, o, t, s):
        if not o or not o.animation_data: return
        for window in context_ref.window_manager.windows:
            for area in window.screen.areas:
                if area.type == 'NLA_EDITOR':
                    target_space = None
                    if hasattr(area, "spaces"):
                        for space in area.spaces:
                            if space.type == 'NLA_EDITOR':
                                target_space = space
                                break
                    target_region = None
                    for region in area.regions:
                        if region.type == 'WINDOW':
                            target_region = region
                            break
                            
                    with context_ref.temp_override(window=window, area=area, region=target_region, space_data=target_space, active_object=o, selected_objects=[o]):
                        if getattr(o.animation_data, 'use_tweak_mode', False):
                            try: bpy.ops.nla.tweakmode_exit(isolate_action=False)
                            except: pass
                        if t and s:
                            for tr in o.animation_data.nla_tracks:
                                tr.select = False
                                for st in tr.strips: st.select = False
                            t.select = True
                            s.select = True
                            try:
                                o.animation_data.nla_tracks.active = t
                            except Exception: pass
                            bind_strip_slot(o, s)
                            try: bpy.ops.nla.tweakmode_enter(isolate_action=False)
                            except: pass
                            bind_strip_slot(o, s)
                            
                            # Ensure timeline displays strictly in frames and graph editor displays curves
                            ensure_timeline_frame_display(context_ref)
                    return
            
    active_layer = None
    active_track = None
    if layer_data.active_layer_index >= 0 and layer_data.active_layer_index < len(layer_data.layers):
        active_layer = layer_data.layers[layer_data.active_layer_index]
        for t in obj.animation_data.nla_tracks:
            if t.name == active_layer.name or t.name == active_layer.track_name:
                active_track = t
                break
        
        if not active_track:
            active_track = ensure_nla_track(obj, active_layer.track_name if active_layer.track_name else active_layer.name)

        if active_track.strips:
            obj.animation_data.use_nla = True
            active_strip = active_track.strips[0]
            
            # Set the track as the active track for Tweak Mode
            for t in obj.animation_data.nla_tracks:
                t.select = False
                for s in t.strips:
                    s.select = False
            
            active_track.select = True
            active_strip.select = True
            
            try:
                obj.animation_data.nla_tracks.active = active_track
            except Exception: pass
            
            bind_strip_slot(obj, active_strip)

            # Retroactively ensure all existing strips use proper bounds and 1:1 scale
            for t in obj.animation_data.nla_tracks:
                is_b = ("Base_Layer" in t.name or "Base Layer" in t.name)
                for s in t.strips:
                    ensure_strip_bounds_and_scale(s, scene_frame_end, is_base=is_b)
                
            try:
                obj.animation_data.action_blend_type = active_layer.blend_type
                obj.animation_data.action_extrapolation = 'HOLD' if active_layer.blend_type == 'REPLACE' else 'HOLD'
            except Exception: pass
            
            # Try to assign the active track if the API supports it
            try:
                for t in obj.animation_data.nla_tracks:
                    t.is_solo = False
                active_track.is_solo = False
            except Exception: pass
            
            # Ensure the action matches the layer state
            try:
                if active_layer.is_muted:
                    if getattr(obj.animation_data, 'use_tweak_mode', False):
                        for window in context.window_manager.windows:
                            for area in window.screen.areas:
                                if area.type == 'NLA_EDITOR':
                                    with context.temp_override(window=window, area=area, active_object=obj):
                                        try: bpy.ops.nla.tweakmode_exit(isolate_action=False)
                                        except: pass
                                    break
                    obj.animation_data.action = None
                    active_track.mute = True
                else:
                    if enter_tweak_mode:
                        safe_enter_tweak_mode(context, obj, active_track, active_strip)
                    
                    if getattr(obj.animation_data, 'use_tweak_mode', False):
                        active_track.mute = False
                        bind_strip_slot(obj, active_strip)
                    else:
                        # Dual-mode support: If Tweak Mode is not active (e.g. no NLA Editor open in layout),
                        # directly assign the active layer's action to obj.animation_data.action if frame_start <= 1.0
                        # so native keyframing (I / auto-key) writes to this layer.
                        # For strips with frame_start > 1.0, they MUST evaluate through NLA track so timeline offsets are respected!
                        if active_strip.frame_start <= 1.0:
                            obj.animation_data.action = active_strip.action
                            bind_strip_slot(obj, active_strip)
                            active_track.mute = True
                            try:
                                obj.animation_data.action_blend_type = active_layer.blend_type
                                obj.animation_data.action_extrapolation = 'HOLD'
                            except Exception: pass
                        else:
                            active_track.mute = False
                            bind_strip_slot(obj, active_strip)
                            try:
                                obj.animation_data.action = None
                            except Exception: pass
            except Exception: pass
                
            # Ensure timeline displays in frames and proper curve visibility
            ensure_timeline_frame_display(context)
            
    # Now configure the active track ONLY for the active object
    if active_layer and active_track:
        pass

def invisible_tweakmode_swap(context, exit_first=False, enter_second=False):
    """Executes Tweak Mode transitions invisibly within a single frame redraw, avoiding UI flashes."""
    target_area = None
    target_window = None
    original_type = None
    
    # Prefer an existing NLA Editor if one happens to be open
    for window in context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'NLA_EDITOR':
                target_window = window
                target_area = area
                original_type = area.type
                break
        if target_area: break
        
    if not target_area:
        # Hijack an inactive background area (e.g. Outliner or Timeline)
        for window in context.window_manager.windows:
            for area in window.screen.areas:
                if area != context.area and area.type != 'PROPERTIES':
                    target_window = window
                    target_area = area
                    original_type = area.type
                    break
            if target_area: break
            
    if not target_area:
        # Fallback to pure logic if no safe area exists
        execute_sync_logic(context, enter_tweak_mode=enter_second)
        return
        
    try:
        # Synchronous hijack: Swap the area type in memory
        if target_area.type != 'NLA_EDITOR':
            target_area.type = 'NLA_EDITOR'
            
        target_region = None
        for region in target_area.regions:
            if region.type == 'WINDOW':
                target_region = region
                break
                
        if target_region:
            override = context.copy()
            override['window'] = target_window
            override['area'] = target_area
            override['region'] = target_region
            if hasattr(target_area, "spaces"):
                for space in target_area.spaces:
                    if space.type == 'NLA_EDITOR':
                        override['space_data'] = space
                        break
            override['active_object'] = get_active_object(context)
            if override['active_object']:
                override['selected_objects'] = [override['active_object']]
            
            with context.temp_override(**override):
                if exit_first:
                    try: bpy.ops.nla.tweakmode_exit(isolate_action=False)
                    except:
                        try: bpy.ops.nla.tweakmode_exit()
                        except: pass
                    
                execute_sync_logic(context, enter_tweak_mode=enter_second)
                
                # CRITICAL: Force a depsgraph update before entering Tweak Mode. 
                # If we don't, Blender caches the uninitialized strip state and COMBINE mode acts like REPLACE until the user manually switches layers.
                if context.view_layer:
                    context.view_layer.update()
                
                # Tweak Mode is already safely entered by execute_sync_logic
                
                # CRITICAL: Force another depsgraph update and tag the object after entering Tweak Mode.
                # If we don't, Blender's viewport gets stuck displaying the previous layer's poses until the user manually toggles a property like Mute.
                if context.view_layer:
                    context.view_layer.update()
                
                obj = get_active_object(context)
                if obj:
                    obj.update_tag(refresh={'OBJECT', 'DATA'})
                            
                    if context.view_layer:
                        context.view_layer.update()
        else:
            # Fallback if region generation failed
            execute_sync_logic(context, enter_tweak_mode=enter_second)
            
    finally:
        # CRITICAL: Instantly restore the area type BEFORE Blender executes its next frame redraw!
        # This completely hides the NLA Editor swap from the user's screen.
        if target_area and original_type and target_area.type != original_type:
            target_area.type = original_type
        reset_auto_keyframe_cache()
        
        # CRITICAL: Force onion skin to instantly flush and rebuild after a tweakmode swap (layer change)
        try:
            from . import anim_onion_skin
            anim_onion_skin._cached_data.clear()
            anim_onion_skin._cached_keyframe_frames.clear()
        except: pass

def update_layer_property_light(self, context):
    sync_layer_light(context)

def update_layer_property_heavy(self, context):
    invisible_tweakmode_swap(context, exit_first=True, enter_second=True)

def update_active_layer(self, context):
    invisible_tweakmode_swap(context, exit_first=True, enter_second=True)

