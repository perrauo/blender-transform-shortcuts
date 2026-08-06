import bpy
from bpy.types import Panel, Operator, PropertyGroup
from bpy.props import FloatVectorProperty, BoolProperty
from bpy.app.handlers import persistent
import json
import math


# ─────────────────────────────────────────────────────────────────────────────
# Reliable transform detection
# ─────────────────────────────────────────────────────────────────────────────

def _is_transform_running():
    for window in bpy.context.window_manager.windows:
        for op in window.modal_operators:
            if op.bl_idname.startswith("TRANSFORM_OT"):
                return True
    return False


_was_transforming = False


# ─────────────────────────────────────────────────────────────────────────────
# Core logic (shared / Live Offset)
# ─────────────────────────────────────────────────────────────────────────────

def _iter_channels(bone):
    name = bone.name

    path = f'pose.bones["{name}"].location'
    for i, v in enumerate(bone.location):
        yield path, i, v

    if bone.rotation_mode == 'QUATERNION':
        path = f'pose.bones["{name}"].rotation_quaternion'
        q = bone.rotation_quaternion
        for i, v in enumerate((q.w, q.x, q.y, q.z)):
            yield path, i, v
    elif bone.rotation_mode == 'AXIS_ANGLE':
        path = f'pose.bones["{name}"].rotation_axis_angle'
        for i, v in enumerate(bone.rotation_axis_angle):
            yield path, i, v
    else:
        path = f'pose.bones["{name}"].rotation_euler'
        for i, v in enumerate(bone.rotation_euler):
            yield path, i, v

    path = f'pose.bones["{name}"].scale'
    for i, v in enumerate(bone.scale):
        yield path, i, v


def _get_action_fcurves(action, obj):
    fcurves = getattr(action, "fcurves", None)
    if fcurves is not None:
        return fcurves

    anim_data = obj.animation_data
    slot = anim_data.action_slot if anim_data else None
    if slot is None:
        return None

    for layer in action.layers:
        for strip in layer.strips:
            for channelbag in strip.channelbags:
                if channelbag.slot_handle == slot.handle:
                    return channelbag.fcurves
    return None


def collect_offsets(context):
    obj = context.active_object
    if not obj or obj.type != 'ARMATURE' or obj.mode != 'POSE':
        return []
    if not obj.animation_data or not obj.animation_data.action:
        return []

    action = obj.animation_data.action
    fcurves = _get_action_fcurves(action, obj)
    if fcurves is None:
        return []

    selected = context.selected_pose_bones or []
    if not selected:
        return []

    current_frame = float(context.scene.frame_current)
    pending = []

    for bone in selected:
        for data_path, array_index, live_val in _iter_channels(bone):
            fc = fcurves.find(data_path, index=array_index)
            if fc is None or fc.lock or fc.mute:
                continue

            offset = live_val - fc.evaluate(current_frame)
            if abs(offset) < 1e-9:
                continue

            pending.append((fc, offset, data_path, array_index, bone.name))

    return pending


def apply_offsets(pending):
    for fc, offset, *_ in pending:
        for kp in fc.keyframe_points:
            kp.co.y += offset
            kp.handle_left.y += offset
            kp.handle_right.y += offset
        fc.update()


def get_keyed_value(action, obj, data_path, array_index, frame):
    fcurves = _get_action_fcurves(action, obj)
    if fcurves is None:
        return None
    fc = fcurves.find(data_path, index=array_index)
    if fc is None:
        return None
    return fc.evaluate(frame)


# ─────────────────────────────────────────────────────────────────────────────
# Live Offset PropertyGroup
# ─────────────────────────────────────────────────────────────────────────────

def _update_loc(self, context):
    if self.suppress_update or _is_transform_running():
        return
    bone = context.active_pose_bone
    if not bone:
        return
    obj = context.active_object
    if not obj or not obj.animation_data or not obj.animation_data.action:
        return
    action = obj.animation_data.action
    frame = context.scene.frame_current
    name = bone.name

    for i in range(3):
        keyed = get_keyed_value(action, obj, f'pose.bones["{name}"].location', i, frame)
        bone.location[i] = (keyed + self.loc_offset[i]) if keyed is not None else self.loc_offset[i]


def _update_rot(self, context):
    if self.suppress_update or _is_transform_running():
        return
    bone = context.active_pose_bone
    if not bone:
        return
    obj = context.active_object
    if not obj or not obj.animation_data or not obj.animation_data.action:
        return
    action = obj.animation_data.action
    frame = context.scene.frame_current
    name = bone.name

    if bone.rotation_mode == 'QUATERNION':
        path = f'pose.bones["{name}"].rotation_quaternion'
        for i in range(4):
            keyed = get_keyed_value(action, obj, path, i, frame)
            bone.rotation_quaternion[i] = (keyed + self.rot_offset[i]) if keyed is not None else self.rot_offset[i]
    elif bone.rotation_mode == 'AXIS_ANGLE':
        path = f'pose.bones["{name}"].rotation_axis_angle'
        for i in range(4):
            keyed = get_keyed_value(action, obj, path, i, frame)
            bone.rotation_axis_angle[i] = (keyed + self.rot_offset[i]) if keyed is not None else self.rot_offset[i]
    else:
        path = f'pose.bones["{name}"].rotation_euler'
        for i in range(3):
            keyed = get_keyed_value(action, obj, path, i, frame)
            bone.rotation_euler[i] = (keyed + self.rot_offset[i]) if keyed is not None else self.rot_offset[i]


def _update_scale(self, context):
    if self.suppress_update or _is_transform_running():
        return
    bone = context.active_pose_bone
    if not bone:
        return
    obj = context.active_object
    if not obj or not obj.animation_data or not obj.animation_data.action:
        return
    action = obj.animation_data.action
    frame = context.scene.frame_current
    name = bone.name

    for i in range(3):
        keyed = get_keyed_value(action, obj, f'pose.bones["{name}"].scale', i, frame)
        bone.scale[i] = (keyed + self.scale_offset[i]) if keyed is not None else self.scale_offset[i]


class LIVEOFFSET_PG_settings(PropertyGroup):
    suppress_update: BoolProperty(default=False)

    loc_offset: FloatVectorProperty(
        name="Location Offset",
        size=3,
        subtype='TRANSLATION',
        precision=5,
        update=_update_loc,
    )
    rot_offset: FloatVectorProperty(
        name="Rotation Offset",
        size=4,
        precision=5,
        update=_update_rot,
    )
    scale_offset: FloatVectorProperty(
        name="Scale Offset",
        size=3,
        subtype='XYZ',
        precision=5,
        update=_update_scale,
    )


def sync_offsets_from_bone(context):
    if _is_transform_running():
        return

    wm = context.window_manager
    settings = wm.live_offset_settings
    bone = context.active_pose_bone
    obj = context.active_object

    if not bone or not obj or not obj.animation_data or not obj.animation_data.action:
        return

    action = obj.animation_data.action
    frame = float(context.scene.frame_current)
    name = bone.name

    settings.suppress_update = True

    for i in range(3):
        keyed = get_keyed_value(action, obj, f'pose.bones["{name}"].location', i, frame)
        settings.loc_offset[i] = (bone.location[i] - keyed) if keyed is not None else bone.location[i]

    if bone.rotation_mode == 'QUATERNION':
        q = bone.rotation_quaternion
        for i, v in enumerate((q.w, q.x, q.y, q.z)):
            keyed = get_keyed_value(action, obj, f'pose.bones["{name}"].rotation_quaternion', i, frame)
            settings.rot_offset[i] = (v - keyed) if keyed is not None else v
    elif bone.rotation_mode == 'AXIS_ANGLE':
        for i, v in enumerate(bone.rotation_axis_angle):
            keyed = get_keyed_value(action, obj, f'pose.bones["{name}"].rotation_axis_angle', i, frame)
            settings.rot_offset[i] = (v - keyed) if keyed is not None else v
    else:
        for i, v in enumerate(bone.rotation_euler):
            keyed = get_keyed_value(action, obj, f'pose.bones["{name}"].rotation_euler', i, frame)
            settings.rot_offset[i] = (v - keyed) if keyed is not None else v
        settings.rot_offset[3] = 0.0

    for i in range(3):
        keyed = get_keyed_value(action, obj, f'pose.bones["{name}"].scale', i, frame)
        settings.scale_offset[i] = (bone.scale[i] - keyed) if keyed is not None else bone.scale[i]

    settings.suppress_update = False


# ─────────────────────────────────────────────────────────────────────────────
# Transform Shortcuts PropertyGroup – actual transform values
# ─────────────────────────────────────────────────────────────────────────────

def _update_ts_loc(self, context):
    if self.suppress_update or _is_transform_running():
        return
    bone = context.active_pose_bone
    if not bone:
        return
    bone.location = self.location[:]


def _update_ts_rot(self, context):
    if self.suppress_update or _is_transform_running():
        return
    bone = context.active_pose_bone
    if not bone:
        return

    if bone.rotation_mode == 'QUATERNION':
        bone.rotation_quaternion = self.rotation[:]
    elif bone.rotation_mode == 'AXIS_ANGLE':
        bone.rotation_axis_angle = self.rotation[:]
    else:
        bone.rotation_euler = self.rotation[:3]


def _update_ts_scale(self, context):
    if self.suppress_update or _is_transform_running():
        return
    bone = context.active_pose_bone
    if not bone:
        return
    bone.scale = self.scale[:]


class TRANSFORMSHORTCUTS_PG_settings(PropertyGroup):
    suppress_update: BoolProperty(default=False)

    location: FloatVectorProperty(
        name="Location",
        size=3,
        subtype='TRANSLATION',
        precision=5,
        update=_update_ts_loc,
    )
    rotation: FloatVectorProperty(
        name="Rotation",
        size=4,
        precision=5,
        update=_update_ts_rot,
    )
    scale: FloatVectorProperty(
        name="Scale",
        size=3,
        subtype='XYZ',
        default=(1.0, 1.0, 1.0),
        precision=5,
        update=_update_ts_scale,
    )


def sync_transforms_from_bone(context):
    if _is_transform_running():
        return

    wm = context.window_manager
    settings = wm.transform_shortcuts_settings
    bone = context.active_pose_bone

    if not bone:
        return

    settings.suppress_update = True

    settings.location = bone.location[:]

    if bone.rotation_mode == 'QUATERNION':
        q = bone.rotation_quaternion
        settings.rotation = (q.w, q.x, q.y, q.z)
    elif bone.rotation_mode == 'AXIS_ANGLE':
        settings.rotation = bone.rotation_axis_angle[:]
    else:
        settings.rotation = (*bone.rotation_euler[:], 0.0)

    settings.scale = bone.scale[:]

    settings.suppress_update = False


# ─────────────────────────────────────────────────────────────────────────────
# Safe refresh
# ─────────────────────────────────────────────────────────────────────────────

def _tag_ui_redraw():
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                for region in area.regions:
                    if region.type == 'UI':
                        region.tag_redraw()


@persistent
def _on_depsgraph_update(scene, depsgraph):
    global _was_transforming

    transforming = _is_transform_running()

    if _was_transforming and not transforming:
        try:
            sync_offsets_from_bone(bpy.context)
            sync_transforms_from_bone(bpy.context)
            _tag_ui_redraw()
        except Exception:
            pass

    _was_transforming = transforming

    if transforming:
        return

    obj = bpy.context.active_object
    if not obj or obj.type != 'ARMATURE' or obj.mode != 'POSE':
        return

    try:
        wm = bpy.context.window_manager
        bone = bpy.context.active_pose_bone
        frame = scene.frame_current

        fingerprint = (bone.name if bone else "", frame)
        last = getattr(wm, "_ts_last_fingerprint", None)
        if fingerprint != last:
            wm._ts_last_fingerprint = fingerprint
            sync_offsets_from_bone(bpy.context)
            sync_transforms_from_bone(bpy.context)
            _tag_ui_redraw()
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Live Offset Operators
# ─────────────────────────────────────────────────────────────────────────────

class LIVEOFFSET_OT_apply(Operator):
    bl_idname = "liveoffset.apply"
    bl_label = "Apply Offset"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if _is_transform_running():
            self.report({'WARNING'}, "Finish the transform first")
            return {'CANCELLED'}

        pending = collect_offsets(context)
        if not pending:
            self.report({'INFO'}, "Nothing to do – all offsets are zero")
            return {'CANCELLED'}

        bpy.ops.ed.undo_push(message="Live Transform Offset")
        apply_offsets(pending)
        context.scene.frame_set(context.scene.frame_current)
        sync_offsets_from_bone(context)

        bones = {p[4] for p in pending}
        self.report({'INFO'}, f"Shifted {len(pending)} channel(s) across {len(bones)} bone(s)")
        return {'FINISHED'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (
            obj
            and obj.type == 'ARMATURE'
            and obj.mode == 'POSE'
            and obj.animation_data
            and obj.animation_data.action
            and context.selected_pose_bones
            and not _is_transform_running()
        )


class LIVEOFFSET_OT_refresh(Operator):
    bl_idname = "liveoffset.refresh"
    bl_label = "Refresh Offsets"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        if not _is_transform_running():
            sync_offsets_from_bone(context)
        return {'FINISHED'}


class LIVEOFFSET_OT_reset(Operator):
    """Reset all offsets to zero (restores bone to keyed values)"""
    bl_idname = "liveoffset.reset"
    bl_label = "Reset Offsets"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if _is_transform_running():
            self.report({'WARNING'}, "Finish the transform first")
            return {'CANCELLED'}

        settings = context.window_manager.live_offset_settings

        settings.suppress_update = True
        settings.loc_offset = (0.0, 0.0, 0.0)
        settings.rot_offset = (0.0, 0.0, 0.0, 0.0)
        settings.scale_offset = (0.0, 0.0, 0.0)
        settings.suppress_update = False

        _update_loc(settings, context)
        _update_rot(settings, context)
        _update_scale(settings, context)

        self.report({'INFO'}, "Offsets reset to zero")
        return {'FINISHED'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (
            obj
            and obj.type == 'ARMATURE'
            and obj.mode == 'POSE'
            and context.active_pose_bone
            and not _is_transform_running()
        )


class LIVEOFFSET_OT_copy(Operator):
    bl_idname = "liveoffset.copy"
    bl_label = "Copy Offsets"
    bl_options = {'REGISTER'}

    def execute(self, context):
        settings = context.window_manager.live_offset_settings

        data = {
            "live_offset": True,
            "loc":   list(settings.loc_offset),
            "rot":   list(settings.rot_offset),
            "scale": list(settings.scale_offset),
        }

        try:
            context.window_manager.clipboard = json.dumps(data, separators=(',', ':'))
            self.report({'INFO'}, "Offsets copied to clipboard")
        except Exception as e:
            self.report({'ERROR'}, f"Copy failed: {e}")
            return {'CANCELLED'}

        return {'FINISHED'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (
            obj
            and obj.type == 'ARMATURE'
            and obj.mode == 'POSE'
            and not _is_transform_running()
        )


class LIVEOFFSET_OT_paste(Operator):
    bl_idname = "liveoffset.paste"
    bl_label = "Paste Offsets"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if _is_transform_running():
            self.report({'WARNING'}, "Finish the transform first")
            return {'CANCELLED'}

        clipboard = context.window_manager.clipboard.strip()
        if not clipboard:
            self.report({'WARNING'}, "Clipboard is empty")
            return {'CANCELLED'}

        try:
            data = json.loads(clipboard)
        except json.JSONDecodeError:
            self.report({'WARNING'}, "Clipboard does not contain valid Live Offset data")
            return {'CANCELLED'}

        if not isinstance(data, dict) or not data.get("live_offset"):
            self.report({'WARNING'}, "Clipboard does not contain valid Live Offset data")
            return {'CANCELLED'}

        settings = context.window_manager.live_offset_settings

        settings.suppress_update = True

        try:
            if "loc" in data and len(data["loc"]) == 3:
                settings.loc_offset = data["loc"]
            if "rot" in data and len(data["rot"]) == 4:
                settings.rot_offset = data["rot"]
            if "scale" in data and len(data["scale"]) == 3:
                settings.scale_offset = data["scale"]
        except Exception as e:
            settings.suppress_update = False
            self.report({'ERROR'}, f"Paste failed: {e}")
            return {'CANCELLED'}

        settings.suppress_update = False

        _update_loc(settings, context)
        _update_rot(settings, context)
        _update_scale(settings, context)

        self.report({'INFO'}, "Offsets pasted from clipboard")
        return {'FINISHED'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (
            obj
            and obj.type == 'ARMATURE'
            and obj.mode == 'POSE'
            and context.active_pose_bone
            and not _is_transform_running()
        )


# ─────────────────────────────────────────────────────────────────────────────
# Transform Shortcuts Operators
# ─────────────────────────────────────────────────────────────────────────────

class TRANSFORMSHORTCUTS_OT_copy(Operator):
    bl_idname = "transformshortcuts.copy"
    bl_label = "Copy Transform"
    bl_options = {'REGISTER'}

    def execute(self, context):
        settings = context.window_manager.transform_shortcuts_settings
        bone = context.active_pose_bone

        data = {
            "transform_shortcuts": True,
            "location": list(settings.location),
            "rotation": list(settings.rotation),
            "scale":    list(settings.scale),
            "rotation_mode": bone.rotation_mode if bone else "XYZ",
        }

        try:
            context.window_manager.clipboard = json.dumps(data, separators=(',', ':'))
            self.report({'INFO'}, "Transform copied to clipboard")
        except Exception as e:
            self.report({'ERROR'}, f"Copy failed: {e}")
            return {'CANCELLED'}

        return {'FINISHED'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (
            obj
            and obj.type == 'ARMATURE'
            and obj.mode == 'POSE'
            and context.active_pose_bone
            and not _is_transform_running()
        )


class TRANSFORMSHORTCUTS_OT_paste(Operator):
    bl_idname = "transformshortcuts.paste"
    bl_label = "Paste Transform"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if _is_transform_running():
            self.report({'WARNING'}, "Finish the transform first")
            return {'CANCELLED'}

        clipboard = context.window_manager.clipboard.strip()
        if not clipboard:
            self.report({'WARNING'}, "Clipboard is empty")
            return {'CANCELLED'}

        try:
            data = json.loads(clipboard)
        except json.JSONDecodeError:
            self.report({'WARNING'}, "Clipboard does not contain valid Transform data")
            return {'CANCELLED'}

        if not isinstance(data, dict) or not data.get("transform_shortcuts"):
            self.report({'WARNING'}, "Clipboard does not contain valid Transform data")
            return {'CANCELLED'}

        settings = context.window_manager.transform_shortcuts_settings
        bone = context.active_pose_bone
        if not bone:
            self.report({'WARNING'}, "No active pose bone")
            return {'CANCELLED'}

        settings.suppress_update = True

        try:
            if "location" in data and len(data["location"]) == 3:
                settings.location = data["location"]
            if "rotation" in data and len(data["rotation"]) == 4:
                settings.rotation = data["rotation"]
            if "scale" in data and len(data["scale"]) == 3:
                settings.scale = data["scale"]
        except Exception as e:
            settings.suppress_update = False
            self.report({'ERROR'}, f"Paste failed: {e}")
            return {'CANCELLED'}

        settings.suppress_update = False

        _update_ts_loc(settings, context)
        _update_ts_rot(settings, context)
        _update_ts_scale(settings, context)

        self.report({'INFO'}, "Transform pasted from clipboard")
        return {'FINISHED'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (
            obj
            and obj.type == 'ARMATURE'
            and obj.mode == 'POSE'
            and context.active_pose_bone
            and not _is_transform_running()
        )


class TRANSFORMSHORTCUTS_OT_refresh(Operator):
    bl_idname = "transformshortcuts.refresh"
    bl_label = "Refresh Transform"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        if not _is_transform_running():
            sync_transforms_from_bone(context)
        return {'FINISHED'}


# ─────────────────────────────────────────────────────────────────────────────
# Anim Shortcuts – Loop Detection
# ─────────────────────────────────────────────────────────────────────────────

# Tolerance for considering two channel values identical
_LOOP_TOL = 1e-5
# Minimum acceptable loop length (frames)
_MIN_LOOP_FRAMES = 2


def _collect_pose_fcurves(action, obj):
    """Return list of (fcurve, data_path, array_index) for every pose-bone channel."""
    fcurves = _get_action_fcurves(action, obj)
    if fcurves is None:
        return []

    result = []
    for fc in fcurves:
        if fc.data_path.startswith('pose.bones[') and not fc.lock and not fc.mute:
            result.append((fc, fc.data_path, fc.array_index))
    return result


def _get_action_frame_range(fcurves):
    """Return (min_frame, max_frame) from all keyframe points, or None."""
    min_f = float('inf')
    max_f = float('-inf')
    has_keys = False

    for fc, _, _ in fcurves:
        for kp in fc.keyframe_points:
            t = kp.co[0]
            if t < min_f:
                min_f = t
            if t > max_f:
                max_f = t
            has_keys = True

    if not has_keys:
        return None
    return (math.floor(min_f), math.ceil(max_f))


def _pose_matches(fcurves, frame_a, frame_b, tol=_LOOP_TOL):
    """Return True if every channel evaluates identically at frame_a and frame_b."""
    for fc, _, _ in fcurves:
        va = fc.evaluate(frame_a)
        vb = fc.evaluate(frame_b)
        if abs(va - vb) > tol:
            return False
    return True


def _is_valid_period(fcurves, start, period, end, tol=_LOOP_TOL):
    """
    Verify that the animation is periodic with the given period over [start, end].
    Checks every integer frame in the first cycle against the shifted frame.
    Also requires that start pose matches start+period.
    """
    if period < _MIN_LOOP_FRAMES:
        return False
    if start + period > end:
        return False

    # Primary check: start pose must match the pose at start+period
    if not _pose_matches(fcurves, start, start + period, tol):
        return False

    # Full verification: every frame in the first cycle must match its counterpart
    # We sample every integer frame for maximum accuracy
    for f in range(int(start), int(start + period)):
        if f + period > end:
            break
        if not _pose_matches(fcurves, f, f + period, tol):
            return False

    return True


def detect_loop_period(context, tol=_LOOP_TOL):
    """
    Detect the shortest valid loop period for the active armature's action.
    Requires EVERY bone channel to match.
    Returns (start_frame, period) or (None, None) on failure.
    """
    obj = context.active_object
    if not obj or obj.type != 'ARMATURE' or not obj.animation_data or not obj.animation_data.action:
        return None, None

    action = obj.animation_data.action
    fcurves = _collect_pose_fcurves(action, obj)
    if not fcurves:
        return None, None

    frame_range = _get_action_frame_range(fcurves)
    if frame_range is None:
        return None, None

    start, end = frame_range
    total_len = end - start
    if total_len < _MIN_LOOP_FRAMES * 2:
        return None, None

    # Strategy:
    # 1. Compute the reference pose at 'start'
    # 2. Scan forward for frames where the pose matches the reference
    # 3. For each candidate period, fully verify the cycle
    # Prefer the shortest valid period that covers a reasonable amount of the action

    candidates = []

    # Scan every integer frame for a pose match with the start
    # (we start looking from start + MIN so we don't accept period 0)
    max_search = start + total_len // 2 + 1
    for candidate_end in range(int(start) + _MIN_LOOP_FRAMES, int(max_search) + 1):
        if _pose_matches(fcurves, start, candidate_end, tol):
            period = candidate_end - start
            if _is_valid_period(fcurves, start, period, end, tol):
                candidates.append(period)

    if not candidates:
        # Fallback: try a coarser scan in case of floating-point drift on non-integer keys
        # or if the true start is not exactly at the first key
        for candidate_end in range(int(start) + _MIN_LOOP_FRAMES, int(max_search) + 1, 1):
            if _pose_matches(fcurves, start, candidate_end, tol * 10):  # slightly looser
                period = candidate_end - start
                if _is_valid_period(fcurves, start, period, end, tol * 10):
                    candidates.append(period)
                    break

    if not candidates:
        return None, None

    # Return the shortest valid period
    best_period = min(candidates)
    return start, best_period


def truncate_after_loop(action, obj, start, period):
    """Delete every keyframe that lies strictly after start + period."""
    fcurves = _get_action_fcurves(action, obj)
    if fcurves is None:
        return 0

    cutoff = start + period
    removed = 0

    for fc in fcurves:
        # Walk backwards so removals don't invalidate indices
        for i in range(len(fc.keyframe_points) - 1, -1, -1):
            kp = fc.keyframe_points[i]
            if kp.co[0] > cutoff + 1e-6:  # strict after
                fc.keyframe_points.remove(kp)
                removed += 1
        fc.update()

    return removed


# ─────────────────────────────────────────────────────────────────────────────
# Anim Shortcuts Operators
# ─────────────────────────────────────────────────────────────────────────────

class ANIMSHORTCUTS_OT_detect_loop(Operator):
    """Detect the shortest looping period that holds for every bone and set the scene frame range to it"""
    bl_idname = "animshortcuts.detect_loop"
    bl_label = "Detect Loop"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        start, period = detect_loop_period(context)
        if start is None or period is None:
            self.report({'WARNING'}, "No valid loop detected (all bones must match)")
            return {'CANCELLED'}

        scene = context.scene
        scene.frame_start = int(start)
        scene.frame_end = int(start + period)
        # Also set preview range so the timeline window focuses on the loop
        scene.frame_preview_start = int(start)
        scene.frame_preview_end = int(start + period)
        scene.use_preview_range = True

        self.report({'INFO'}, f"Loop detected: frames {int(start)} → {int(start + period)} (period = {period})")
        return {'FINISHED'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (
            obj
            and obj.type == 'ARMATURE'
            and obj.mode == 'POSE'
            and obj.animation_data
            and obj.animation_data.action
            and not _is_transform_running()
        )


class ANIMSHORTCUTS_OT_detect_truncate_loop(Operator):
    """Detect the loop and delete every keyframe that lies after the end of the first cycle"""
    bl_idname = "animshortcuts.detect_truncate_loop"
    bl_label = "Detect and Truncate Loop"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        start, period = detect_loop_period(context)
        if start is None or period is None:
            self.report({'WARNING'}, "No valid loop detected (all bones must match)")
            return {'CANCELLED'}

        obj = context.active_object
        action = obj.animation_data.action

        bpy.ops.ed.undo_push(message="Detect and Truncate Loop")

        removed = truncate_after_loop(action, obj, start, period)

        scene = context.scene
        scene.frame_start = int(start)
        scene.frame_end = int(start + period)
        scene.frame_preview_start = int(start)
        scene.frame_preview_end = int(start + period)
        scene.use_preview_range = True

        # Force a refresh of the animation data
        context.scene.frame_set(context.scene.frame_current)

        self.report(
            {'INFO'},
            f"Loop truncated to frames {int(start)} → {int(start + period)} "
            f"(period = {period}, removed {removed} keyframe(s))"
        )
        return {'FINISHED'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (
            obj
            and obj.type == 'ARMATURE'
            and obj.mode == 'POSE'
            and obj.animation_data
            and obj.animation_data.action
            and not _is_transform_running()
        )


# ─────────────────────────────────────────────────────────────────────────────
# UI Panels
# ─────────────────────────────────────────────────────────────────────────────

class LIVEOFFSET_PT_panel(Panel):
    bl_label = "Live Offset"
    bl_idname = "LIVEOFFSET_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Transform Shortcuts"

    def draw(self, context):
        layout = self.layout
        obj = context.active_object
        wm = context.window_manager

        if not obj or obj.type != 'ARMATURE':
            layout.label(text="Select an Armature", icon='ERROR')
            return
        if obj.mode != 'POSE':
            layout.label(text="Switch to Pose Mode", icon='ERROR')
            return
        if not obj.animation_data or not obj.animation_data.action:
            layout.label(text="No active Action", icon='ERROR')
            return

        selected = context.selected_pose_bones or []
        if not selected:
            layout.label(text="Select one or more bones", icon='INFO')
            return

        transforming = _is_transform_running()
        settings = wm.live_offset_settings
        pending = collect_offsets(context)

        # Status only (no active bone / frame display)
        box = layout.box()
        box.label(text=f"Selected: {len(selected)} bone(s)", icon='BONE_DATA')

        if transforming:
            box.label(text="Transforming… (offsets frozen)", icon='TIME')
        elif not pending:
            box.label(text="All offsets ≈ 0", icon='CHECKMARK')
        else:
            box.label(text=f"{len(pending)} channel(s) with offset", icon='MODIFIER')

        bone = context.active_pose_bone
        if bone:
            # Rotation mode only
            row = layout.row()
            row.label(text="Rotation Mode")
            row.prop(bone, "rotation_mode", text="")

            # Location Offset
            box = layout.box()
            box.label(text="Location Offset")
            box.enabled = not transforming
            box.prop(settings, "loc_offset", text="")

            # Rotation Offset
            box = layout.box()
            box.enabled = not transforming
            if bone.rotation_mode == 'QUATERNION':
                box.label(text="Rotation Offset (Quaternion)")
                row = box.row(align=True)
                row.prop(settings, "rot_offset", index=0, text="W")
                row.prop(settings, "rot_offset", index=1, text="X")
                row = box.row(align=True)
                row.prop(settings, "rot_offset", index=2, text="Y")
                row.prop(settings, "rot_offset", index=3, text="Z")
            elif bone.rotation_mode == 'AXIS_ANGLE':
                box.label(text="Rotation Offset (Axis-Angle)")
                box.prop(settings, "rot_offset", index=0, text="Angle")
                row = box.row(align=True)
                row.prop(settings, "rot_offset", index=1, text="X")
                row.prop(settings, "rot_offset", index=2, text="Y")
                row.prop(settings, "rot_offset", index=3, text="Z")
            else:
                box.label(text="Rotation Offset (Euler)")
                box.prop(settings, "rot_offset", index=0, text="X")
                box.prop(settings, "rot_offset", index=1, text="Y")
                box.prop(settings, "rot_offset", index=2, text="Z")

            # Scale Offset
            box = layout.box()
            box.label(text="Scale Offset")
            box.enabled = not transforming
            box.prop(settings, "scale_offset", text="")

        if pending and len(selected) > 1:
            layout.separator()
            layout.label(text="All selected offsets:")
            by_bone = {}
            for _, offset, path, idx, bone_name in pending:
                by_bone.setdefault(bone_name, []).append((path, idx, offset))

            for bone_name, channels in by_bone.items():
                bone_box = layout.box()
                bone_box.label(text=bone_name, icon='BONE_DATA')
                for path, idx, offset in channels:
                    short = path.split('"].')[-1]
                    row = bone_box.row()
                    row.label(text=f"{short}[{idx}]")
                    row.label(text=f"{offset:+.5f}")

        layout.separator()

        # Copy / Paste
        row = layout.row(align=True)
        row.enabled = not transforming
        row.operator("liveoffset.copy", text="Copy Offsets", icon='COPYDOWN')
        row.operator("liveoffset.paste", text="Paste Offsets", icon='PASTEDOWN')

        # Apply / Reset / Refresh
        row = layout.row(align=True)
        row.scale_y = 1.4
        row.enabled = not transforming
        row.operator("liveoffset.apply", text="Apply to Keyframes", icon='CHECKMARK')
        row.operator("liveoffset.reset", text="Reset", icon='LOOP_BACK')
        row.operator("liveoffset.refresh", text="", icon='FILE_REFRESH')


class TRANSFORMSHORTCUTS_PT_panel(Panel):
    bl_label = "Transform Shortcuts"
    bl_idname = "TRANSFORMSHORTCUTS_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Transform Shortcuts"

    def draw(self, context):
        layout = self.layout
        obj = context.active_object
        wm = context.window_manager

        if not obj or obj.type != 'ARMATURE':
            layout.label(text="Select an Armature", icon='ERROR')
            return
        if obj.mode != 'POSE':
            layout.label(text="Switch to Pose Mode", icon='ERROR')
            return

        bone = context.active_pose_bone
        if not bone:
            layout.label(text="Select a bone", icon='INFO')
            return

        transforming = _is_transform_running()
        settings = wm.transform_shortcuts_settings

        box = layout.box()
        box.label(text=f"Active: {bone.name}", icon='BONE_DATA')
        box.label(text=f"Frame: {context.scene.frame_current}", icon='TIME')

        if transforming:
            box.label(text="Transforming… (values frozen)", icon='TIME')

        row = layout.row()
        row.label(text="Rotation Mode")
        row.prop(bone, "rotation_mode", text="")

        # Location
        box = layout.box()
        box.label(text="Location")
        box.enabled = not transforming
        box.prop(settings, "location", text="")

        # Rotation
        box = layout.box()
        box.enabled = not transforming
        if bone.rotation_mode == 'QUATERNION':
            box.label(text="Rotation (Quaternion)")
            row = box.row(align=True)
            row.prop(settings, "rotation", index=0, text="W")
            row.prop(settings, "rotation", index=1, text="X")
            row = box.row(align=True)
            row.prop(settings, "rotation", index=2, text="Y")
            row.prop(settings, "rotation", index=3, text="Z")
        elif bone.rotation_mode == 'AXIS_ANGLE':
            box.label(text="Rotation (Axis-Angle)")
            box.prop(settings, "rotation", index=0, text="Angle")
            row = box.row(align=True)
            row.prop(settings, "rotation", index=1, text="X")
            row.prop(settings, "rotation", index=2, text="Y")
            row.prop(settings, "rotation", index=3, text="Z")
        else:
            box.label(text="Rotation (Euler)")
            box.prop(settings, "rotation", index=0, text="X")
            box.prop(settings, "rotation", index=1, text="Y")
            box.prop(settings, "rotation", index=2, text="Z")

        # Scale
        box = layout.box()
        box.label(text="Scale")
        box.enabled = not transforming
        box.prop(settings, "scale", text="")

        layout.separator()

        row = layout.row(align=True)
        row.scale_y = 1.4
        row.enabled = not transforming
        row.operator("transformshortcuts.copy", text="Copy Transform", icon='COPYDOWN')
        row.operator("transformshortcuts.paste", text="Paste Transform", icon='PASTEDOWN')

        row = layout.row()
        row.enabled = not transforming
        row.operator("transformshortcuts.refresh", text="Refresh", icon='FILE_REFRESH')


class ANIMSHORTCUTS_PT_panel(Panel):
    bl_label = "Anim Shortcuts"
    bl_idname = "ANIMSHORTCUTS_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Anim Shortcuts"

    def draw(self, context):
        layout = self.layout
        obj = context.active_object

        if not obj or obj.type != 'ARMATURE':
            layout.label(text="Select an Armature", icon='ERROR')
            return
        if obj.mode != 'POSE':
            layout.label(text="Switch to Pose Mode", icon='ERROR')
            return
        if not obj.animation_data or not obj.animation_data.action:
            layout.label(text="No active Action", icon='ERROR')
            return

        box = layout.box()
        box.label(text="Loop Detection", icon='LOOP_FORWARDS')
        box.label(text="Requires every bone channel to match")

        layout.separator()

        col = layout.column(align=True)
        col.scale_y = 1.4
        col.operator("animshortcuts.detect_loop", text="Detect Loop", icon='TIME')
        col.operator("animshortcuts.detect_truncate_loop", text="Detect and Truncate Loop", icon='TRASH')

        layout.separator()
        layout.label(text="Detect Loop → sets scene & preview range", icon='INFO')
        layout.label(text="Truncate → also deletes keys after the loop", icon='INFO')


# ─────────────────────────────────────────────────────────────────────────────
# Registration
# ─────────────────────────────────────────────────────────────────────────────

classes = (
    LIVEOFFSET_PG_settings,
    TRANSFORMSHORTCUTS_PG_settings,
    LIVEOFFSET_OT_apply,
    LIVEOFFSET_OT_refresh,
    LIVEOFFSET_OT_reset,
    LIVEOFFSET_OT_copy,
    LIVEOFFSET_OT_paste,
    TRANSFORMSHORTCUTS_OT_copy,
    TRANSFORMSHORTCUTS_OT_paste,
    TRANSFORMSHORTCUTS_OT_refresh,
    ANIMSHORTCUTS_OT_detect_loop,
    ANIMSHORTCUTS_OT_detect_truncate_loop,
    LIVEOFFSET_PT_panel,
    TRANSFORMSHORTCUTS_PT_panel,
    ANIMSHORTCUTS_PT_panel,
)


def register():
    for cls in classes:
        try:
            bpy.utils.register_class(cls)
        except ValueError:
            bpy.utils.unregister_class(cls)
            bpy.utils.register_class(cls)

    if not hasattr(bpy.types.WindowManager, "live_offset_settings"):
        bpy.types.WindowManager.live_offset_settings = bpy.props.PointerProperty(
            type=LIVEOFFSET_PG_settings
        )

    if not hasattr(bpy.types.WindowManager, "transform_shortcuts_settings"):
        bpy.types.WindowManager.transform_shortcuts_settings = bpy.props.PointerProperty(
            type=TRANSFORMSHORTCUTS_PG_settings
        )

    if _on_depsgraph_update not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph_update)


def unregister():
    if _on_depsgraph_update in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_on_depsgraph_update)

    if hasattr(bpy.types.WindowManager, "live_offset_settings"):
        del bpy.types.WindowManager.live_offset_settings

    if hasattr(bpy.types.WindowManager, "transform_shortcuts_settings"):
        del bpy.types.WindowManager.transform_shortcuts_settings

    for cls in reversed(classes):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass


if __name__ == "__main__":
    register()